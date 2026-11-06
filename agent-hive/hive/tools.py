"""Vetted tool library. Agents can only use tools defined here; models never write tools."""
import subprocess
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

MAX_OUT = 6000  # keep tool results small: local models have short context windows


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
    def __init__(self, workspace, allow_python=False):
        self.ws = Path(workspace).resolve()
        self.ws.mkdir(parents=True, exist_ok=True)
        obj = lambda props, req: {"type": "object", "properties": props, "required": req}
        s = lambda d: {"type": "string", "description": d}
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
        }
        if allow_python:
            self.tools["run_python"] = (
                "Run a Python script in the workspace folder (30 s limit) and return its output.",
                obj({"code": s("Python source code")}, ["code"]), self.run_python)

    # --- registry helpers -------------------------------------------------
    def names(self):
        return list(self.tools)

    def describe(self):
        return "\n".join(f"- {n}: {d}" for n, (d, _, _) in self.tools.items())

    def subset(self, names):
        return {n: self.tools[n] for n in names if n in self.tools}

    # --- tools --------------------------------------------------------------
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
        p.write_text(content)
        return f"wrote {len(content)} chars to {path}"

    def list_files(self):
        files = [str(p.relative_to(self.ws)) for p in sorted(self.ws.rglob("*")) if p.is_file()]
        return "\n".join(files) or "(workspace is empty)"

    def run_python(self, code):
        script = self.ws / ".agent_script.py"
        script.write_text(code)
        try:
            r = subprocess.run([sys.executable, script.name], cwd=self.ws,
                               capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return "ERROR: script exceeded 30 s and was stopped"
        out = r.stdout + (f"\n[stderr]\n{r.stderr}" if r.stderr else "")
        return _clip(f"exit code {r.returncode}\n{out}")
