"""Scripted fake OpenAI-compatible server for testing agent-hive without a GPU.

Usage: python tests/mock_server.py 18080
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def call(name, args, native=True):
    if native:
        return {"role": "assistant", "content": "",
                "tool_calls": [{"id": f"c_{name}", "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)}}]}
    # simulate a small model writing the call as plain text
    return {"role": "assistant", "content": json.dumps({"name": name, "arguments": args})}


def say(text):
    return {"role": "assistant", "content": f"<think>hidden reasoning</think>{text}"}


def orchestrator(msgs):
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    n = len(tool_msgs)
    if n == 0:
        return call("list_agents", {})
    if n == 1:
        # first attempt has a bad tool name -> should get an error back
        return call("create_agent", {"name": "file_writer", "description": "Writes files",
                                     "system_prompt": "You write files.", "tools": ["teleport"]})
    if n == 2:
        return call("create_agent", {"name": "file_writer", "description": "Writes files",
                                     "system_prompt": "You write files the user asks for.",
                                     "tools": ["write_file", "list_files"], "extra": "ignored"})
    if n == 3:
        return call("assign_task", {"agent": "file_writer",
                                    "task": "Write 'hello from the hive' to hello.txt"})
    return say("Done: file_writer created hello.txt. Result: " + tool_msgs[-1]["content"][:80])


def worker(msgs):
    n = sum(m["role"] == "tool" for m in msgs)
    if n == 0:
        return call("write_file", {"path": "hello.txt", "content": "hello from the hive"},
                    native=False)
    if n == 1:
        return call("write_file", {"path": "../escape.txt", "content": "x"})  # must be blocked
    return say("Wrote hello.txt; refused to write outside the workspace.")


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        sys_prompt = body["messages"][0].get("content", "") if body.get("messages") else ""
        if isinstance(sys_prompt, list):
            sys_prompt = " ".join(p.get("text", "") for p in sys_prompt if isinstance(p, dict))
        is_orch = "You are the orchestrator" in sys_prompt or body["model"] == "qwen3:8b"
        msg = orchestrator(body["messages"]) if is_orch else worker(body["messages"])
        out = json.dumps({"choices": [{"message": msg}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
