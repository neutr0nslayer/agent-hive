"""FastAPI REST server, Auth/RBAC, file uploads (.md/.txt/.csv), quotas, Admin & Studio GUI."""
import hmac
import json
import os
import queue
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .channels.email_client import (
    EmailConfigError,
    fetch_unread_from_last,
    get_email_config,
    is_email_configured,
    send_email_reply,
)
from .channels.telegram_bot import TelegramBotManager, TelegramError, tg_send_message
from .channels.whatsapp_client import (
    WhatsAppError,
    enable_store_and_restart as wa_enable_store,
    fetch_qr_image,
    fetch_unread_chats as wa_fetch_unread,
    get_session_status as wa_get_status,
    get_whatsapp_config,
    send_whatsapp_message,
    start_session as wa_start_session,
    stop_session as wa_stop_session,
)
from .llm import (
    LLMError,
    emit_stream_event,
    finish_usage_tracking,
    start_stream_emitter,
    start_usage_tracking,
    stop_stream_emitter,
)
from .orchestrator import NAME_RE
from .registry import AgentSpec, SESSION_TTL_SECONDS, VALID_SCOPES
from .scheduler import ALLOWED_INTERVAL_MINUTES, TaskScheduler, compute_next_run, validate_trigger

DASHBOARD_PATH = Path(__file__).resolve().parent / "dashboard.html"
FAVICON_PATH = Path(__file__).resolve().parent / "favicon.svg"
SESSION_COOKIE = "hive_session"
ALLOWED_UPLOAD_EXTS = {".md", ".markdown", ".txt", ".csv"}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MiB per file
MAX_UPLOAD_FILES = 5
INLINE_PREVIEW_CHARS = 4000
SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")
DEFAULT_TOKEN_LIMIT = 100_000


# --- Pydantic Schemas --------------------------------------------------------

class AuthCredentialsRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=1, max_length=128)


class SignupRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=8, max_length=128)


class ResetCredentialsRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)
    new_username: str = Field(..., min_length=3, max_length=32)
    new_password: str = Field(..., min_length=8, max_length=128)


class AdminCreateUserRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=8, max_length=128)
    role: Literal["user", "admin"] = "user"
    token_limit: Optional[int] = Field(None, ge=0)


class AdminUpdateUserRequest(BaseModel):
    status: Optional[Literal["active", "restricted", "revoked"]] = None
    role: Optional[Literal["user", "admin"]] = None
    token_limit: Optional[int] = Field(None, ge=0)
    clear_token_limit: bool = False
    reset_tokens: bool = False
    restriction_reason: Optional[str] = None


class AdminSettingsRequest(BaseModel):
    allow_signup: Optional[bool] = None
    default_user_token_limit: Optional[int] = Field(None, ge=0)
    clear_default_token_limit: bool = False


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="Prompt for the orchestrator agent")
    reset_before: bool = Field(False, description="Clear conversation memory before handling")


class ChatResponse(BaseModel):
    reply: str
    logs: list[str] = Field(default_factory=list)
    files: list[dict] = Field(default_factory=list)
    usage: dict = Field(default_factory=dict)
    user_quota: Optional[dict] = None


class AgentCreateRequest(BaseModel):
    name: str = Field(..., description="lowercase_snake_case identifier (2-40 chars)")
    description: str = Field(..., min_length=1, description="One-line summary of the agent")
    system_prompt: str = Field(..., min_length=1, description="System prompt for the worker")
    tools: list[str] = Field(default_factory=list, description="Subset of available tool names")
    model: str = Field("", description="Optional model ID or alias")
    endpoint: str = Field("", description="Optional endpoint name")


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
    usage: dict = Field(default_factory=dict)
    user_quota: Optional[dict] = None


class ModelSwitchRequest(BaseModel):
    target: str = Field(..., description="'orch', 'workers', or an existing agent name")
    model: str = Field(..., min_length=1, description="Model alias or raw model ID")
    endpoint: Optional[str] = Field(None, description="Optional endpoint override")


class WorkspaceWriteRequest(BaseModel):
    content: str = Field(..., description="UTF-8 text file contents")


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    scopes: list[Literal["read", "write", "admin"]] = Field(default=["read", "write"])
    expires_in_days: Optional[float] = Field(None, gt=0)


class ScheduleCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80, description="Short schedule name")
    target_type: Literal["orchestrator", "worker"] = "orchestrator"
    agent_name: str = Field("", description="Required when target_type='worker'")
    prompt: str = Field(..., min_length=1, description="Task prompt to execute on schedule")
    trigger_type: Literal["interval", "daily"] = "interval"
    interval_minutes: Optional[int] = Field(
        None, description="Allowed: 5, 10, 15, 20, 30, 45, 60, 240, 480, 720, 960, 1200, 1440"
    )
    daily_time: Optional[str] = Field(None, description="24-hour HH:MM time of day (e.g. '09:00')")
    days_of_week: Optional[list[int]] = Field(None, description="0=Sun..6=Sat (defaults to all days)")
    timezone_offset_minutes: int = Field(0, description="User UTC offset in minutes (e.g. +360 for UTC+6)")
    enabled: bool = True
    notify_telegram: bool = False
    notify_whatsapp: bool = False


class ScheduleUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    target_type: Optional[Literal["orchestrator", "worker"]] = None
    agent_name: Optional[str] = None
    prompt: Optional[str] = Field(None, min_length=1)
    trigger_type: Optional[Literal["interval", "daily"]] = None
    interval_minutes: Optional[int] = None
    daily_time: Optional[str] = None
    days_of_week: Optional[list[int]] = None
    timezone_offset_minutes: Optional[int] = None
    enabled: Optional[bool] = None
    notify_telegram: Optional[bool] = None
    notify_whatsapp: Optional[bool] = None


class UserChannelsUpdateRequest(BaseModel):
    telegram_bot_token: Optional[str] = None
    clear_telegram_token: bool = False
    telegram_chat_id: Optional[str] = None
    telegram_enabled: Optional[bool] = None
    regenerate_link_code: bool = False
    whatsapp_target: Optional[str] = None
    whatsapp_enabled: Optional[bool] = None


class ToolCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=40, description="lowercase_snake_case tool name")
    description: str = Field(..., min_length=3, max_length=240)
    requirements: str = Field(..., min_length=5, description="Detailed behavior, inputs, and output spec")


class ToolTestRequest(BaseModel):
    arguments: dict = Field(default_factory=dict)


class EmailConfigUpdateRequest(BaseModel):
    imap_host: str = ""
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 587
    user: str = ""
    password: Optional[str] = None
    folder: str = "INBOX"


class EmailSummarizeRequest(BaseModel):
    scan_last: int = Field(100, ge=1, le=500)


class EmailBatchDraftRequest(BaseModel):
    scan_last: int = Field(100, ge=1, le=500)
    instructions: str = Field("", max_length=500)
    max_drafts: int = Field(15, ge=1, le=40)


class DraftUpdateRequest(BaseModel):
    draft_subject: Optional[str] = None
    draft_reply: Optional[str] = None
    status: Optional[Literal["pending", "dismissed"]] = None


class BatchSendDraftsRequest(BaseModel):
    draft_ids: list[str] = Field(..., min_length=1)


class WhatsAppManualDraftRequest(BaseModel):
    sender_name: str = Field(..., min_length=1)
    chat_id: str = Field(..., min_length=3, description="Phone number or WA chatId (e.g. 15551234567@c.us)")
    incoming_text: str = Field(..., min_length=1)
    instructions: str = ""


class EmailSingleDraftRequest(BaseModel):
    uid: str = ""
    message_id: str = ""
    sender_name: str = ""
    sender_email: str = Field(..., min_length=3)
    subject: str = "(no subject)"
    body: str = ""
    instructions: str = ""


# --- Helpers -----------------------------------------------------------------

def _build_distribution_kit(base_url: str, raw_key: str, key_meta: dict) -> dict:
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
        f"- **Studio GUI**: `{base}/`\n"
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


def _extract_bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _set_session_cookie(response: Response, token: str):
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _apply_persisted_model_settings(orch):
    """Restore admin model/endpoint overrides saved in SQLite across container restarts."""
    for section, key_prefix in (("orchestrator", "orchestrator"), ("workers", "workers")):
        saved_m = orch.reg.get_setting(f"{key_prefix}_model")
        saved_ep = orch.reg.get_setting(f"{key_prefix}_endpoint")
        if saved_m:
            orch.cfg[section]["model"] = saved_m
        if saved_ep and saved_ep in orch.cfg.get("endpoints", {}):
            orch.cfg[section]["endpoint"] = saved_ep


