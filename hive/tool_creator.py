"""Dynamic Tool Creator: uses the highest-capability model (deepseek-chat) to synthesize,
AST-check, unit-test, repair, and persist reusable custom tools."""
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .llm import LLMError, chat, resolve_api_key

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

ALLOWED_IMPORTS = {
    "math", "statistics", "decimal", "fractions", "random", "re", "json", "csv",
    "datetime", "time", "calendar", "collections", "itertools", "functools",
    "operator", "string", "textwrap", "difflib", "hashlib", "base64", "uuid",
    "urllib.parse", "html", "html.parser", "xml.etree.ElementTree", "pathlib",
    "io", "typing", "dataclasses", "enum", "copy", "bisect", "heapq", "zlib",
}

FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "__import__", "breakpoint", "input",
}

FORBIDDEN_ATTRS = {
    "system", "popen", "spawn", "fork", "kill", "remove", "unlink", "rmdir",
    "rmtree", "chmod", "chown", "environ", "getenv", "putenv",
}

SYNTHESIS_SYSTEM_PROMPT = """You are an expert Python tool architect for agent-hive.
Design and implement a single clean, reusable, self-contained Python tool.

Output STRICTLY a single JSON object (no markdown outside JSON) with these exact keys:
{
  "name": "lowercase_snake_case_name",
  "description": "One concise sentence describing what this tool does.",
  "parameters": {
    "type": "object",
    "properties": {
      "arg1": {"type": "string", "description": "..."}
    },
    "required": ["arg1"]
  },
  "code": "Python source code defining def run(...) -> str",
  "tests": "Python unit test code that calls run(...) with assertions"
}

STRICT RULES:
1. Only use Python standard library modules from this safe allowlist:
   math, statistics, decimal, fractions, random, re, json, csv, datetime, time, calendar,
   collections, itertools, functools, operator, string, textwrap, difflib, hashlib, base64,
   uuid, urllib.parse, html, html.parser, xml.etree.ElementTree, pathlib, io, typing,
   dataclasses, enum, copy, bisect, heapq, zlib.
2. NEVER import os, sys, subprocess, socket, shutil, ctypes, or urllib.request.
3. NEVER use eval, exec, compile, __import__, or open() directly.
   If the tool needs workspace files, call the pre-injected helper functions:
   - ws_read(rel_path: str) -> str
   - ws_write(rel_path: str, content: str) -> str
   - ws_list() -> list[str]
4. The function MUST be named `run`, accept exact keyword arguments matching `parameters.properties`,
   and ALWAYS return a string (`str`).
5. `parameters` must be a flat JSON schema object using types: string, integer, number, boolean, array, object.
6. `tests` MUST run at least 2 realistic assertions against `run(...)` (happy path + edge case).
   In `tests`, `run`, `ws_read`, `ws_write`, `ws_list` are already in scope.
"""


