"""Lightweight stdlib HTTP JSON API server for deploying agent-hive in Docker/cloud."""
import hmac
import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .llm import LLMError

MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB


def serve(orch, host="0.0.0.0", port=8000, api_token=None):
    """Run a zero-dependency HTTP server exposing the orchestrator and registry."""
    token = (api_token if api_token is not None else os.environ.get("HIVE_API_TOKEN", "")).strip()
    op_lock = threading.Lock()
    tls = threading.local()

    orig_log = orch.log

    def _capture_log(msg):
        buf = getattr(tls, "logs", None)
        if buf is not None:
            buf.append(str(msg))
        orig_log(msg)

    orch.log = _capture_log

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, payload):
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authorized(self):
            if not token:
                return True
            auth = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if not auth.startswith(prefix):
                return False
            return hmac.compare_digest(auth[len(prefix):].strip(), token)

        def _read_json(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return None, "invalid Content-Length header"
            if length < 0 or length > MAX_BODY_BYTES:
                return None, f"request body too large (max {MAX_BODY_BYTES} bytes)"
            if length == 0:
                return {}, None
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                return None, f"invalid JSON: {e}"
            if not isinstance(data, dict):
                return None, "request body must be a JSON object"
            return data, None

        def _model_summary(self):
            oc = orch.cfg["orchestrator"]
            w = orch.cfg["workers"]
            om, oep = orch.resolve_model(oc["model"], oc["endpoint"])
            wm, wep = orch.resolve_model(w["model"], w["endpoint"])
            return {
                "orchestrator": {"configured": oc["model"], "model": om, "endpoint": oep},
                "workers_default": {"configured": w["model"], "model": wm, "endpoint": wep},
                "aliases": orch.cfg.get("models", {}),
                "agents": {a.name: {"model": a.model, "endpoint": a.endpoint}
                           for a in orch.reg.list_agents()},
            }

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path == "/health":
                return self._send(200, {"status": "ok"})
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})

            if path == "/agents":
                agents = [a.__dict__ for a in orch.reg.list_agents()]
                return self._send(200, {"agents": agents})
            if path.startswith("/agents/"):
                name = unquote(path[len("/agents/"):])
                a = orch.reg.get_agent(name)
                if not a:
                    return self._send(404, {"error": f"no agent named '{name}'"})
                return self._send(200, a.__dict__)
            if path == "/tasks":
                qs = parse_qs(parsed.query)
                try:
                    limit = max(1, min(int(qs.get("limit", ["10"])[0]), 100))
                except ValueError:
                    limit = 10
                return self._send(200, {"tasks": orch.reg.recent_tasks(limit)})
            if path == "/model":
                with op_lock:
                    return self._send(200, self._model_summary())

            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            body, err = self._read_json()
            if err:
                return self._send(400, {"error": err})

            if path == "/chat":
                msg = str(body.get("message", "")).strip()
                if not msg:
                    return self._send(400, {"error": "missing 'message'"})
                tls.logs = []
                try:
                    with op_lock:
                        reply = orch.handle(msg)
                except LLMError as e:
                    return self._send(502, {"error": str(e), "logs": tls.logs})
                finally:
                    logs = tls.logs
                    tls.logs = None
                return self._send(200, {"reply": reply, "logs": logs})

            if path == "/run":
                agent = str(body.get("agent", "")).strip()
                task = str(body.get("task", "")).strip()
                if not agent or not task:
                    return self._send(400, {"error": "missing 'agent' or 'task'"})
                model_ov = str(body.get("model", "") or "")
                ep_ov = str(body.get("endpoint", "") or "")
                tls.logs = []
                try:
                    with op_lock:
                        result = orch.run_worker(agent, task, model_override=model_ov,
                                                 endpoint_override=ep_ov)
                except LLMError as e:
                    return self._send(502, {"error": str(e), "logs": tls.logs})
                finally:
                    logs = tls.logs
                    tls.logs = None
                return self._send(200, {"result": result, "logs": logs})

            if path == "/model":
                target = str(body.get("target", "")).strip()
                new_m = str(body.get("model", "")).strip()
                new_ep = str(body.get("endpoint", "")).strip() or None
                if not target or not new_m:
                    return self._send(400, {"error": "missing 'target' or 'model'"})
                with op_lock:
                    rm, rep = orch.resolve_model(new_m, new_ep)
                    if rep and rep not in orch.cfg["endpoints"]:
                        return self._send(400, {"error": f"unknown endpoint '{rep}'"})
                    if target in ("orch", "orchestrator"):
                        orch.cfg["orchestrator"]["model"] = rm
                        if rep:
                            orch.cfg["orchestrator"]["endpoint"] = rep
                    elif target in ("workers", "worker", "child", "children"):
                        orch.cfg["workers"]["model"] = rm
                        if rep:
                            orch.cfg["workers"]["endpoint"] = rep
                    else:
                        a = orch.reg.get_agent(target)
                        if not a:
                            return self._send(404, {"error": f"no agent named '{target}'"})
                        a.model = rm
                        if rep:
                            a.endpoint = rep
                        orch.reg.save_agent(a)
                    return self._send(200, self._model_summary())

            if path == "/reset":
                with op_lock:
                    orch.history.clear()
                return self._send(200, {"status": "cleared"})

            return self._send(404, {"error": "not found"})

        def do_DELETE(self):
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path.startswith("/agents/"):
                name = unquote(path[len("/agents/"):])
                deleted = orch.reg.delete_agent(name)
                if not deleted:
                    return self._send(404, {"error": f"no agent named '{name}'"})
                return self._send(200, {"deleted": True, "name": name})
            return self._send(404, {"error": "not found"})

        def log_message(self, fmt, *args):
            orig_log(f"[http] {self.address_string()} - {fmt % args}")

    httpd = ThreadingHTTPServer((host, port), Handler)

    def _shutdown(*_args):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _shutdown)
        except ValueError:
            pass

    auth_note = "enabled" if token else "disabled (set HIVE_API_TOKEN to enable)"
    print(f"agent-hive HTTP server listening on http://{host}:{port} (auth: {auth_note})", flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        print("agent-hive HTTP server stopped.", flush=True)