def _ensure_initial_settings_and_admin(orch):
    if orch.reg.get_setting("allow_signup") is None:
        orch.reg.set_setting("allow_signup", True)
    if orch.reg.get_setting("default_user_token_limit") is None:
        orch.reg.set_setting("default_user_token_limit", DEFAULT_TOKEN_LIMIT)
    _apply_persisted_model_settings(orch)

    creds = orch.reg.ensure_default_admin()
    if creds:
        username, password = creds
        banner = (
            "\n"
            "========================================================================\n"
            "  agent-hive DEFAULT ADMIN CREDENTIALS (RESET REQUIRED ON FIRST LOGIN)\n"
            f"  Username : {username}\n"
            f"  Password : {password}\n"
            "========================================================================\n"
        )
        print(banner, flush=True)
    else:
        print("agent-hive: persisted admin account loaded from database.", flush=True)


# --- App Factory -------------------------------------------------------------

def create_app(orch, api_token: Optional[str] = None) -> FastAPI:
    master_token = (
        api_token if api_token is not None else os.environ.get("HIVE_API_TOKEN", "")
    ).strip()
    started_at = time.time()
    op_lock = threading.Lock()
    tls = threading.local()

    _ensure_initial_settings_and_admin(orch)

    orig_log = orch.log

    def _capture_log(msg):
        s = str(msg)
        buf = getattr(tls, "logs", None)
        if buf is not None:
            buf.append(s)
        emit_stream_event({"type": "trace", "line": s, "ts": time.time()})
        orig_log(msg)

    orch.log = _capture_log
    scheduler = TaskScheduler(orch, op_lock, tls)
    tg_manager = TelegramBotManager(orch, scheduler, op_lock, tls)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        scheduler.start()
        tg_manager.start_all()
        try:
            yield
        finally:
            tg_manager.stop_all()
            scheduler.stop()

    app = FastAPI(
        title="agent-hive Studio & API",
        version="2.0.0",
        description=(
            "Multi-model AI agent orchestrator with Studio GUI, user auth & admin controls, "
            "token usage quotas, chat file uploads (.md/.txt/.csv), persistent task scheduler, "
            "and API key distribution."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
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

    # --- Unified Auth & RBAC Dependencies ------------------------------------

    def _resolve_caller(request: Request, allow_pending_reset: bool = False) -> dict:
        # 1. Check Bearer / X-API-Key first (master token, API key, or session token)
        bearer = _extract_bearer(request)
        if bearer:
            if master_token and hmac.compare_digest(bearer, master_token):
                return {
                    "type": "master",
                    "id": "master",
                    "name": "HIVE_API_TOKEN",
                    "scopes": list(VALID_SCOPES),
                    "user": None,
                }
            if bearer.startswith("hive_sess_"):
                user = orch.reg.get_session_user(bearer)
                if user:
                    return _caller_from_user(user, allow_pending_reset)
            key_info = orch.reg.verify_api_key(bearer)
            if key_info:
                return {
                    "type": "api_key",
                    "id": key_info["id"],
                    "name": key_info["name"],
                    "scopes": key_info["scopes"],
                    "user": None,
                }
            raise HTTPException(status_code=401, detail="Invalid, expired, or revoked credentials.")

        # 2. Check HttpOnly session cookie (GUI login)
        sess_cookie = request.cookies.get(SESSION_COOKIE, "")
        if sess_cookie:
            user = orch.reg.get_session_user(sess_cookie)
            if user:
                return _caller_from_user(user, allow_pending_reset)

        raise HTTPException(
            status_code=401,
            detail="Authentication required. Sign in via the GUI or provide an API key.",
        )

    def _caller_from_user(user: dict, allow_pending_reset: bool) -> dict:
        if user["status"] == "revoked":
            raise HTTPException(
                status_code=403,
                detail="Your account has been revoked by an administrator.",
            )
        if user["must_reset_credentials"] and not allow_pending_reset:
            raise HTTPException(
                status_code=403,
                detail="MUST_RESET_CREDENTIALS: You must change your default admin username and password first.",
            )
        if user["role"] == "admin":
            scopes = list(VALID_SCOPES)
        elif user["status"] == "restricted":
            scopes = ["read"]
        else:
            scopes = ["read", "write"]
        return {
            "type": "user",
            "id": user["id"],
            "name": user["username"],
            "scopes": scopes,
            "user": user,
        }

    def require_scope(scope: str):
        def _dep(request: Request) -> dict:
            caller = _resolve_caller(request, allow_pending_reset=False)
            scopes = caller["scopes"]
            if "admin" not in scopes and scope not in scopes:
                u = caller.get("user")
                if u and u["status"] == "restricted":
                    reason = f" Reason: {u['restriction_reason']}" if u["restriction_reason"] else ""
                    raise HTTPException(
                        status_code=403,
                        detail=f"Your account is restricted by an administrator.{reason}",
                    )
                raise HTTPException(
                    status_code=403,
                    detail=f"Insufficient permissions: requires '{scope}' scope.",
                )
            return caller

        return _dep

    def _check_user_quota(caller: dict):
        u = caller.get("user")
        if not u:
            return
        fresh = orch.reg.get_user(u["id"])
        if not fresh:
            raise HTTPException(status_code=401, detail="User account no longer exists.")
        if fresh["status"] == "revoked":
            raise HTTPException(status_code=403, detail="Your account has been revoked.")
        if fresh["status"] == "restricted":
            reason = f" Reason: {fresh['restriction_reason']}" if fresh["restriction_reason"] else ""
            raise HTTPException(status_code=403, detail=f"Your account is restricted.{reason}")
        lim = fresh["token_limit"]
        if lim is not None and fresh["tokens_used"] >= lim:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Token usage limit reached ({fresh['tokens_used']:,} / {lim:,} tokens). "
                    "Contact an administrator to increase or reset your quota."
                ),
            )

    def _record_caller_usage(caller: dict, usage: dict) -> Optional[dict]:
        u = caller.get("user")
        if not u:
            return None
        orch.reg.add_user_tokens(
            u["id"],
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            usage.get("total_tokens", 0),
        )
        fresh = orch.reg.get_user(u["id"])
        return {
            "tokens_used": fresh["tokens_used"],
            "token_limit": fresh["token_limit"],
            "prompt_tokens": fresh["prompt_tokens"],
            "completion_tokens": fresh["completion_tokens"],
        }

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

    async def _save_chat_uploads(files: list[UploadFile], owner_name: str) -> tuple[list[dict], str]:
        """Validate & save ONLY .md, .txt, and .csv uploads into workspace/uploads/<owner>/."""
        if len(files) > MAX_UPLOAD_FILES:
            raise HTTPException(
                status_code=400,
                detail=f"At most {MAX_UPLOAD_FILES} files can be attached per message.",
            )
        saved_meta = []
        prompt_parts = []
        safe_owner = SAFE_NAME_RE.sub("_", owner_name).strip("_") or "api"
        ts = time.strftime("%Y%m%d_%H%M%S")

        for up in files:
            raw_name = Path(up.filename or "upload.txt").name
            ext = Path(raw_name).suffix.lower()
            if ext not in ALLOWED_UPLOAD_EXTS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unsupported file type '{raw_name}'. "
                        "Only Markdown (.md), Text (.txt), and CSV (.csv) files are allowed."
                    ),
                )
            data = await up.read()
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=400,
                    detail=f"File '{raw_name}' exceeds maximum size of {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                )
            if b"\x00" in data:
                raise HTTPException(
                    status_code=400,
                    detail=f"File '{raw_name}' appears to be binary; only UTF-8 text files are allowed.",
                )
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(
                    status_code=400,
                    detail=f"File '{raw_name}' is not valid UTF-8 text.",
                )

            safe_file = SAFE_NAME_RE.sub("_", raw_name).strip("_") or f"file{ext}"
            rel_path = f"uploads/{safe_owner}/{ts}_{safe_file}"
            dest = orch.lib._path(rel_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")

            saved_meta.append({
                "filename": raw_name,
                "workspace_path": rel_path,
                "size_bytes": len(data),
                "ext": ext,
            })
            preview = text[:INLINE_PREVIEW_CHARS]
            trunc_note = ""
            if len(text) > INLINE_PREVIEW_CHARS:
                trunc_note = (
                    f"\n...[truncated {len(text) - INLINE_PREVIEW_CHARS} chars; "
                    f"use read_file('{rel_path}') for full contents]"
                )
            prompt_parts.append(
                f"--- Attached File: `{raw_name}` (saved in workspace at `{rel_path}`, {len(data)} bytes) ---\n"
                f"{preview}{trunc_note}\n--- End of `{raw_name}` ---"
            )

        return saved_meta, "\n\n".join(prompt_parts)

    # --- GUI & System Health -------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def studio_gui():
        if DASHBOARD_PATH.is_file():
            return HTMLResponse(DASHBOARD_PATH.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>agent-hive</h1><p>Visit <a href='/docs'>/docs</a>.</p>")

    @app.get("/favicon.svg", include_in_schema=False)
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        if FAVICON_PATH.is_file():
            return Response(
                content=FAVICON_PATH.read_bytes(),
                media_type="image/svg+xml",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        return Response(status_code=204)

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
            "auth_enabled": True,
            "allow_signup": bool(orch.reg.get_setting("allow_signup", True)),
            "allowed_chat_file_types": sorted(ALLOWED_UPLOAD_EXTS),
            "counts": {
                "agents": len(orch.reg.list_agents()),
                "tasks": orch.reg.task_count(),
                "tools": len(orch.lib.tools),
                "api_keys": len(orch.reg.list_api_keys()),
                "users": len(orch.reg.list_users()),
                "schedules": len(orch.reg.list_schedules()),
            },
            "models": {
                "orchestrator": f"{om} @ {oep}",
                "workers_default": f"{wm} @ {wep}",
            },
        }

    # --- Authentication Endpoints --------------------------------------------

    @app.get("/api/v1/auth/status", tags=["Auth"], summary="Public authentication settings")
    def auth_status():
        return {
            "allow_signup": bool(orch.reg.get_setting("allow_signup", True)),
            "allowed_chat_file_types": sorted(ALLOWED_UPLOAD_EXTS),
        }

    @app.post("/api/v1/auth/signup", status_code=201, tags=["Auth"], summary="Register a new user account")
    def signup(req: SignupRequest, response: Response):
        if not orch.reg.get_setting("allow_signup", True):
            raise HTTPException(status_code=403, detail="Public sign-up is disabled by the administrator.")
        default_limit = orch.reg.get_setting("default_user_token_limit", DEFAULT_TOKEN_LIMIT)
        try:
            user = orch.reg.create_user(req.username, req.password, role="user", token_limit=default_limit)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        orch.reg.mark_user_login(user["id"])
        token = orch.reg.create_session(user["id"])
        _set_session_cookie(response, token)
        return {"user": orch.reg.get_user(user["id"])}

    @app.post("/api/v1/auth/login", tags=["Auth"], summary="Sign in with username and password")
    def login(req: AuthCredentialsRequest, response: Response):
        user = orch.reg.authenticate_user(req.username, req.password)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid username or password.")
        if user["status"] == "revoked":
            raise HTTPException(status_code=403, detail="Your account has been revoked by an administrator.")
        orch.reg.mark_user_login(user["id"])
        token = orch.reg.create_session(user["id"])
        _set_session_cookie(response, token)
        return {"user": orch.reg.get_user(user["id"])}

    @app.post("/api/v1/auth/logout", tags=["Auth"], summary="Sign out and invalidate session")
    def logout(request: Request, response: Response):
        sess_cookie = request.cookies.get(SESSION_COOKIE, "")
        bearer = _extract_bearer(request)
        if sess_cookie:
            orch.reg.delete_session(sess_cookie)
        if bearer.startswith("hive_sess_"):
            orch.reg.delete_session(bearer)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"status": "logged_out"}

    @app.get("/api/v1/auth/me", tags=["Auth"], summary="Get currently authenticated user profile")
    def auth_me(request: Request):
        caller = _resolve_caller(request, allow_pending_reset=True)
        return {
            "caller_type": caller["type"],
            "scopes": caller["scopes"],
            "user": caller["user"],
        }

    @app.post(
        "/api/v1/auth/reset-credentials",
        tags=["Auth"],
        summary="Reset username and password (required on first login for generated default admin)",
    )
    def reset_credentials(req: ResetCredentialsRequest, request: Request, response: Response):
        caller = _resolve_caller(request, allow_pending_reset=True)
        user = caller.get("user")
        if not user:
            raise HTTPException(status_code=400, detail="Only user accounts can reset credentials.")
        verified = orch.reg.authenticate_user(user["username"], req.current_password)
        if not verified:
            raise HTTPException(status_code=401, detail="Current password is incorrect.")
        try:
            updated = orch.reg.reset_user_credentials(
                user["id"],
                req.new_username,
                req.new_password,
                require_different_username=user["must_reset_credentials"],
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # Rotate session on credential reset
        orch.reg.delete_user_sessions(user["id"])
        new_token = orch.reg.create_session(user["id"])
        _set_session_cookie(response, new_token)
        return {"user": updated}

    # --- Chat / Orchestrator (JSON + File Upload) ----------------------------

    def _run_chat_turn(caller: dict, display_message: str, prompt_message: str,
                       attached_files: list[dict], reset_before: bool) -> ChatResponse:
        _check_user_quota(caller)
        user = caller.get("user")
        tls.logs = []
        start_usage_tracking()
        try:
            with op_lock:
                if user:
                    if reset_before:
                        orch.reg.clear_chat_messages(user["id"])
                    pairs = orch.reg.get_user_chat_pairs(
                        user["id"], max_turns=orch.cfg["runtime"].get("history_turns", 6)
                    )
                    reply = orch.handle(prompt_message, history_override=pairs)
                else:
                    if reset_before:
                        orch.history.clear()
                    reply = orch.handle(prompt_message)
        except LLMError as e:
            usage = finish_usage_tracking()
            if usage["total_tokens"] > 0:
                _record_caller_usage(caller, usage)
            return JSONResponse(status_code=502, content={"detail": str(e), "logs": tls.logs})
        finally:
            logs = list(tls.logs or [])
            tls.logs = None

        usage = finish_usage_tracking()
        quota = _record_caller_usage(caller, usage)
        if user:
            orch.reg.add_chat_message(user["id"], "user", display_message, files=attached_files)
            orch.reg.add_chat_message(
                user["id"], "assistant", reply, logs=logs, tokens_used=usage.get("total_tokens", 0)
            )
        return ChatResponse(reply=reply, logs=logs, files=attached_files, usage=usage, user_quota=quota)

    @app.post(
        "/api/v1/chat",
        response_model=ChatResponse,
        tags=["Orchestrator"],
        summary="Send a JSON prompt to the orchestrator agent",
    )
    @app.post("/chat", response_model=ChatResponse, include_in_schema=False)
    def chat(req: ChatRequest, caller=Depends(require_scope("write"))):
        return _run_chat_turn(caller, req.message, req.message, [], req.reset_before)

    @app.post(
        "/api/v1/chat/upload",
        response_model=ChatResponse,
        tags=["Orchestrator"],
        summary="Send a chat message with optional .md, .txt, or .csv file attachments",
    )
    async def chat_with_files(
        message: str = Form(""),
        reset_before: bool = Form(False),
        files: list[UploadFile] = File(default=[]),
        caller=Depends(require_scope("write")),
    ):
        _check_user_quota(caller)
        msg_clean = message.strip()
        if not msg_clean and not files:
            raise HTTPException(status_code=400, detail="Provide a message or at least one .md/.txt/.csv file.")
        saved_meta, files_block = await _save_chat_uploads(files, caller["name"]) if files else ([], "")
        display_msg = msg_clean or f"Uploaded {len(saved_meta)} file(s) for review."
        prompt_msg = f"{display_msg}\n\n{files_block}" if files_block else display_msg
        return _run_chat_turn(caller, display_msg, prompt_msg, saved_meta, reset_before)

    def _stream_chat_turn(caller: dict, display_message: str, prompt_message: str,
                          attached_files: list[dict], reset_before: bool) -> StreamingResponse:
        """Stream real-time NDJSON thinking/reasoning/tool-trace/content events for ChatGPT-style UI."""
        _check_user_quota(caller)
        ev_queue: queue.Queue = queue.Queue()
        t0 = time.time()

        def _worker():
            user = caller.get("user")
            tls.logs = []
            start_usage_tracking()
            start_stream_emitter(lambda ev: ev_queue.put(ev))
            ev_queue.put({"type": "start", "ts": t0, "files": attached_files})
            try:
                with op_lock:
                    if user:
                        if reset_before:
                            orch.reg.clear_chat_messages(user["id"])
                        pairs = orch.reg.get_user_chat_pairs(
                            user["id"], max_turns=orch.cfg["runtime"].get("history_turns", 6)
                        )
                        reply = orch.handle(prompt_message, history_override=pairs)
                    else:
                        if reset_before:
                            orch.history.clear()
                        reply = orch.handle(prompt_message)
            except LLMError as e:
                stop_stream_emitter()
                usage = finish_usage_tracking()
                if usage["total_tokens"] > 0:
                    _record_caller_usage(caller, usage)
                logs_err = list(tls.logs or [])
                tls.logs = None
                ev_queue.put({"type": "error", "detail": str(e), "logs": logs_err})
                ev_queue.put(None)
                return
            except Exception as e:
                stop_stream_emitter()
                finish_usage_tracking()
                logs_err = list(tls.logs or [])
                tls.logs = None
                ev_queue.put({"type": "error", "detail": f"{type(e).__name__}: {e}", "logs": logs_err})
                ev_queue.put(None)
                return

            stop_stream_emitter()
            duration_s = round(max(0.1, time.time() - t0), 1)
            logs = list(tls.logs or [])
            logs.insert(0, f"[meta] duration={duration_s}s")
            tls.logs = None
            usage = finish_usage_tracking()
            quota = _record_caller_usage(caller, usage)
            if user:
                orch.reg.add_chat_message(user["id"], "user", display_message, files=attached_files)
                orch.reg.add_chat_message(
                    user["id"], "assistant", reply, logs=logs, tokens_used=usage.get("total_tokens", 0)
                )
            ev_queue.put({
                "type": "done",
                "reply": reply,
                "logs": logs,
                "files": attached_files,
                "usage": usage,
                "user_quota": quota,
                "duration_s": duration_s,
            })
            ev_queue.put(None)

        threading.Thread(target=_worker, name="hive-chat-stream", daemon=True).start()

        def _iter_ndjson():
            while True:
                try:
                    item = ev_queue.get(timeout=12.0)
                except queue.Empty:
                    yield json.dumps({"type": "ping", "ts": time.time()}) + "\n"
                    continue
                if item is None:
                    break
                yield json.dumps(item, ensure_ascii=False) + "\n"

        return StreamingResponse(
            _iter_ndjson(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post(
        "/api/v1/chat/stream",
        tags=["Orchestrator"],
        summary="Stream real-time NDJSON thinking, tool trace, and answer events",
    )
    def chat_stream(req: ChatRequest, caller=Depends(require_scope("write"))):
        return _stream_chat_turn(caller, req.message, req.message, [], req.reset_before)

    @app.post(
        "/api/v1/chat/upload/stream",
        tags=["Orchestrator"],
        summary="Stream real-time NDJSON thinking & answer events with .md/.txt/.csv file uploads",
    )
    async def chat_with_files_stream(
        message: str = Form(""),
        reset_before: bool = Form(False),
        files: list[UploadFile] = File(default=[]),
        caller=Depends(require_scope("write")),
    ):
        _check_user_quota(caller)
        msg_clean = message.strip()
        if not msg_clean and not files:
            raise HTTPException(status_code=400, detail="Provide a message or at least one .md/.txt/.csv file.")
        saved_meta, files_block = await _save_chat_uploads(files, caller["name"]) if files else ([], "")
        display_msg = msg_clean or f"Uploaded {len(saved_meta)} file(s) for review."
        prompt_msg = f"{display_msg}\n\n{files_block}" if files_block else display_msg
        return _stream_chat_turn(caller, display_msg, prompt_msg, saved_meta, reset_before)

    @app.get(
        "/api/v1/chat/history",
        tags=["Orchestrator"],
        summary="Get persistent conversation history for the current user",
    )
    def chat_history(limit: int = Query(100, ge=1, le=500), caller=Depends(require_scope("read"))):
        user = caller.get("user")
        if user:
            msgs = orch.reg.list_chat_messages(user["id"], limit=limit)
            return {
                "persistent": True,
                "messages": msgs,
                "turns": sum(1 for m in msgs if m["role"] == "assistant"),
                "max_turns": orch.cfg["runtime"].get("history_turns", 6),
            }
        with op_lock:
            exchanges = [{"user": u, "assistant": a} for u, a in orch.history]
        return {
            "persistent": False,
            "turns": len(exchanges),
            "max_turns": orch.cfg["runtime"].get("history_turns", 6),
            "history": exchanges,
        }

    @app.post(
        "/api/v1/chat/reset",
        tags=["Orchestrator"],
        summary="Clear conversation history for the current user",
    )
    @app.post("/reset", include_in_schema=False)
    def chat_reset(caller=Depends(require_scope("write"))):
        user = caller.get("user")
        if user:
            orch.reg.clear_chat_messages(user["id"])
        else:
            with op_lock:
                orch.history.clear()
        return {"status": "cleared"}

    # --- Agents CRUD ---------------------------------------------------------

    @app.get("/api/v1/agents", tags=["Agents"], summary="List all worker agents")
    @app.get("/agents", include_in_schema=False)
    def list_agents(_caller=Depends(require_scope("read"))):
        return {"agents": [a.__dict__ for a in orch.reg.list_agents()]}

    @app.post("/api/v1/agents", status_code=201, tags=["Agents"], summary="Create or overwrite a worker agent")
    def create_agent(req: AgentCreateRequest, _caller=Depends(require_scope("write"))):
        name = req.name.strip().lower()
        if not NAME_RE.match(name):
            raise HTTPException(
                status_code=400,
                detail="name must be lowercase_snake_case, 2-40 chars, starting with a letter",
            )
        bad = [t for t in req.tools if t not in orch.lib.names()]
        if bad:
            raise HTTPException(status_code=400, detail=f"unknown tools {bad}; allowed: {orch.lib.names()}")
        w = orch.cfg["workers"]
        resolved_model, resolved_ep = orch.resolve_model(req.model or w["model"], req.endpoint or None)
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
                raise HTTPException(status_code=400, detail=f"unknown tools {bad}; allowed: {orch.lib.names()}")
            a.tools = list(dict.fromkeys(req.tools))
        if req.model is not None or req.endpoint is not None:
            rm, rep = orch.resolve_model(req.model or a.model, req.endpoint or a.endpoint)
            ep = rep or a.endpoint
            if ep not in orch.cfg["endpoints"]:
                raise HTTPException(status_code=400, detail=f"unknown endpoint '{ep}'")
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
    def run_worker(req: RunWorkerRequest, caller=Depends(require_scope("write"))):
        _check_user_quota(caller)
        tls.logs = []
        start_usage_tracking()
        try:
            with op_lock:
                result = orch.run_worker(
                    req.agent, req.task, model_override=req.model, endpoint_override=req.endpoint
                )
        except LLMError as e:
            usage = finish_usage_tracking()
            if usage["total_tokens"] > 0:
                _record_caller_usage(caller, usage)
            return JSONResponse(status_code=502, content={"detail": str(e), "logs": tls.logs})
        finally:
            logs = list(tls.logs or [])
            tls.logs = None
        usage = finish_usage_tracking()
        quota = _record_caller_usage(caller, usage)
        return RunWorkerResponse(result=result, logs=logs, usage=usage, user_quota=quota)

    @app.get("/api/v1/tasks", tags=["Tasks"], summary="List recent tasks with optional filters")
    @app.get("/tasks", include_in_schema=False)
    def list_tasks(
        limit: int = Query(20, ge=1, le=200),
        agent: Optional[str] = Query(None),
        status: Optional[str] = Query(None),
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

    @app.get("/api/v1/models", tags=["Models & Tools"], summary="Show active models, aliases, and endpoints")
    @app.get("/model", include_in_schema=False)
    def get_models(_caller=Depends(require_scope("read"))):
        with op_lock:
            return _model_summary()

    @app.post("/api/v1/models", tags=["Models & Tools"], summary="Switch and persist model/endpoint live")
    @app.post("/model", include_in_schema=False)
    def switch_model(req: ModelSwitchRequest, _caller=Depends(require_scope("admin"))):
        with op_lock:
            rm, rep = orch.resolve_model(req.model, req.endpoint)
            if rep and rep not in orch.cfg["endpoints"]:
                raise HTTPException(status_code=400, detail=f"unknown endpoint '{rep}'")
            target = req.target.strip()
            if target in ("orch", "orchestrator"):
                orch.cfg["orchestrator"]["model"] = rm
                orch.reg.set_setting("orchestrator_model", rm)
                if rep:
                    orch.cfg["orchestrator"]["endpoint"] = rep
                    orch.reg.set_setting("orchestrator_endpoint", rep)
            elif target in ("workers", "worker", "child", "children"):
                orch.cfg["workers"]["model"] = rm
                orch.reg.set_setting("workers_model", rm)
                if rep:
                    orch.cfg["workers"]["endpoint"] = rep
                    orch.reg.set_setting("workers_endpoint", rep)
            else:
                a = orch.reg.get_agent(target)
                if not a:
                    raise HTTPException(status_code=404, detail=f"no agent named '{target}'")
                a.model = rm
                if rep:
                    a.endpoint = rep
                orch.reg.save_agent(a)
            return _model_summary()

    @app.get("/api/v1/tools", tags=["Models & Tools"], summary="List built-in and dynamically created custom tools")
    def list_tools(_caller=Depends(require_scope("read"))):
        return {
            "tools": orch.lib.list_tool_details(),
            "creator_model": orch.tool_creator.resolve_creator_model()[0],
        }

    @app.post(
        "/api/v1/tools/create",
        status_code=201,
        tags=["Models & Tools"],
        summary="Synthesize, test, and persist a new reusable tool using deepseek-chat",
    )
    def create_custom_tool(req: ToolCreateRequest, caller=Depends(require_scope("write"))):
        _check_user_quota(caller)
        tls.logs = []
        start_usage_tracking()
        try:
            with op_lock:
                saved = orch.tool_creator.create_tool(
                    req.name, req.description, req.requirements, created_by=caller["name"]
                )
        except ValueError as e:
            finish_usage_tracking()
            raise HTTPException(status_code=400, detail=str(e))
        except (RuntimeError, LLMError) as e:
            usage = finish_usage_tracking()
            if usage["total_tokens"] > 0:
                _record_caller_usage(caller, usage)
            return JSONResponse(status_code=502, content={"detail": str(e), "logs": tls.logs})
        finally:
            logs = list(tls.logs or [])
            tls.logs = None
        usage = finish_usage_tracking()
        quota = _record_caller_usage(caller, usage)
        return {"tool": saved, "logs": logs, "usage": usage, "user_quota": quota}

    @app.post("/api/v1/tools/{name}/test", tags=["Models & Tools"], summary="Invoke a registered tool directly with JSON args")
    def test_tool(name: str, req: ToolTestRequest, _caller=Depends(require_scope("write"))):
        if name not in orch.lib.tools:
            raise HTTPException(status_code=404, detail=f"no tool named '{name}'")
        from .runner import _execute
        out = _execute({"name": name, "arguments": json.dumps(req.arguments)}, orch.lib.tools)
        return {"tool": name, "output": out}

    @app.delete("/api/v1/tools/{name}", tags=["Models & Tools"], summary="Delete a dynamically created custom tool")
    def delete_custom_tool(name: str, _caller=Depends(require_scope("admin"))):
        if name in orch.lib.builtin_names():
            raise HTTPException(status_code=400, detail=f"built-in tool '{name}' cannot be deleted")
        if not orch.lib.unregister_custom_tool(name):
            raise HTTPException(status_code=404, detail=f"no custom tool named '{name}'")
        return {"deleted": True, "name": name}

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

    @app.get("/api/v1/workspace/files/{file_path:path}", tags=["Workspace"], summary="Read a text file in workspace/")
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

    @app.put("/api/v1/workspace/files/{file_path:path}", tags=["Workspace"], summary="Create/overwrite a text file")
    def write_workspace_file(
        file_path: str, req: WorkspaceWriteRequest, _caller=Depends(require_scope("write"))
    ):
        try:
            p = orch.lib._path(file_path)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(req.content, encoding="utf-8")
        return {"path": p.relative_to(orch.lib.ws).as_posix(), "bytes": len(req.content.encode("utf-8"))}

    @app.delete("/api/v1/workspace/files/{file_path:path}", tags=["Workspace"], summary="Delete a file in workspace/")
    def delete_workspace_file(file_path: str, _caller=Depends(require_scope("admin"))):
        try:
            p = orch.lib._path(file_path)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"file not found: {file_path}")
        p.unlink()
        return {"deleted": True, "path": file_path}

    # --- Admin User Management & Settings ------------------------------------

    @app.get("/api/v1/admin/users", tags=["Admin"], summary="List all users, statuses, and token usage")
    def admin_list_users(_caller=Depends(require_scope("admin"))):
        return {"users": orch.reg.list_users()}

    @app.post("/api/v1/admin/users", status_code=201, tags=["Admin"], summary="Create a user account as admin")
    def admin_create_user(req: AdminCreateUserRequest, _caller=Depends(require_scope("admin"))):
        try:
            user = orch.reg.create_user(
                req.username, req.password, role=req.role, token_limit=req.token_limit
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"user": user}

    @app.patch(
        "/api/v1/admin/users/{user_id}",
        tags=["Admin"],
        summary="Revoke, restrict, activate, or set token usage limits for a user",
    )
    def admin_update_user(
        user_id: str, req: AdminUpdateUserRequest, caller=Depends(require_scope("admin"))
    ):
        me = caller.get("user")
        if me and me["id"] == user_id:
            if req.status in ("revoked", "restricted") or req.role == "user":
                raise HTTPException(status_code=400, detail="Admins cannot revoke, restrict, or demote themselves.")
        limit_arg = None if req.clear_token_limit else (req.token_limit if req.token_limit is not None else "UNSET")
        try:
            updated = orch.reg.update_user_admin(
                user_id,
                status=req.status,
                role=req.role,
                token_limit=limit_arg,
                reset_tokens=req.reset_tokens,
                restriction_reason=req.restriction_reason,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not updated:
            raise HTTPException(status_code=404, detail=f"no user '{user_id}'")
        tg_manager.sync_user(user_id)
        return {"user": updated}

    @app.delete("/api/v1/admin/users/{user_id}", tags=["Admin"], summary="Permanently delete a user account")
    def admin_delete_user(user_id: str, caller=Depends(require_scope("admin"))):
        me = caller.get("user")
        if me and me["id"] == user_id:
            raise HTTPException(status_code=400, detail="Admins cannot delete their own account.")
        if not orch.reg.delete_user(user_id):
            raise HTTPException(status_code=404, detail=f"no user '{user_id}'")
        tg_manager.sync_user(user_id)
        return {"deleted": True, "id": user_id}

    @app.get("/api/v1/admin/settings", tags=["Admin"], summary="Get persisted global platform settings")
    def admin_get_settings(_caller=Depends(require_scope("admin"))):
        return {
            "allow_signup": bool(orch.reg.get_setting("allow_signup", True)),
            "default_user_token_limit": orch.reg.get_setting("default_user_token_limit", DEFAULT_TOKEN_LIMIT),
        }

    @app.patch("/api/v1/admin/settings", tags=["Admin"], summary="Update persisted global platform settings")
    def admin_update_settings(req: AdminSettingsRequest, _caller=Depends(require_scope("admin"))):
        if req.allow_signup is not None:
            orch.reg.set_setting("allow_signup", bool(req.allow_signup))
        if req.clear_default_token_limit:
            orch.reg.set_setting("default_user_token_limit", None)
        elif req.default_user_token_limit is not None:
            orch.reg.set_setting("default_user_token_limit", int(req.default_user_token_limit))
        return {
            "allow_signup": bool(orch.reg.get_setting("allow_signup", True)),
            "default_user_token_limit": orch.reg.get_setting("default_user_token_limit", DEFAULT_TOKEN_LIMIT),
        }

    # --- API Key Generation & Distribution -----------------------------------

    @app.get("/api/v1/keys", tags=["API Keys"], summary="List all generated API keys")
    def list_keys(_caller=Depends(require_scope("admin"))):
        return {
            "auth_enabled": True,
            "master_token_configured": bool(master_token),
            "keys": orch.reg.list_api_keys(),
        }

    @app.post("/api/v1/keys", status_code=201, tags=["API Keys"], summary="Generate an API key & distribution kit")
    def create_key(
        req: ApiKeyCreateRequest, request: Request, _caller=Depends(require_scope("admin"))
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

    @app.post("/api/v1/keys/{key_id}/rotate", tags=["API Keys"], summary="Rotate an API key")
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

    @app.delete("/api/v1/keys/{key_id}", tags=["API Keys"], summary="Revoke an API key immediately")
    def revoke_key(key_id: str, _caller=Depends(require_scope("admin"))):
        if not orch.reg.revoke_api_key(key_id):
            if not orch.reg.get_api_key(key_id):
                raise HTTPException(status_code=404, detail=f"no API key '{key_id}'")
        return {"revoked": True, "id": key_id}

    # --- Task Scheduler (Persistent) -----------------------------------------

    def _can_manage_schedule(caller: dict, sch: dict) -> bool:
        if "admin" in caller["scopes"]:
            return True
        u = caller.get("user")
        return bool(u and sch.get("user_id") == u["id"])

    @app.get("/api/v1/schedules", tags=["Scheduler"], summary="List scheduled tasks")
    def list_schedules(caller=Depends(require_scope("read"))):
        if "admin" in caller["scopes"]:
            items = orch.reg.list_schedules()
        else:
            u = caller.get("user")
            items = orch.reg.list_schedules(user_id=u["id"]) if u else []
        return {
            "schedules": items,
            "allowed_intervals_minutes": list(ALLOWED_INTERVAL_MINUTES),
        }

    @app.post("/api/v1/schedules", status_code=201, tags=["Scheduler"], summary="Create a recurring scheduled task")
    def create_schedule(req: ScheduleCreateRequest, caller=Depends(require_scope("write"))):
        _check_user_quota(caller)
        target_type = req.target_type
        agent_name = req.agent_name.strip()
        if target_type == "worker":
            if not agent_name or not orch.reg.get_agent(agent_name):
                raise HTTPException(
                    status_code=400, detail=f"unknown worker agent '{agent_name}'"
                )
        else:
            agent_name = ""
        try:
            trig, iv, dt_str, days, tz_off = validate_trigger(
                req.trigger_type,
                req.interval_minutes,
                req.daily_time,
                req.days_of_week,
                req.timezone_offset_minutes,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        next_ts = (
            compute_next_run(trig, iv, dt_str, days, tz_off) if req.enabled else None
        )
        u = caller.get("user")
        sch = orch.reg.create_schedule(
            user_id=u["id"] if u else None,
            owner_name=caller["name"],
            name=req.name.strip(),
            target_type=target_type,
            agent_name=agent_name,
            prompt=req.prompt.strip(),
            trigger_type=trig,
            interval_minutes=iv,
            daily_time=dt_str,
            days_of_week=days,
            timezone_offset_minutes=tz_off,
            enabled=req.enabled,
            next_run_at=next_ts,
            notify_telegram=req.notify_telegram,
            notify_whatsapp=req.notify_whatsapp,
        )
        return {"schedule": sch}

    @app.patch("/api/v1/schedules/{schedule_id}", tags=["Scheduler"], summary="Pause, resume, or update a schedule")
    def update_schedule(
        schedule_id: str, req: ScheduleUpdateRequest, caller=Depends(require_scope("write"))
    ):
        sch = orch.reg.get_schedule(schedule_id)
        if not sch:
            raise HTTPException(status_code=404, detail=f"no schedule '{schedule_id}'")
        if not _can_manage_schedule(caller, sch):
            raise HTTPException(status_code=403, detail="You can only modify your own schedules.")

        new_target = req.target_type if req.target_type is not None else sch["target_type"]
        new_agent = req.agent_name.strip() if req.agent_name is not None else sch["agent_name"]
        if new_target == "worker":
            if not new_agent or not orch.reg.get_agent(new_agent):
                raise HTTPException(status_code=400, detail=f"unknown worker agent '{new_agent}'")
        else:
            new_agent = ""

        trig_in = req.trigger_type if req.trigger_type is not None else sch["trigger_type"]
        iv_in = req.interval_minutes if req.interval_minutes is not None else sch["interval_minutes"]
        dt_in = req.daily_time if req.daily_time is not None else sch["daily_time"]
        days_in = req.days_of_week if req.days_of_week is not None else sch["days_of_week"]
        tz_in = req.timezone_offset_minutes if req.timezone_offset_minutes is not None else sch["timezone_offset_minutes"]

        try:
            trig, iv, dt_str, days, tz_off = validate_trigger(trig_in, iv_in, dt_in, days_in, tz_in)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        new_enabled = sch["enabled"] if req.enabled is None else req.enabled
        trigger_changed = any(
            v is not None
            for v in (req.trigger_type, req.interval_minutes, req.daily_time, req.days_of_week, req.timezone_offset_minutes)
        )
        if not new_enabled:
            next_ts = None
        elif trigger_changed or not sch["enabled"] or sch["next_run_at"] is None:
            next_ts = compute_next_run(trig, iv, dt_str, days, tz_off)
        else:
            next_ts = sch["next_run_at"]

        updated = orch.reg.update_schedule(schedule_id, {
            "name": req.name.strip() if req.name is not None else sch["name"],
            "target_type": new_target,
            "agent_name": new_agent,
            "prompt": req.prompt.strip() if req.prompt is not None else sch["prompt"],
            "trigger_type": trig,
            "interval_minutes": iv,
            "daily_time": dt_str,
            "days_of_week": days,
            "timezone_offset_minutes": tz_off,
            "enabled": new_enabled,
            "next_run_at": next_ts,
            "notify_telegram": sch["notify_telegram"] if req.notify_telegram is None else req.notify_telegram,
            "notify_whatsapp": sch["notify_whatsapp"] if req.notify_whatsapp is None else req.notify_whatsapp,
        })
        return {"schedule": updated}

    @app.delete("/api/v1/schedules/{schedule_id}", tags=["Scheduler"], summary="Delete a scheduled task")
    def delete_schedule(schedule_id: str, caller=Depends(require_scope("write"))):
        sch = orch.reg.get_schedule(schedule_id)
        if not sch:
            raise HTTPException(status_code=404, detail=f"no schedule '{schedule_id}'")
        if not _can_manage_schedule(caller, sch):
            raise HTTPException(status_code=403, detail="You can only delete your own schedules.")
        orch.reg.delete_schedule(schedule_id)
        return {"deleted": True, "id": schedule_id}

    @app.post("/api/v1/schedules/{schedule_id}/run", tags=["Scheduler"], summary="Run a scheduled task immediately")
    def run_schedule_now(schedule_id: str, caller=Depends(require_scope("write"))):
        _check_user_quota(caller)
        sch = orch.reg.get_schedule(schedule_id)
        if not sch:
            raise HTTPException(status_code=404, detail=f"no schedule '{schedule_id}'")
        if not _can_manage_schedule(caller, sch):
            raise HTTPException(status_code=403, detail="You can only run your own schedules.")
        run = scheduler.execute_schedule(schedule_id, scheduled=False)
        fresh_sch = orch.reg.get_schedule(schedule_id)
        u = caller.get("user")
        quota = None
        if u:
            fu = orch.reg.get_user(u["id"])
            if fu:
                quota = {"tokens_used": fu["tokens_used"], "token_limit": fu["token_limit"]}
        return {"run": run, "schedule": fresh_sch, "user_quota": quota}

    @app.get("/api/v1/schedules/{schedule_id}/runs", tags=["Scheduler"], summary="Get execution history of a schedule")
    def list_schedule_runs(
        schedule_id: str,
        limit: int = Query(30, ge=1, le=200),
        caller=Depends(require_scope("read")),
    ):
        sch = orch.reg.get_schedule(schedule_id)
        if not sch:
            raise HTTPException(status_code=404, detail=f"no schedule '{schedule_id}'")
        if not _can_manage_schedule(caller, sch):
            raise HTTPException(status_code=403, detail="You can only view your own schedule runs.")
        return {"runs": orch.reg.list_schedule_runs(schedule_id, limit=limit)}

    # --- Inbox: Email (Batch Drafts + Important Summary) & Personal WhatsApp (QR) ---

    @app.get("/api/v1/inbox/drafts", tags=["Inbox"], summary="List editable Email & WhatsApp reply drafts")
    def list_inbox_drafts(
        channel: Optional[Literal["email", "whatsapp"]] = Query(None),
        status: Optional[Literal["pending", "sent", "dismissed"]] = Query(None),
        limit: int = Query(100, ge=1, le=300),
        _caller=Depends(require_scope("read")),
    ):
        return {
            "drafts": orch.reg.list_inbox_drafts(channel=channel, status=status, limit=limit),
            "email_configured": is_email_configured(orch.reg),
        }

    @app.patch("/api/v1/inbox/drafts/{draft_id}", tags=["Inbox"], summary="Edit a reply draft or dismiss/restore it")
    def update_inbox_draft(draft_id: str, req: DraftUpdateRequest, _caller=Depends(require_scope("write"))):
        fields = {}
        if req.draft_subject is not None:
            fields["draft_subject"] = req.draft_subject.strip()
        if req.draft_reply is not None:
            fields["draft_reply"] = req.draft_reply
        if req.status is not None:
            fields["status"] = req.status
        updated = orch.reg.update_inbox_draft(draft_id, fields)
        if not updated:
            raise HTTPException(status_code=404, detail=f"no draft '{draft_id}'")
        return {"draft": updated}

    def _deliver_single_draft(draft_id: str) -> dict:
        drf = orch.reg.get_inbox_draft(draft_id)
        if not drf:
            raise HTTPException(status_code=404, detail=f"no draft '{draft_id}'")
        if not drf["draft_reply"].strip():
            raise HTTPException(status_code=400, detail="Draft reply cannot be empty.")
        try:
            if drf["channel"] == "email":
                send_email_reply(
                    to_addr=drf["sender_addr"],
                    subject=drf["draft_subject"] or drf["subject"],
                    body=drf["draft_reply"],
                    in_reply_to=drf["in_reply_to"],
                    registry=orch.reg,
                )
            elif drf["channel"] == "whatsapp":
                send_whatsapp_message(drf["sender_addr"], drf["draft_reply"], registry=orch.reg)
                orch.reg.mark_whatsapp_read(chat_id=drf["sender_addr"])
            else:
                raise HTTPException(status_code=400, detail=f"unsupported channel '{drf['channel']}'")
        except (EmailConfigError, WhatsAppError, Exception) as e:
            orch.reg.update_inbox_draft(draft_id, {"error": str(e)})
            raise HTTPException(status_code=502, detail=str(e))
        return orch.reg.update_inbox_draft(
            draft_id, {"status": "sent", "error": "", "sent_at": time.time()}
        )

    @app.post("/api/v1/inbox/drafts/{draft_id}/send", tags=["Inbox"], summary="Send an edited Email or WhatsApp draft")
    def send_inbox_draft(draft_id: str, _caller=Depends(require_scope("write"))):
        updated = _deliver_single_draft(draft_id)
        return {"sent": True, "draft": updated}

    @app.post("/api/v1/inbox/drafts/send-batch", tags=["Inbox"], summary="Send multiple approved/edited drafts at once")
    def send_inbox_drafts_batch(req: BatchSendDraftsRequest, _caller=Depends(require_scope("write"))):
        results = []
        for did in req.draft_ids:
            try:
                upd = _deliver_single_draft(did)
                results.append({"id": did, "sent": True, "draft": upd})
            except HTTPException as e:
                results.append({"id": did, "sent": False, "error": e.detail})
        return {"results": results}

    @app.delete("/api/v1/inbox/drafts/{draft_id}", tags=["Inbox"], summary="Delete a reply draft")
    def delete_inbox_draft(draft_id: str, _caller=Depends(require_scope("write"))):
        if not orch.reg.delete_inbox_draft(draft_id):
            raise HTTPException(status_code=404, detail=f"no draft '{draft_id}'")
        return {"deleted": True, "id": draft_id}

    @app.get("/api/v1/inbox/email/config", tags=["Inbox"], summary="Get Email IMAP/SMTP config (password masked)")
    def get_email_settings(_caller=Depends(require_scope("admin"))):
        c = get_email_config(orch.reg)
        return {
            "configured": bool(c["imap_host"] and c["user"] and c["password"]),
            "imap_host": c["imap_host"],
            "imap_port": c["imap_port"],
            "smtp_host": c["smtp_host"],
            "smtp_port": c["smtp_port"],
            "user": c["user"],
            "has_password": bool(c["password"]),
            "folder": c["folder"],
        }

    @app.put("/api/v1/inbox/email/config", tags=["Inbox"], summary="Save Email IMAP/SMTP credentials persistently")
    def save_email_settings(req: EmailConfigUpdateRequest, _caller=Depends(require_scope("admin"))):
        prev = orch.reg.get_setting("email_config", {}) or {}
        new_cfg = {
            "imap_host": req.imap_host.strip(),
            "imap_port": int(req.imap_port),
            "smtp_host": req.smtp_host.strip(),
            "smtp_port": int(req.smtp_port),
            "user": req.user.strip(),
            "password": req.password if (req.password is not None and req.password != "") else prev.get("password", ""),
            "folder": req.folder.strip() or "INBOX",
        }
        orch.reg.set_setting("email_config", new_cfg)
        return {"saved": True, "configured": is_email_configured(orch.reg)}

    def _save_email_cache(scan_last: int, emails: list[dict]) -> dict:
        snap = {
            "fetched_at": time.time(),
            "scanned_last": scan_last,
            "unread_count": len(emails),
            "emails": emails,
        }
        orch.reg.set_setting("email_unread_cache", snap)
        return snap

    @app.get(
        "/api/v1/inbox/email/unread",
        tags=["Inbox"],
        summary="Get saved unread emails or refresh from IMAP (persisted until next fetch)",
    )
    def list_unread_emails_endpoint(
        scan_last: int = Query(100, ge=1, le=500),
        refresh: bool = Query(False, description="Set true to fetch fresh emails from IMAP and replace cache"),
        _caller=Depends(require_scope("read")),
    ):
        last_summary = orch.reg.get_setting("email_summary_cache")
        if not refresh:
            cached = orch.reg.get_setting("email_unread_cache")
            if isinstance(cached, dict) and "emails" in cached:
                return {**cached, "cached": True, "last_summary": last_summary}
            return {
                "fetched_at": None,
                "scanned_last": scan_last,
                "unread_count": 0,
                "emails": [],
                "cached": True,
                "last_summary": last_summary,
            }
        try:
            emails = fetch_unread_from_last(orch.reg, scan_last=scan_last, body_chars=3000)
        except EmailConfigError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))
        snap = _save_email_cache(scan_last, emails)
        return {**snap, "cached": False, "last_summary": last_summary}

    @app.post(
        "/api/v1/inbox/email/draft-one",
        tags=["Inbox"],
        summary="Prepare an editable reply draft for a single selected unread email",
    )
    def draft_single_email_endpoint(req: EmailSingleDraftRequest, caller=Depends(require_scope("write"))):
        _check_user_quota(caller)
        start_usage_tracking()
        sample = [{
            "uid": req.uid or "single",
            "message_id": req.message_id,
            "from": f"{req.sender_name} <{req.sender_email}>",
            "sender_name": req.sender_name or req.sender_email,
            "sender_email": req.sender_email,
            "subject": req.subject,
            "date": "",
            "is_automated": False,
            "body": req.body,
        }]
        try:
            with op_lock:
                res = orch.batch_prepare_email_drafts(
                    scan_last=1, instructions=req.instructions, max_drafts=1, emails_override=sample
                )
        except Exception as e:
            finish_usage_tracking()
            raise HTTPException(status_code=502, detail=str(e))
        usage = finish_usage_tracking()
        quota = _record_caller_usage(caller, usage)
        if not res["drafts"]:
            # Fallback: create blank editable draft so user can still edit & send directly
            drf = orch.reg.create_inbox_draft(
                channel="email",
                external_id=req.message_id or f"uid:{req.uid}",
                sender_name=req.sender_name or req.sender_email,
                sender_addr=req.sender_email,
                subject=req.subject,
                in_reply_to=req.message_id,
                incoming_body=req.body,
                draft_reply="",
                summary=req.subject,
            )
            res["drafts"] = [drf]
        return {"draft": res["drafts"][0], "usage": usage, "user_quota": quota}

    @app.post(
        "/api/v1/inbox/email/summarize",
        tags=["Inbox"],
        summary="Summarize important unread emails from the last 100 inbox messages",
    )
    def summarize_emails_endpoint(req: EmailSummarizeRequest, caller=Depends(require_scope("write"))):
        _check_user_quota(caller)
        start_usage_tracking()
        try:
            emails = fetch_unread_from_last(orch.reg, scan_last=req.scan_last, body_chars=3000)
            snap = _save_email_cache(req.scan_last, emails)
            with op_lock:
                res = orch.summarize_important_emails(scan_last=req.scan_last, emails_override=emails)
        except EmailConfigError as e:
            finish_usage_tracking()
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            usage = finish_usage_tracking()
            if usage["total_tokens"] > 0:
                _record_caller_usage(caller, usage)
            raise HTTPException(status_code=502, detail=str(e))
        usage = finish_usage_tracking()
        quota = _record_caller_usage(caller, usage)
        summary_snap = {**res, "fetched_at": snap["fetched_at"]}
        orch.reg.set_setting("email_summary_cache", summary_snap)
        return {**summary_snap, "usage": usage, "user_quota": quota}

    @app.post(
        "/api/v1/inbox/email/batch-draft",
        tags=["Inbox"],
        summary="Batch-prepare editable reply drafts for all important unread emails from the last 100",
    )
    def batch_draft_emails_endpoint(req: EmailBatchDraftRequest, caller=Depends(require_scope("write"))):
        _check_user_quota(caller)
        start_usage_tracking()
        try:
            emails = fetch_unread_from_last(orch.reg, scan_last=req.scan_last, body_chars=3000)
            snap = _save_email_cache(req.scan_last, emails)
            with op_lock:
                res = orch.batch_prepare_email_drafts(
                    scan_last=req.scan_last,
                    instructions=req.instructions,
                    max_drafts=req.max_drafts,
                    emails_override=emails,
                )
        except EmailConfigError as e:
            finish_usage_tracking()
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            usage = finish_usage_tracking()
            if usage["total_tokens"] > 0:
                _record_caller_usage(caller, usage)
            raise HTTPException(status_code=502, detail=str(e))
        usage = finish_usage_tracking()
        quota = _record_caller_usage(caller, usage)
        return {**res, "fetched_at": snap["fetched_at"], "usage": usage, "user_quota": quota}

    # --- Personal WhatsApp via WAHA QR Sidecar -------------------------------

    @app.get("/api/v1/inbox/whatsapp/status", tags=["Inbox"], summary="Get personal WhatsApp (WAHA) session status")
    def whatsapp_status(_caller=Depends(require_scope("read"))):
        return wa_get_status(orch.reg)

    @app.post("/api/v1/inbox/whatsapp/start", tags=["Inbox"], summary="Start personal WhatsApp session for QR pairing")
    def whatsapp_start(_caller=Depends(require_scope("admin"))):
        try:
            data = wa_start_session(orch.reg)
        except WhatsAppError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {"started": True, "session": data}

    @app.post("/api/v1/inbox/whatsapp/stop", tags=["Inbox"], summary="Stop personal WhatsApp session")
    def whatsapp_stop(_caller=Depends(require_scope("admin"))):
        try:
            data = wa_stop_session(orch.reg)
        except WhatsAppError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {"stopped": True, "session": data}

    @app.get("/api/v1/inbox/whatsapp/qr", tags=["Inbox"], summary="Fetch live WhatsApp QR code PNG for pairing")
    def whatsapp_qr(_caller=Depends(require_scope("admin"))):
        try:
            img_bytes, ctype = fetch_qr_image(orch.reg)
        except WhatsAppError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return Response(content=img_bytes, media_type=ctype)

    @app.post(
        "/api/v1/inbox/whatsapp/enable-sync",
        tags=["Inbox"],
        summary="Enable WAHA NOWEB chat store and restart session to load all unread WhatsApp messages",
    )
    def whatsapp_enable_sync(_caller=Depends(require_scope("admin"))):
        try:
            data = wa_enable_store(orch.reg)
        except WhatsAppError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {"enabled": True, "session": data}

    @app.get(
        "/api/v1/inbox/whatsapp/unread",
        tags=["Inbox"],
        summary="Show all unread WhatsApp messages (from WAHA store + incoming webhook log)",
    )
    def whatsapp_unread_list(limit: int = Query(50, ge=1, le=200), _caller=Depends(require_scope("read"))):
        local_items = orch.reg.list_unread_whatsapp_messages(limit=limit)
        store_enabled = False
        hint = ""
        live_items = []
        try:
            res = wa_fetch_unread(orch.reg, limit=limit)
            store_enabled = bool(res.get("store_enabled"))
            live_items = res.get("items") or []
            hint = res.get("hint", "")
        except WhatsAppError as e:
            hint = str(e)

        # Merge live store chats and local webhook unread messages (avoid duplicate chat_id if store has it)
        seen_chats = {x["chat_id"] for x in live_items}
        merged = list(live_items) + [x for x in local_items if x["chat_id"] not in seen_chats]
        return {
            "store_enabled": store_enabled,
            "unread_count": len(merged),
            "messages": merged,
            "hint": hint,
        }

    @app.post(
        "/api/v1/inbox/whatsapp/unread/{msg_id}/read",
        tags=["Inbox"],
        summary="Mark a logged unread WhatsApp message as read",
    )
    def whatsapp_mark_read(msg_id: str, _caller=Depends(require_scope("write"))):
        orch.reg.mark_whatsapp_read(msg_id=msg_id)
        return {"ok": True}

    @app.post("/api/v1/inbox/whatsapp/manual-draft", tags=["Inbox"], summary="Generate an editable WhatsApp reply draft")
    def whatsapp_manual_draft(req: WhatsAppManualDraftRequest, caller=Depends(require_scope("write"))):
        _check_user_quota(caller)
        start_usage_tracking()
        try:
            with op_lock:
                drf = orch.draft_whatsapp_reply(
                    sender_name=req.sender_name,
                    chat_id=req.chat_id,
                    incoming_text=req.incoming_text,
                    instructions=req.instructions,
                )
        except Exception as e:
            finish_usage_tracking()
            raise HTTPException(status_code=502, detail=str(e))
        usage = finish_usage_tracking()
        quota = _record_caller_usage(caller, usage)
        return {"draft": drf, "usage": usage, "user_quota": quota}

    @app.post(
        "/api/v1/webhooks/whatsapp",
        tags=["Inbox"],
        summary="Webhook receiver for WAHA incoming personal WhatsApp messages",
    )
    async def whatsapp_webhook(request: Request):
        wa_cfg = get_whatsapp_config(orch.reg)
        secret = wa_cfg["webhook_secret"]
        if secret:
            hdr = request.headers.get("x-hive-webhook-secret", "")
            if not hmac.compare_digest(hdr, secret):
                raise HTTPException(status_code=401, detail="invalid webhook secret")
        body = await request.json()
        event = body.get("event")
        payload = body.get("payload") or {}
        if event != "message":
            return {"ignored": True, "event": event}
        if payload.get("fromMe"):
            return {"ignored": True, "reason": "fromMe"}
        chat_id = str(payload.get("from") or payload.get("chatId") or "")
        text = str(payload.get("body") or "").strip()
        if not chat_id or not text or chat_id.endswith("@broadcast") or chat_id == "status@broadcast":
            return {"ignored": True, "reason": "empty_or_broadcast"}
        sender_name = str(
            (payload.get("_data") or {}).get("notifyName")
            or payload.get("pushName")
            or chat_id.split("@")[0]
        )
        ext_id = str(payload.get("id") or "")
        orch.reg.save_whatsapp_message(ext_id, chat_id, sender_name, text)
        try:
            with op_lock:
                drf = orch.draft_whatsapp_reply(
                    sender_name=sender_name,
                    chat_id=chat_id,
                    incoming_text=text,
                    external_id=ext_id,
                )
        except Exception as e:
            # Save raw incoming message as a draft even if LLM fails so nothing is lost
            drf = orch.reg.create_inbox_draft(
                channel="whatsapp",
                external_id=ext_id,
                sender_name=sender_name,
                sender_addr=chat_id,
                subject="WhatsApp Message",
                incoming_body=text,
                draft_reply="",
                summary=f"(Auto-draft failed: {e})",
            )
        return {"ok": True, "draft_id": drf["id"]}

    # --- Per-User Personal Bot & Notification Settings (Telegram + WhatsApp) ---

    @app.get(
        "/api/v1/me/channels",
        tags=["My Bots"],
        summary="Get the signed-in user's personal Telegram bot & WhatsApp alert settings",
    )
    def get_my_channels(caller=Depends(require_scope("read"))):
        u = caller.get("user")
        if not u:
            raise HTTPException(status_code=400, detail="Sign in as a user account to configure personal bots.")
        cfg = orch.reg.get_user_channels(u["id"], include_secret=False)
        return {"channels": cfg, "telegram_worker": tg_manager.user_status(u["id"])}

    @app.put(
        "/api/v1/me/channels",
        tags=["My Bots"],
        summary="Save the signed-in user's Telegram bot & WhatsApp schedule notification settings",
    )
    def update_my_channels(req: UserChannelsUpdateRequest, caller=Depends(require_scope("write"))):
        u = caller.get("user")
        if not u:
            raise HTTPException(status_code=400, detail="Sign in as a user account to configure personal bots.")
        cfg = orch.reg.save_user_channels(
            u["id"],
            telegram_bot_token=req.telegram_bot_token,
            clear_telegram_token=req.clear_telegram_token,
            telegram_chat_id=req.telegram_chat_id,
            telegram_enabled=req.telegram_enabled,
            regenerate_link_code=req.regenerate_link_code,
            whatsapp_target=req.whatsapp_target,
            whatsapp_enabled=req.whatsapp_enabled,
        )
        tg_manager.sync_user(u["id"])
        return {"channels": cfg, "telegram_worker": tg_manager.user_status(u["id"])}

    @app.post(
        "/api/v1/me/channels/test-telegram",
        tags=["My Bots"],
        summary="Send a test message through the user's configured Telegram bot",
    )
    def test_my_telegram(caller=Depends(require_scope("write"))):
        u = caller.get("user")
        if not u:
            raise HTTPException(status_code=400, detail="Sign in as a user account.")
        cfg = orch.reg.get_user_channels(u["id"], include_secret=True)
        if not cfg["telegram_bot_token"]:
            raise HTTPException(status_code=400, detail="Save your Telegram bot token first.")
        if not cfg["telegram_chat_id"]:
            raise HTTPException(
                status_code=400,
                detail=f"Telegram chat not linked yet. Send '/link {cfg['telegram_link_code']}' to your bot first.",
            )
        try:
            tg_send_message(
                cfg["telegram_bot_token"],
                cfg["telegram_chat_id"],
                f"Hello {u['username']}! Your agent-hive Telegram bot is connected and ready.",
            )
        except TelegramError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {"sent": True}

    @app.post(
        "/api/v1/me/channels/test-whatsapp",
        tags=["My Bots"],
        summary="Send a test scheduled-task notification to the user's WhatsApp number",
    )
    def test_my_whatsapp(caller=Depends(require_scope("write"))):
        u = caller.get("user")
        if not u:
            raise HTTPException(status_code=400, detail="Sign in as a user account.")
        cfg = orch.reg.get_user_channels(u["id"], include_secret=False)
        target = cfg.get("whatsapp_target", "").strip()
        if not target:
            st = wa_get_status(orch.reg)
            target = str((st.get("me") or {}).get("id") or "").strip()
        if not target:
            raise HTTPException(
                status_code=400,
                detail="Enter your WhatsApp phone number (with country code) or connect WhatsApp in Inbox.",
            )
        try:
            send_whatsapp_message(
                target,
                f"Hello {u['username']}! Your agent-hive WhatsApp schedule notifications are working.",
                registry=orch.reg,
            )
        except WhatsAppError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {"sent": True, "target": target}

    return app


def serve(orch, host="0.0.0.0", port=8000, api_token=None):
    """Run the FastAPI server with Uvicorn."""
    import uvicorn

    app = create_app(orch, api_token=api_token)
    print(
        f"agent-hive Studio & API listening on http://{host}:{port} (GUI: /, Swagger: /docs)",
        flush=True,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
