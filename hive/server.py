"""FastAPI REST server, OpenAPI docs, interactive dashboard, and API key distribution."""
import hmac
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .llm import LLMError
from .orchestrator import NAME_RE
from .registry import AgentSpec, VALID_SCOPES

DASHBOARD_PATH = Path(__file__).resolve().parent / "dashboard.html"


# --- Pydantic Schemas --------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="Prompt for the orchestrator agent")
    reset_before: bool = Field(False, description="Clear orchestrator memory before handling")


class ChatResponse(BaseModel):
    reply: str
    logs: list[str] = Field(default_factory=list)


class AgentCreateRequest(BaseModel):
    name: str = Field(..., description="lowercase_snake_case identifier (2-40 chars)")
    description: str = Field(..., min_length=1, description="One-line summary of the agent")
    system_prompt: str = Field(..., min_length=1, description="System prompt for the worker")
    tools: list[str] = Field(default_factory=list, description="Subset of available tool names")
    model: str = Field("", description="Optional model ID or alias (defaults to workers.model)")
    endpoint: str = Field("", description="Optional endpoint name (defaults to workers.endpoint)")


class AgentUpdateRequest(BaseModel):
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    tools: Optional[list[str]] = None
    model: Optional[str] = None
    endpoint: Optional[str] = None


class RunWorkerRequest(BaseModel):
    agent: str = Field(..., min_length=1, description="Registered worker agent name")
    task: str = Field(..., min_length=1, description="Self-contained task instructions")
    model: str = Field("", description="Optional one-off model override")
    endpoint: str = Field("", description="Optional one-off endpoint override")


class RunWorkerResponse(BaseModel):
    result: str
    logs: list[str] = Field(default_factory=list)


class ModelSwitchRequest(BaseModel):
    target: str = Field(..., description="'orch', 'workers', or an existing agent name")
    model: str = Field(..., min_length=1, description="Model alias or raw model ID")
    endpoint: Optional[str] = Field(None, description="Optional endpoint override")


class WorkspaceWriteRequest(BaseModel):
    content: str = Field(..., description="UTF-8 text file contents")


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80, description="Human label (e.g. 'frontend-app')")
    scopes: list[Literal["read", "write", "admin"]] = Field(
        default=["read", "write"],
        description="Permission scopes granted to this key",
    )
    expires_in_days: Optional[float] = Field(
        None, gt=0, description="Optional expiration in days (null = never expires)"
    )


# --- Helpers -----------------------------------------------------------------

def _build_distribution_kit(base_url: str, raw_key: str, key_meta: dict) -> dict:
    """Build copy-ready client snippets and onboarding bundle for easy API distribution."""
    base = base_url.rstrip("/")
    curl_chat = (
        f'curl -X POST "{base}/api/v1/chat" \\\n'
        f'  -H "Authorization: Bearer {raw_key}" \\\n'
        f'  -H "Content-Type: application/json" \\\n'
        f'  -d \'{{"message": "List your agents"}}\''
    )
    python_snippet = (
        "import requests\n\n"
        f'BASE_URL = "{base}"\n'
        f'API_KEY = "{raw_key}"\n\n'
        'resp = requests.post(\n'
        '    f"{BASE_URL}/api/v1/chat",\n'
        '    headers={"Authorization": f"Bearer {API_KEY}"},\n'
        '    json={"message": "List your agents"},\n'
        '    timeout=120,\n'
        ')\n'
        'print(resp.json())\n'
    )
    js_snippet = (
        f'const res = await fetch("{base}/api/v1/chat", {{\n'
        '  method: "POST",\n'
        '  headers: {\n'
        f'    "Authorization": "Bearer {raw_key}",\n'
        '    "Content-Type": "application/json",\n'
        '  },\n'
        '  body: JSON.stringify({ message: "List your agents" }),\n'
        '});\n'
        'console.log(await res.json());\n'
    )
    env_snippet = f"AGENT_HIVE_URL={base}\nAGENT_HIVE_API_KEY={raw_key}\n"
    markdown_invite = (
        f"## agent-hive API Access — `{key_meta['name']}`\n\n"
        f"- **Base URL**: `{base}`\n"
        f"- **API Key**: `{raw_key}`\n"
        f"- **Scopes**: `{', '.join(key_meta['scopes'])}`\n"
        f"- **Interactive Docs**: `{base}/docs`\n"
        f"- **Dashboard**: `{base}/`\n"
        f"- **OpenAPI Schema**: `{base}/openapi.json`\n\n"
        f"### Quick Test (`curl`)\n```bash\n{curl_chat}\n```\n"
    )
    return {
        "base_url": base,
        "docs_url": f"{base}/docs",
        "redoc_url": f"{base}/redoc",
        "openapi_url": f"{base}/openapi.json",
        "auth_header": f"Authorization: Bearer {raw_key}",
        "snippets": {
            "curl": curl_chat,
            "python": python_snippet,
            "javascript": js_snippet,
            "env": env_snippet,
            "markdown": markdown_invite,
        },
    }