def check_ast_safety(code: str, label: str = "code"):
    """Validate syntax and enforce AST import/call allowlist."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"{label} syntax error on line {e.lineno}: {e.msg}"

    has_run = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            has_run = True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name
                if mod not in ALLOWED_IMPORTS:
                    return f"{label} imports disallowed module '{mod}'"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level != 0 or mod not in ALLOWED_IMPORTS:
                return f"{label} imports from disallowed module '{mod}'"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                return f"{label} calls forbidden builtin '{node.func.id}'"
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                return f"{label} calls open() directly; use ws_read()/ws_write() instead"
            if isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_ATTRS:
                return f"{label} accesses forbidden attribute '.{node.func.attr}'"
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
            return f"{label} accesses dunder attribute '{node.attr}'"

    if label == "code" and not has_run:
        return "code must define a top-level function named `run`"
    return None


def _build_runner_harness(code: str, tests: str = "", call_args_json: str = None) -> str:
    """Wrap tool code with workspace-confined helpers inside a scrubbed subprocess."""
    prelude = '''
import json, sys
from pathlib import Path

_WS = Path(sys.argv[1]).resolve()

def _ws_path(rel):
    p = (_WS / str(rel)).resolve()
    if not p.is_relative_to(_WS):
        raise ValueError("path must stay inside workspace")
    return p

def ws_read(rel_path):
    return _ws_path(rel_path).read_text(encoding="utf-8", errors="replace")

def ws_write(rel_path, content):
    p = _ws_path(rel_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(content), encoding="utf-8")
    return f"wrote {len(str(content))} chars to {rel_path}"

def ws_list():
    return [p.relative_to(_WS).as_posix() for p in sorted(_WS.rglob("*")) if p.is_file()]
'''
    if call_args_json is not None:
        tail = '''
if __name__ == "__main__":
    _args = json.loads(sys.stdin.read() or "{}")
    _res = run(**_args)
    sys.stdout.write(_res if isinstance(_res, str) else json.dumps(_res, default=str))
'''
        return prelude + "\n" + code + "\n" + tail
    return prelude + "\n" + code + "\n\n# --- TESTS ---\n" + tests + "\nprint('TOOL_TESTS_OK')\n"


def _scrubbed_env() -> dict:
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_tool_tests(code: str, tests: str, timeout: int = 15):
    """Run unit tests for a synthesized tool in a temp workspace with scrubbed env."""
    with tempfile.TemporaryDirectory(prefix="hive_tool_test_") as tmp:
        ws = Path(tmp) / "ws"
        ws.mkdir()
        harness = Path(tmp) / "test_tool.py"
        harness.write_text(_build_runner_harness(code, tests=tests), encoding="utf-8")
        try:
            r = subprocess.run(
                [sys.executable, str(harness), str(ws)],
                cwd=ws,
                env=_scrubbed_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"Tests exceeded {timeout}s timeout"
        if r.returncode != 0 or "TOOL_TESTS_OK" not in r.stdout:
            err = (r.stderr or r.stdout or "tests did not print TOOL_TESTS_OK").strip()
            return False, err[-1500:]
        return True, r.stdout.strip()


def execute_custom_tool(code: str, workspace: Path, kwargs: dict, timeout: int = 20) -> str:
    """Execute a persisted custom tool safely in a scrubbed-env subprocess."""
    err = check_ast_safety(code, label="code")
    if err:
        return f"ERROR: tool safety check failed: {err}"
    with tempfile.NamedTemporaryFile("w", suffix=".py", encoding="utf-8", delete=False) as tf:
        tf.write(_build_runner_harness(code, call_args_json=""))
        script_path = Path(tf.name)
    try:
        r = subprocess.run(
            [sys.executable, str(script_path), str(workspace)],
            input=json.dumps(kwargs),
            cwd=workspace,
            env=_scrubbed_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: custom tool exceeded {timeout}s timeout"
    finally:
        script_path.unlink(missing_ok=True)
    if r.returncode != 0:
        err_txt = (r.stderr or r.stdout or f"exit code {r.returncode}").strip()
        return f"ERROR: {err_txt[:1500]}"
    return r.stdout


def _extract_json_obj(text: str) -> dict:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n", "", s)
        s = re.sub(r"\n```$", "", s).strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    for m in re.finditer(r"\{", s):
        try:
            obj, _ = json.JSONDecoder().raw_decode(s[m.start():])
            if isinstance(obj, dict) and "code" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    raise ValueError("Model did not return a valid JSON object containing tool specification.")


class ToolCreator:
    """Synthesizes, verifies, repairs, and persists custom tools using the flagship model (deepseek-chat)."""

    def __init__(self, cfg, registry, library, log=print):
        self.cfg = cfg
        self.reg = registry
        self.lib = library
        self.log = log

    def resolve_creator_model(self) -> tuple[str, str, str, str]:
        """Always prefer the configured highest-capability model (default: deepseek-chat / v4)."""
        tc = self.cfg.get("tool_creator", {})
        oc = self.cfg["orchestrator"]
        raw_model = tc.get("model") or "v4"
        raw_ep = tc.get("endpoint") or oc["endpoint"]

        aliases = self.cfg.get("models", {})
        target = aliases.get(raw_model) or aliases.get(raw_model.lower())
        model = raw_model
        endpoint = raw_ep
        if isinstance(target, dict):
            model = target.get("model", raw_model)
            endpoint = tc.get("endpoint") or target.get("endpoint") or raw_ep
        elif isinstance(target, str):
            if "/" in target and target.split("/", 1)[0] in self.cfg["endpoints"]:
                ep, m = target.split("/", 1)
                model = m
                endpoint = tc.get("endpoint") or ep
            else:
                model = target

        # Ensure flagship default resolves to deepseek-chat if alias not found
        if model == "v4":
            model = "deepseek-chat"
        ep_cfg = self.cfg["endpoints"][endpoint]
        return model, endpoint, ep_cfg["base_url"], resolve_api_key(ep_cfg)

    def create_tool(self, name: str, description: str, requirements: str, created_by: str = "orchestrator") -> dict:
        clean_name = name.strip().lower()
        if not TOOL_NAME_RE.match(clean_name):
            raise ValueError("Tool name must be lowercase_snake_case, 2-40 chars, starting with a letter.")
        if clean_name in self.lib.builtin_names():
            raise ValueError(f"Cannot overwrite built-in vetted tool '{clean_name}'.")

        model, endpoint, base_url, api_key = self.resolve_creator_model()
        max_attempts = int(self.cfg.get("tool_creator", {}).get("max_repair_attempts", 3))
        self.log(f"[tool_creator:{model}] synthesizing custom tool '{clean_name}'...")

        messages = [
            {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Create a reusable tool named `{clean_name}`.\n"
                    f"Short description: {description}\n"
                    f"Detailed requirements:\n{requirements}\n\n"
                    f"Existing tools already available (do not duplicate unnecessarily): {self.lib.names()}"
                ),
            },
        ]

        last_err = ""
        for attempt in range(1, max_attempts + 1):
            content, _, _, _ = chat(base_url, model, messages, tools=None, temperature=0.1, api_key=api_key)
            messages.append({"role": "assistant", "content": content})

            try:
                spec = _extract_json_obj(content)
            except ValueError as e:
                last_err = str(e)
                self.log(f"[tool_creator:{model}] attempt {attempt} JSON parse error: {last_err}")
                messages.append({"role": "user", "content": f"ERROR: {last_err}. Reply with ONLY valid JSON."})
                continue

            desc = str(spec.get("description") or description).strip()
            params = spec.get("parameters")
            code = str(spec.get("code") or "").strip()
            tests = str(spec.get("tests") or "").strip()

            if not isinstance(params, dict) or params.get("type") != "object":
                last_err = "parameters must be a JSON Schema object with type='object'"
            elif not code:
                last_err = "code is empty"
            elif not tests:
                last_err = "tests is empty"
            else:
                last_err = check_ast_safety(code, label="code") or check_ast_safety(tests, label="tests") or ""

            if last_err:
                self.log(f"[tool_creator:{model}] attempt {attempt} safety/schema rejected: {last_err}")
                messages.append({
                    "role": "user",
                    "content": f"Validation failed: {last_err}. Fix the tool and return ONLY the corrected JSON.",
                })
                continue

            ok, test_out = run_tool_tests(code, tests)
            if not ok:
                last_err = f"Unit tests failed:\n{test_out}"
                self.log(f"[tool_creator:{model}] attempt {attempt} tests failed; repairing...")
                messages.append({
                    "role": "user",
                    "content": f"{last_err}\n\nFix the implementation/tests and return ONLY the corrected JSON.",
                })
                continue

            # Persist in SQLite + custom_tools directory & hot-register in ToolLibrary
            saved = self.reg.save_custom_tool(
                name=clean_name,
                description=desc,
                schema=params,
                code=code,
                tests=tests,
                created_by_model=f"{model} @ {endpoint}",
                created_by_user=created_by,
            )
            self.lib.register_custom_tool(saved)
            self.log(f"[tool_creator:{model}] verified & saved tool '{clean_name}' (attempt {attempt})")
            return saved

        raise RuntimeError(f"Failed to synthesize verified tool '{clean_name}' after {max_attempts} attempts: {last_err}")
