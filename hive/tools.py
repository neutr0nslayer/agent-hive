"""Tool library: built-in vetted tools + dynamically synthesized & persisted custom tools."""
import json
import os
import subprocess
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from .channels.email_client import EmailConfigError, fetch_unread_from_last
from .tool_creator import execute_custom_tool

MAX_OUT = 6000  # keep tool results concise for model context windows


def _clip(s, n=MAX_OUT):
    return s if len(s) <= n else s[:n] + f"\n...[truncated {len(s) - n} chars]"


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())


class ToolLibrary:
    def __init__(self, workspace, allow_python=False, registry=None, custom_tools_dir=None):
        self.ws = Path(workspace).resolve()
        self.ws.mkdir(parents=True, exist_ok=True)
        self.reg = registry
        self.custom_dir = Path(custom_tools_dir).resolve() if custom_tools_dir else (self.ws.parent / "custom_tools")
        self.custom_dir.mkdir(parents=True, exist_ok=True)
        self._email_summarizer_cb = None
        self._email_batch_draft_cb = None

        obj = lambda props, req: {"type": "object", "properties": props, "required": req}
        s = lambda d: {"type": "string", "description": d}
        i = lambda d: {"type": "integer", "description": d}
        # name -> (description, JSON schema, function)
        self.tools = {
            "web_fetch": ("Fetch a web page or text URL and return its readable text.",
                          obj({"url": s("http(s) URL")}, ["url"]), self.web_fetch),
            "read_file": ("Read a text file from the workspace.",
                          obj({"path": s("path relative to the workspace")}, ["path"]),
                          self.read_file),
            "write_file": ("Create or overwrite a text file in the workspace.",
                           obj({"path": s("path relative to the workspace"),
                                "content": s("full file content")}, ["path", "content"]),
                           self.write_file),
            "list_files": ("List all files in the workspace.", obj({}, []), self.list_files),
            "list_unread_emails": (
                "Scan the last N inbox emails (default 100) and return unread email headers & snippets.",
                obj({"scan_last": i("number of recent inbox messages to scan (default 100, max 500)")}, []),
                self.list_unread_emails,
            ),
            "summarize_important_emails": (
                "Scan unread emails from the last 100 inbox messages, filter out automated noise, "
                "and produce a prioritized Markdown summary of important emails.",
                obj({"scan_last": i("number of recent inbox messages to scan (default 100)")}, []),
                self.summarize_important_emails,
            ),
            "batch_prepare_email_drafts": (
                "Scan unread emails from the last 100 messages and prepare editable reply drafts "
                "for all important/actionable emails at once in the Inbox queue.",
                obj({
                    "scan_last": i("number of recent inbox messages to scan (default 100)"),
                    "instructions": s("optional reply style/tone instructions"),
                }, []),
                self.batch_prepare_email_drafts,
            ),
        }
        if allow_python:
            self.tools["run_python"] = (
                "Run a Python script in the workspace folder (30 s limit) and return its output.",
                obj({"code": s("Python source code")}, ["code"]), self.run_python)

        self._builtin_names = set(self.tools.keys())
        self.custom_meta = {}  # name -> dict metadata from SQLite
        self.load_custom_tools()

    # --- registry helpers -------------------------------------------------
    def builtin_names(self) -> set[str]:
        return set(self._builtin_names)

    def names(self):
        return list(self.tools)

    def describe(self):
        lines = []
        for n, (d, _, _) in self.tools.items():
            tag = " [custom]" if n in self.custom_meta else ""
            lines.append(f"- {n}{tag}: {d}")
        return "\n".join(lines)

    def subset(self, names):
        return {n: self.tools[n] for n in names if n in self.tools}

    def list_tool_details(self) -> list[dict]:
        out = []
        for name, (desc, schema, _) in self.tools.items():
            meta = self.custom_meta.get(name)
            out.append({
                "name": name,
                "description": desc,
                "parameters": schema,
                "is_custom": bool(meta),
                "created_by_model": meta["created_by_model"] if meta else "built-in",
                "created_by_user": meta["created_by_user"] if meta else "system",
                "use_count": meta["use_count"] if meta else None,
                "code": meta["code"] if meta else None,
                "tests": meta["tests"] if meta else None,
                "created_at": meta["created_at"] if meta else None,
            })
        return out

    def load_custom_tools(self):
        """Load all persisted custom tools from SQLite at startup."""
        if not self.reg:
            return
        for spec in self.reg.list_custom_tools():
            if spec["name"] not in self._builtin_names:
                self.register_custom_tool(spec)

    def register_custom_tool(self, spec: dict):
        """Hot-register a validated custom tool into memory and mirror source on disk."""
        name = spec["name"]
        if name in self._builtin_names:
            raise ValueError(f"cannot overwrite built-in tool '{name}'")
        code = spec["code"]
        try:
            (self.custom_dir / f"{name}.py").write_text(code, encoding="utf-8")
        except OSError:
            pass

        def _invoke(**kwargs):
            if self.reg:
                self.reg.bump_custom_tool_use(name)
                fresh = self.reg.get_custom_tool(name)
                if fresh:
                    self.custom_meta[name] = fresh
            return _clip(execute_custom_tool(code, self.ws, kwargs))

        self.tools[name] = (spec["description"], spec["parameters"], _invoke)
        self.custom_meta[name] = spec

    def unregister_custom_tool(self, name: str) -> bool:
        if name in self._builtin_names:
            return False
        removed = self.tools.pop(name, None) is not None
        self.custom_meta.pop(name, None)
        try:
            (self.custom_dir / f"{name}.py").unlink(missing_ok=True)
        except OSError:
            pass
        if self.reg:
            removed = self.reg.delete_custom_tool(name) or removed
        return removed

    # --- built-in tools -----------------------------------------------------
    def _path(self, rel):
        p = (self.ws / rel).resolve()
        if not p.is_relative_to(self.ws):
            raise ValueError("path must stay inside the workspace")
        return p

    def web_fetch(self, url):
        if not url.startswith(("http://", "https://")):
            return "ERROR: url must start with http:// or https://"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 agent-hive"})
        with urllib.request.urlopen(req, timeout=20) as r:
            ctype = r.headers.get_content_type() or ""
            raw = r.read(1_000_000).decode(r.headers.get_content_charset() or "utf-8",
                                           errors="replace")
        if "html" in ctype:
            p = _Text()
            p.feed(raw)
            raw = "\n".join(p.parts)
        return _clip(raw)

    def read_file(self, path):
        return _clip(self._path(path).read_text(errors="replace"))

    def write_file(self, path, content):
        p = self._path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {path}"

    def list_files(self):
        files = [str(p.relative_to(self.ws)) for p in sorted(self.ws.rglob("*")) if p.is_file()]
        return "\n".join(files) or "(workspace is empty)"

    def list_unread_emails(self, scan_last=100):
        try:
            msgs = fetch_unread_from_last(self.reg, scan_last=scan_last, body_chars=500)
        except EmailConfigError as e:
            return f"ERROR: {e}"
        if not msgs:
            return f"No unread emails found in the last {scan_last} messages."
        compact = [
            {
                "uid": m["uid"],
                "from": m["from"],
                "subject": m["subject"],
                "date": m["date"],
                "automated": m["is_automated"],
                "snippet": m["body"][:260],
            }
            for m in msgs
        ]
        return _clip(json.dumps({"unread_count": len(msgs), "scanned_last": scan_last, "emails": compact}, indent=1))

    def summarize_important_emails(self, scan_last=100):
        if not self._email_summarizer_cb:
            return "ERROR: email summarizer is not initialized."
        res = self._email_summarizer_cb(scan_last=scan_last)
        return _clip(res["summary_markdown"])

    def batch_prepare_email_drafts(self, scan_last=100, instructions=""):
        if not self._email_batch_draft_cb:
            return "ERROR: email batch drafter is not initialized."
        res = self._email_batch_draft_cb(scan_last=scan_last, instructions=instructions)
        return _clip(json.dumps({
            "scanned_last": res["scanned_last"],
            "unread_found": res["unread_found"],
            "drafts_created": len(res["drafts"]),
            "draft_ids": [d["id"] for d in res["drafts"]],
            "message": res["message"],
        }, indent=1))

    def run_python(self, code):
        script = self.ws / ".agent_script.py"
        script.write_text(code, encoding="utf-8")
        safe_env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
        safe_env["PYTHONIOENCODING"] = "utf-8"
        try:
            r = subprocess.run([sys.executable, script.name], cwd=self.ws, env=safe_env,
                               capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return "ERROR: script exceeded 30 s and was stopped"
        finally:
            script.unlink(missing_ok=True)
        out = r.stdout + (f"\n[stderr]\n{r.stderr}" if r.stderr else "")
        return _clip(f"exit code {r.returncode}\n{out}")