def _extract_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


# --- App Factory -------------------------------------------------------------

def create_app(orch, api_token: Optional[str] = None) -> FastAPI:
    master_token = (
        api_token if api_token is not None else os.environ.get("HIVE_API_TOKEN", "")
    ).strip()
    started_at = time.time()
    op_lock = threading.Lock()
    tls = threading.local()

    orig_log = orch.log

    def _capture_log(msg):
        buf = getattr(tls, "logs", None)
        if buf is not None:
            buf.append(str(msg))
        orig_log(msg)

    orch.log = _capture_log

    app = FastAPI(
        title="agent-hive API",
        version="1.0.0",
        description=(
            "Multi-model AI agent orchestrator REST API with worker management, "
            "live model routing, workspace file access, and built-in API key distribution."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    cors_origins = [
        o.strip() for o in os.environ.get("HIVE_CORS_ORIGINS", "*").split(",") if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- Auth & Scope Dependencies ------------------------------------------

    def _auth_enabled() -> bool:
        return bool(master_token) or orch.reg.has_active_api_keys()

    def require_scope(scope: str):
        def _dep(request: Request) -> dict:
            if not _auth_enabled():
                return {"id": "open", "name": "open-mode", "scopes": list(VALID_SCOPES), "master": True}
            provided = _extract_token(request)
            if not provided:
                raise HTTPException(
                    status_code=401,
                    detail="Missing API key. Pass 'Authorization: Bearer <key>' or 'X-API-Key: <key>'.",
                )
            if master_token and hmac.compare_digest(provided, master_token):
                return {"id": "master", "name": "HIVE_API_TOKEN", "scopes": list(VALID_SCOPES), "master": True}
            key_info = orch.reg.verify_api_key(provided)
            if not key_info:
                raise HTTPException(status_code=401, detail="Invalid, expired, or revoked API key.")
            scopes = key_info["scopes"]
            if "admin" not in scopes and scope not in scopes:
                raise HTTPException(
                    status_code=403,
                    detail=f"API key lacks required '{scope}' scope (has: {scopes}).",
                )
            return {**key_info, "master": False}

        return _dep

    def _model_summary() -> dict:
        oc = orch.cfg["orchestrator"]
        w = orch.cfg["workers"]
        om, oep = orch.resolve_model(oc["model"], oc["endpoint"])
        wm, wep = orch.resolve_model(w["model"], w["endpoint"])
        return {
            "orchestrator": {"configured": oc["model"], "model": om, "endpoint": oep},
            "workers_default": {"configured": w["model"], "model": wm, "endpoint": wep},
            "aliases": orch.cfg.get("models", {}),
            "endpoints": list(orch.cfg.get("endpoints", {}).keys()),
            "agents": {
                a.name: {"model": a.model, "endpoint": a.endpoint}
                for a in orch.reg.list_agents()
            },
        }

    # --- Dashboard & Health --------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard():
        if DASHBOARD_PATH.is_file():
            return HTMLResponse(DASHBOARD_PATH.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>agent-hive</h1><p>Visit <a href='/docs'>/docs</a>.</p>")

    @app.get("/health", tags=["System"], summary="Health check and runtime summary")
    def health():
        oc = orch.cfg["orchestrator"]
        w = orch.cfg["workers"]
        om, oep = orch.resolve_model(oc["model"], oc["endpoint"])
        wm, wep = orch.resolve_model(w["model"], w["endpoint"])
        return {
            "status": "ok",
            "version": app.version,
            "uptime_seconds": round(time.time() - started_at, 1),
            "auth_enabled": _auth_enabled(),
            "master_token_configured": bool(master_token),
            "counts": {
                "agents": len(orch.reg.list_agents()),
                "tasks": orch.reg.task_count(),
                "tools": len(orch.lib.tools),
                "api_keys": len(orch.reg.list_api_keys()),
            },
            "models": {
                "orchestrator": f"{om} @ {oep}",
                "workers_default": f"{wm} @ {wep}",
            },
        }

    # --- Chat / Orchestrator -------------------------------------------------

    @app.post(
        "/api/v1/chat",
        response_model=ChatResponse,
        tags=["Orchestrator"],
        summary="Send a prompt to the orchestrator agent",
    )
    @app.post("/chat", response_model=ChatResponse, include_in_schema=False)
    def chat(req: ChatRequest, _caller=Depends(require_scope("write"))):
        tls.logs = []
        try:
            with op_lock:
                if req.reset_before:
                    orch.history.clear()
                reply = orch.handle(req.message)
        except LLMError as e:
            return JSONResponse(status_code=502, content={"detail": str(e), "logs": tls.logs})
        finally:
            logs = tls.logs
            tls.logs = None
        return ChatResponse(reply=reply, logs=logs)

    @app.get(
        "/api/v1/chat/history",
        tags=["Orchestrator"],
        summary="Get orchestrator conversation memory",
    )
    def chat_history(_caller=Depends(require_scope("read"))):
        with op_lock:
            exchanges = [{"user": u, "assistant": a} for u, a in orch.history]
        return {
            "turns": len(exchanges),
            "max_turns": orch.cfg["runtime"].get("history_turns", 6),
            "history": exchanges,
        }

    @app.post(
        "/api/v1/chat/reset",
        tags=["Orchestrator"],
        summary="Clear orchestrator conversation memory",
    )
    @app.post("/reset", include_in_schema=False)
    def chat_reset(_caller=Depends(require_scope("write"))):
        with op_lock:
            orch.history.clear()
        return {"status": "cleared"}

    # --- Agents CRUD ---------------------------------------------------------

    @app.get("/api/v1/agents", tags=["Agents"], summary="List all worker agents")
    @app.get("/agents", include_in_schema=False)
    def list_agents(_caller=Depends(require_scope("read"))):
        return {"agents": [a.__dict__ for a in orch.reg.list_agents()]}

    @app.post(
        "/api/v1/agents",
        status_code=201,
        tags=["Agents"],
        summary="Create or overwrite a worker agent",
    )
    def create_agent(req: AgentCreateRequest, _caller=Depends(require_scope("write"))):
        name = req.name.strip().lower()
        if not NAME_RE.match(name):
            raise HTTPException(
                status_code=400,
                detail="name must be lowercase_snake_case, 2-40 chars, starting with a letter",
            )
        bad = [t for t in req.tools if t not in orch.lib.names()]
        if bad:
            raise HTTPException(
                status_code=400,
                detail=f"unknown tools {bad}; allowed: {orch.lib.names()}",
            )
        w = orch.cfg["workers"]
        resolved_model, resolved_ep = orch.resolve_model(
            req.model or w["model"], req.endpoint or None
        )
        endpoint = resolved_ep or w["endpoint"]
        if endpoint not in orch.cfg["endpoints"]:
            raise HTTPException(
                status_code=400,
                detail=f"unknown endpoint '{endpoint}'; allowed: {list(orch.cfg['endpoints'])}",
            )
        spec = AgentSpec(
            name=name,
            description=req.description.strip(),
            system_prompt=req.system_prompt.strip(),
            tools=list(dict.fromkeys(req.tools)),
            endpoint=endpoint,
            model=resolved_model,
        )
        orch.reg.save_agent(spec)
        return {"agent": spec.__dict__}

    @app.get("/api/v1/agents/{name}", tags=["Agents"], summary="Get a worker agent's full spec")
    @app.get("/agents/{name}", include_in_schema=False)
    def get_agent(name: str, _caller=Depends(require_scope("read"))):
        a = orch.reg.get_agent(name)
        if not a:
            raise HTTPException(status_code=404, detail=f"no agent named '{name}'")
        return a.__dict__

    @app.patch("/api/v1/agents/{name}", tags=["Agents"], summary="Update an existing worker agent")
    def update_agent(name: str, req: AgentUpdateRequest, _caller=Depends(require_scope("write"))):
        a = orch.reg.get_agent(name)
        if not a:
            raise HTTPException(status_code=404, detail=f"no agent named '{name}'")
        if req.description is not None:
            a.description = req.description.strip()
        if req.system_prompt is not None:
            a.system_prompt = req.system_prompt.strip()
        if req.tools is not None:
            bad = [t for t in req.tools if t not in orch.lib.names()]
            if bad:
                raise HTTPException(
                    status_code=400, detail=f"unknown tools {bad}; allowed: {orch.lib.names()}"
                )
            a.tools = list(dict.fromkeys(req.tools))
        if req.model is not None or req.endpoint is not None:
            rm, rep = orch.resolve_model(req.model or a.model, req.endpoint or a.endpoint)
            ep = rep or a.endpoint
            if ep not in orch.cfg["endpoints"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown endpoint '{ep}'; allowed: {list(orch.cfg['endpoints'])}",
                )
            a.model = rm
            a.endpoint = ep
        orch.reg.save_agent(a)
        return {"agent": a.__dict__}

    @app.delete("/api/v1/agents/{name}", tags=["Agents"], summary="Delete a worker agent")
    @app.delete("/agents/{name}", include_in_schema=False)
    def delete_agent(name: str, _caller=Depends(require_scope("admin"))):
        if not orch.reg.delete_agent(name):
            raise HTTPException(status_code=404, detail=f"no agent named '{name}'")
        return {"deleted": True, "name": name}

    # --- Direct Worker Run & Tasks -------------------------------------------

    @app.post(
        "/api/v1/run",
        response_model=RunWorkerResponse,
        tags=["Tasks"],
        summary="Run a worker agent directly (skips orchestrator)",
    )
    @app.post("/run", response_model=RunWorkerResponse, include_in_schema=False)
    def run_worker(req: RunWorkerRequest, _caller=Depends(require_scope("write"))):
        tls.logs = []
        try:
            with op_lock:
                result = orch.run_worker(
                    req.agent,
                    req.task,
                    model_override=req.model,
                    endpoint_override=req.endpoint,
                )
        except LLMError as e:
            return JSONResponse(status_code=502, content={"detail": str(e), "logs": tls.logs})
        finally:
            logs = tls.logs
            tls.logs = None
        return RunWorkerResponse(result=result, logs=logs)

    @app.get("/api/v1/tasks", tags=["Tasks"], summary="List recent tasks with optional filters")
    @app.get("/tasks", include_in_schema=False)
    def list_tasks(
        limit: int = Query(20, ge=1, le=200),
        agent: Optional[str] = Query(None, description="Filter by worker agent name"),
        status: Optional[str] = Query(None, description="Filter by status (running, done, failed)"),
        _caller=Depends(require_scope("read")),
    ):
        return {"tasks": orch.reg.list_tasks(limit=limit, agent=agent, status=status)}

    @app.get("/api/v1/tasks/{task_id}", tags=["Tasks"], summary="Get a single task by ID")
    def get_task(task_id: int, _caller=Depends(require_scope("read"))):
        t = orch.reg.get_task(task_id)
        if not t:
            raise HTTPException(status_code=404, detail=f"no task #{task_id}")
        return t

    # --- Models & Tools ------------------------------------------------------

    @app.get(
        "/api/v1/models",
        tags=["Models & Tools"],
        summary="Show orchestrator, default worker, aliases, endpoints, and per-agent models",
    )
    @app.get("/model", include_in_schema=False)
    def get_models(_caller=Depends(require_scope("read"))):
        with op_lock:
            return _model_summary()

    @app.post(
        "/api/v1/models",
        tags=["Models & Tools"],
        summary="Switch orchestrator, default workers, or a specific agent's model live",
    )
    @app.post("/model", include_in_schema=False)
    def switch_model(req: ModelSwitchRequest, _caller=Depends(require_scope("admin"))):
        with op_lock:
            rm, rep = orch.resolve_model(req.model, req.endpoint)
            if rep and rep not in orch.cfg["endpoints"]:
                raise HTTPException(status_code=400, detail=f"unknown endpoint '{rep}'")
            target = req.target.strip()
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
                    raise HTTPException(status_code=404, detail=f"no agent named '{target}'")
                a.model = rm
                if rep:
                    a.endpoint = rep
                orch.reg.save_agent(a)
            return _model_summary()

    @app.get(
        "/api/v1/tools",
        tags=["Models & Tools"],
        summary="List all available worker tools and their JSON schemas",
    )
    def list_tools(_caller=Depends(require_scope("read"))):
        return {
            "tools": [
                {"name": name, "description": desc, "parameters": schema}
                for name, (desc, schema, _) in orch.lib.tools.items()
            ]
        }

    # --- Workspace Files -----------------------------------------------------

    @app.get("/api/v1/workspace/files", tags=["Workspace"], summary="List all files in workspace/")
    def list_workspace_files(_caller=Depends(require_scope("read"))):
        files = []
        for p in sorted(orch.lib.ws.rglob("*")):
            if p.is_file():
                st = p.stat()
                files.append({
                    "path": p.relative_to(orch.lib.ws).as_posix(),
                    "size_bytes": st.st_size,
                    "modified_at": st.st_mtime,
                })
        return {"files": files}

    @app.get(
        "/api/v1/workspace/files/{file_path:path}",
        tags=["Workspace"],
        summary="Read a text file from workspace/",
    )
    def read_workspace_file(file_path: str, _caller=Depends(require_scope("read"))):
        try:
            p = orch.lib._path(file_path)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"file not found: {file_path}")
        return {
            "path": p.relative_to(orch.lib.ws).as_posix(),
            "content": p.read_text(encoding="utf-8", errors="replace"),
        }

    @app.put(
        "/api/v1/workspace/files/{file_path:path}",
        tags=["Workspace"],
        summary="Create or overwrite a text file in workspace/",
    )
    def write_workspace_file(
        file_path: str,
        req: WorkspaceWriteRequest,
        _caller=Depends(require_scope("write")),
    ):
        try:
            p = orch.lib._path(file_path)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(req.content, encoding="utf-8")
        return {
            "path": p.relative_to(orch.lib.ws).as_posix(),
            "bytes": len(req.content.encode("utf-8")),
        }

    @app.delete(
        "/api/v1/workspace/files/{file_path:path}",
        tags=["Workspace"],
        summary="Delete a file from workspace/",
    )
    def delete_workspace_file(file_path: str, _caller=Depends(require_scope("admin"))):
        try:
            p = orch.lib._path(file_path)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"file not found: {file_path}")
        p.unlink()
        return {"deleted": True, "path": file_path}

    # --- API Key Generation & Distribution -----------------------------------

    @app.get(
        "/api/v1/keys",
        tags=["API Keys"],
        summary="List all generated API keys (metadata & usage only)",
    )
    def list_keys(_caller=Depends(require_scope("admin"))):
        return {
            "auth_enabled": _auth_enabled(),
            "master_token_configured": bool(master_token),
            "keys": orch.reg.list_api_keys(),
        }

    @app.post(
        "/api/v1/keys",
        status_code=201,
        tags=["API Keys"],
        summary="Generate a new API key and ready-to-distribute client kit",
    )
    def create_key(
        req: ApiKeyCreateRequest,
        request: Request,
        _caller=Depends(require_scope("admin")),
    ):
        try:
            raw_key, meta = orch.reg.create_api_key(
                req.name, scopes=req.scopes, expires_in_days=req.expires_in_days
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        base_url = str(request.base_url).rstrip("/")
        return {
            "api_key": raw_key,
            "warning": "Store or distribute this key now — it cannot be retrieved again.",
            "key": meta,
            "distribution": _build_distribution_kit(base_url, raw_key, meta),
        }

    @app.post(
        "/api/v1/keys/{key_id}/rotate",
        tags=["API Keys"],
        summary="Rotate an API key (revokes old key and issues a replacement)",
    )
    def rotate_key(key_id: str, request: Request, _caller=Depends(require_scope("admin"))):
        res = orch.reg.rotate_api_key(key_id)
        if not res:
            raise HTTPException(status_code=404, detail=f"no API key '{key_id}'")
        raw_key, meta = res
        base_url = str(request.base_url).rstrip("/")
        return {
            "api_key": raw_key,
            "rotated_from": key_id,
            "warning": "Store or distribute this key now — it cannot be retrieved again.",
            "key": meta,
            "distribution": _build_distribution_kit(base_url, raw_key, meta),
        }

    @app.delete(
        "/api/v1/keys/{key_id}",
        tags=["API Keys"],
        summary="Revoke an API key immediately",
    )
    def revoke_key(key_id: str, _caller=Depends(require_scope("admin"))):
        if not orch.reg.revoke_api_key(key_id):
            existing = orch.reg.get_api_key(key_id)
            if not existing:
                raise HTTPException(status_code=404, detail=f"no API key '{key_id}'")
        return {"revoked": True, "id": key_id}

    return app


def serve(orch, host="0.0.0.0", port=8000, api_token=None):
    """Run the FastAPI server with Uvicorn."""
    import uvicorn

    app = create_app(orch, api_token=api_token)
    auth_note = (
        "enabled"
        if ((api_token if api_token is not None else os.environ.get("HIVE_API_TOKEN", "")).strip()
            or orch.reg.has_active_api_keys())
        else "open (set HIVE_API_TOKEN or generate a key at / to lock down)"
    )
    print(
        f"agent-hive API server listening on http://{host}:{port} "
        f"(dashboard: /, docs: /docs, auth: {auth_note})",
        flush=True,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
