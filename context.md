# `agent-hive` — Comprehensive Project Context & Architecture Reference

> **Purpose of this document:** This file provides a complete, codebase-verified technical specification of **`agent-hive`** — a self-hosted, multi-model **hierarchical AI agent orchestration platform & Studio**. It documents the live architecture, modules, 14-table SQLite WAL schema, real-time SSE/NDJSON streaming pipeline, Dynamic Tool Creator (`Tool Forge`), multi-user RBAC & token governance, persistent Task Scheduler, REST API, Docker deployment topology, optional modular communication channels (Email, Telegram, WhatsApp), security model, and remaining research/engineering frontiers.

---

## 1. Executive Summary

**`agent-hive`** (GPL-3.0, Python 3.12, **11,778 core LOC**) is a self-hosted, Docker-deployable **multi-model hierarchical AI agent orchestration platform** with a zero-build **Studio Web GUI**, **FastAPI REST API** (61 routes / 48 `/api/v1/*` endpoints), **real-time SSE/NDJSON reasoning & tool-call streaming**, **self-extending Dynamic Tool Creator (`Tool Forge`)**, **persistent Task Scheduler**, **multi-user governance & API key distribution**, and **optional modular I/O channels** (Email, Per-User Telegram Bots, and Personal WhatsApp via WAHA sidecar).

### Core Architectural Highlights
- **Hierarchical Multi-Model Orchestration (`hive/orchestrator.py`, `hive/runner.py`, `hive/llm.py`)**: A manager agent (`Orchestrator`) interprets user goals, discovers or dynamically creates specialized worker agents (`AgentSpec`), decomposes complex tasks into subtasks, and freely mixes OpenAI-compatible models and endpoints (e.g., **DeepSeek V4 / `deepseek-chat`** for orchestration & tool synthesis, **DeepSeek R1 / `deepseek-reasoner`** or local Ollama/vLLM for specialized workers, with automatic endpoint fallback).
- **Real-Time Streaming & Reasoning Trace (`hive/llm.py`, `hive/server.py`, `hive/dashboard.html`)**: Consumes upstream `text/event-stream` SSE tokens (`reasoning_content`, `<think>...</think>` blocks, and content deltas) and emits structured NDJSON events (`status`, `reasoning_delta`, `content_delta`, `done`, `ping`) over `POST /api/v1/chat/stream` and `POST /api/v1/chat/upload/stream`, rendered live in a ChatGPT-style collapsible `Thinking…` / `Thought for Xs` trace widget.
- **Self-Extending Dynamic Tool Creator (`hive/tool_creator.py`, `hive/tools.py`)**: When no built-in tool satisfies a capability, the orchestrator (or a user via **Tool Forge** / Telegram) invokes `create_tool`. The designated flagship model (`[tool_creator]`, default `deepseek-chat`) synthesizes Python code (`def run(**kwargs) -> str`), a flat JSON parameter schema, and unit tests; validates the AST against a strict **import/call safety allowlist**; executes tests in a **scrubbed-env subprocess** with up to 3 self-repair iterations; and permanently persists & hot-registers the verified tool into `ToolLibrary`.
- **Modern Glassmorphic Studio SPA & Multi-User Governance (`hive/dashboard.html`, `hive/registry.py`, `hive/server.py`)**: Zero-build single-file SPA with collapsible sidebar, Dark/Light theme persistence, drag-and-drop `.md`/`.txt`/`.csv` uploads, offline Markdown/CSV workspace viewer (Preview / Split / Raw-Edit), fail-closed PBKDF2 + HttpOnly session & SHA-256 API key auth, forced first-login admin credential reset, per-user token usage quotas (`429` enforcement), user restrict/revoke controls, live persisted model switching, and scoped API key client kits.
- **Persistent Task Scheduler (`hive/scheduler.py`)**: Background daemon supporting repeating intervals (5m–1h and 4h–24h) and timezone-aware daily schedules with weekday filters, crash recovery, per-user quota checks, execution audit logs (`schedule_runs`), and optional delivery to Telegram or WhatsApp.
- **Optional Modular Communication Channels (`hive/channels/`)**: Pluggable I/O adapters allowing users to operate `agent-hive` or automate inbox workflows over **Email (IMAP/SMTP)**, **Per-User Telegram Bots** (long-polling), and an optional **Personal WhatsApp (`waha` NOWEB sidecar)** with per-chat grouped sync, configurable debounce wait (seconds/minutes), customizable context window (1–100 messages), and per-chat auto-reply & persona context.
- **Full Persistence Across Restarts**: All platform state lives in `/data` (`hive_data` Docker volume): `/data/agents.db` (SQLite WAL, 14 tables), `/data/workspace/`, and `/data/custom_tools/`, plus `waha_sessions` when the optional WhatsApp profile is enabled.

---

## 2. System Architecture

```text
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                               Clients & Entrypoints                                    │
│  • Modern Studio Web SPA (/) — Streaming Chat + Thinking Trace + .md/.txt/.csv Upload, │
│    Agents, Tool Forge, Workspace (MD/CSV Preview/Split/Edit), Scheduler, Inbox,        │
│    My Bots, Admin Dashboard, API Playground (Dark/Light Glassmorphic UI)               │
│  • REST API (/api/v1/*, 48 endpoints) + Swagger (/docs) + ReDoc (/redoc) + OpenAPI     │
│  • Interactive CLI & API Key Manager (main.py — zero-pip local CLI via lazy imports)   │
│  • Optional Modular Channels: Per-User Telegram Bots, Email (IMAP/SMTP), WhatsApp      │
└────────────────────────────────────┬───────────────────────────────────────────────────┘
                                     │
                                     ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                         FastAPI Control Plane (hive/server.py)                         │
│  • Unified Fail-Closed Auth: HttpOnly session cookies, SHA-256 API keys, Master token  │
│  • RBAC Scopes (read / write / admin) & Per-User Token Quota Enforcement (HTTP 429)    │
│  • Real-Time Streaming: /api/v1/chat/stream & /api/v1/chat/upload/stream (NDJSON)      │
│  • Lifespan Daemons: TaskScheduler + TelegramBotManager + WhatsAppSyncManager          │
└───────────┬────────────────────────┬───────────────────────────┬───────────────────────┘
            │                        │                           │
            ▼                        ▼                           ▼
┌──────────────────────┐  ┌────────────────────┐  ┌──────────────────────────────────────┐
│ Orchestrator         │  │ TaskScheduler      │  │ Modular I/O Channels (hive/channels/)│
│ (orchestrator.py)    │  │ (scheduler.py)     │  │ • email_client.py (IMAP/SMTP stdlib) │
│ • list/create_agent  │  │ • 5m–1h, 4h–24h,   │  │ • telegram_bot.py (per-user polling) │
│ • assign_task        │  │   & daily HH:MM    │  │ • whatsapp_client.py (optional WAHA  │
│ • list/create_tool   │  │ • Quota-checked    │  │   NOWEB sidecar + grouped sync)      │
│ • recent_tasks       │  │ • Audit log +      │  │ • formatting.py (MD → TG HTML & WA)  │
│ • Fallback routing   │  │   channel push     │  └──────────────────┬───────────────────┘
└───────────┬──────────┘  └──────────┬─────────┘                     │
            │                        │                               │
            ├─────────► ToolCreator (tool_creator.py)                │
            │           • Routes to flagship model (deepseek-chat)   │
            │           • AST allowlist + scrubbed unit test runner  │
            │           • Auto-repair (3x) → persist & hot-register  │
            ▼                                                        ▼
┌─────────────────────────────────────┐    ┌─────────────────────────────────────────────┐
│ Shared ReAct Loop & Streaming LLM   │    │ ToolLibrary (hive/tools.py)                 │
│ • runner.py: ReAct loop, schema     │    │ • Built-in: web_fetch, read/write/list_file,│
│   validation, error-as-observation, │    │   list_unread_emails, summarize_important_  │
│   max_steps cap, live status emits  │    │   emails, batch_prepare_email_drafts,       │
│ • llm.py: OpenAI/DeepSeek SSE &     │    │   [optional: run_python]                    │
│   non-stream HTTP, reasoning_content│    │ • Custom: dynamically synthesized tools     │
│   & <think> parsing, text-call      │    │   executed in scrubbed-env subprocesses     │
│   recovery, token usage accumulator │    │   with workspace-confined I/O helpers       │
└──────────────────┬──────────────────┘    └──────────────────────┬──────────────────────┘
                   │                                              │
                   ▼                                              ▼
┌─────────────────────────────────────┐    ┌─────────────────────────────────────────────┐
│ Persistent Volume (/data)           │    │ Optional Sidecar (docker-compose profile)   │
│ • /data/agents.db (SQLite WAL)      │    │ • waha (devlikeapro/waha:latest, NOWEB)     │
│   14 tables: agents, tasks, users,  │    │ • Volume: waha_sessions:/app/.sessions      │
│   sessions, chat_messages, api_keys,│    │ • Webhook → agent-hive:8000/api/v1/webhooks │
│   settings, scheduled_tasks,        │    └─────────────────────────────────────────────┘
│   schedule_runs, custom_tools,      │
│   inbox_drafts, whatsapp_messages,  │
│   whatsapp_chat_settings,           │
│   user_channels                     │
│ • /data/workspace/ (uploads, output)│
│ • /data/custom_tools/*.py           │
└─────────────────────────────────────┘
```

---

## 3. Repository & Module Inventory

Total core codebase size is **11,778 LOC** across 18 source/config files (plus `hive/favicon.svg`, `tests/mock_server.py`, and `API_DOCUMENTATION.md` = **12,316 LOC** total).

| File | LOC | Responsibility |
|---|---:|---|
| `main.py` | 297 | Interactive CLI REPL, `key create/list/rotate/revoke` manager, `--serve` FastAPI entrypoint, `.env` loader & env overrides |
| `hive/server.py` | 2,161 | FastAPI REST API (61 routes / 48 `/api/v1/*`), Auth/RBAC, NDJSON streaming chat, `.md`/`.txt`/`.csv` upload validation, quotas, Scheduler, Tool Forge, Workspace, Admin, and optional Channel endpoints |
| `hive/registry.py` | 1,462 | Thread-safe SQLite WAL persistence (14 tables), PBKDF2-HMAC-SHA256 hashing, SHA-256 API keys/sessions, schema migrations, quotas, channel & chat settings |
| `hive/orchestrator.py` | 528 | Manager agent, meta-tools (`list_agents`, `list_tools`, `create_tool`, `create_agent`, `assign_task`, `recent_tasks`), multi-model fallback routing, Markdown output archival, and channel AI reply/digest workflows |
| `hive/tool_creator.py` | 342 | Dynamic Tool Creator (`Tool Forge`): flagship model synthesis, static AST import/call allowlist, scrubbed-env unit test runner & 3x self-repair loop |
| `hive/scheduler.py` | 281 | Persistent background daemon scheduler (5m–1h, 4h–24h, timezone-aware daily triggers), crash recovery, quota checks, Telegram & multi-part WhatsApp delivery |
| `hive/tools.py` | 258 | Built-in vetted `ToolLibrary` + dynamic custom tool loading/hot-registration, path-traversal guard, 6,000-char output clipping, scrubbed `run_python` |
| `hive/llm.py` | 302 | Stdlib OpenAI/DeepSeek HTTP client supporting both blocking and live SSE (`text/event-stream`), thread-local stream emitters (`reasoning_delta`, `content_delta`, `status`), `<think>` & `reasoning_content` parsing, JSON text-call recovery, and thread-local token accounting |
| `hive/runner.py` | 113 | Shared ReAct tool-use loop, flat JSON schema validation (unknown-key stripping), error-as-observation retry, `max_steps` cap, live tool-call stream status events |
| `hive/channels/whatsapp_client.py` | 866 | Optional WAHA `NOWEB` sidecar client & `WhatsAppSyncManager`: QR proxy, `@lid`→`@c.us` canonicalization, `@g.us` group subject resolution, buffered per-chat sync with customizable wait (sec/min) & context length (1–100 msgs), multi-unread aggregation, auto-reply, and multi-part report delivery |
| `hive/channels/telegram_bot.py` | 617 | Multi-user Telegram long-polling bot manager (`TelegramBotManager`), `/link` pairing, 18+ slash commands & `.md`/`.txt`/`.csv` document router |
| `hive/channels/formatting.py` | 275 | GitHub-flavored Markdown converters → Telegram HTML (`parse_mode="HTML"`) and native WhatsApp formatting (`*bold*`, `_italic_`, bullet tables) |
| `hive/channels/email_client.py` | 223 | Stdlib IMAP/SMTP client: unread scan over last `N` emails, newsletter/automated detection, HTML stripping, threaded `In-Reply-To` / `References` SMTP replies |
| `hive/dashboard.html` | 3,893 | Zero-build glassmorphic Studio SPA (collapsible sidebar, Dark/Light theme, streaming Chat + smooth `.thought-collapse` trace + sticky scroll, Agents, Tool Forge, Workspace MD/CSV preview/edit, Scheduler, Inbox, My Bots, Admin Dashboard, API Playground, glass modals) |
| `Dockerfile` / `docker-compose.yml` | 120 | Non-root `python:3.12-slim` image (34 LOC) + Compose topology (86 LOC): `HEALTHCHECK`, `develop.watch`, optional `whatsapp` (`waha`) & `cli` profiles |
| `config.toml.example` | 37 | Multi-endpoint model configuration template (`[endpoints.*]`, `[orchestrator]`, `[worker_default]`, `[tool_creator]`) |
| `requirements.txt` | 3 | Minimal runtime dependencies: `fastapi`, `uvicorn[standard]`, `python-multipart` |
| `hive/favicon.svg` | 20 | Honeycomb/hive SVG favicon served at `/favicon.svg` and `/favicon.ico` |
| `tests/mock_server.py` | 71 | Local scripted OpenAI-compatible mock server for API-key-free smoke testing |
| `API_DOCUMENTATION.md` | 447 | REST API reference, RBAC scopes, key distribution guide, status codes, and client examples |

---

## 4. Detailed Subsystem Specifications

### 4.1 LLM Client, Real-Time SSE Streaming & ReAct Loop (`hive/llm.py`, `hive/runner.py`)
- **OpenAI-Compatible Protocol**: `POST {base_url}/chat/completions` using Python's `urllib.request` (600s timeout). Works with DeepSeek, OpenAI, Ollama (`/v1`), vLLM, LM Studio, or any OpenAI-compatible proxy.
- **Live SSE Streaming (`_consume_sse_response`)**: When a thread-local emitter is active (`start_stream_emitter(callback)`), `hive/llm.py` sends `stream: true`, parses `data: {...}` SSE chunks incrementally, streams `reasoning_content` and inline `<think>...</think>` tags as `reasoning_delta` events, streams assistant text as `content_delta` events, and reconstructs streamed `tool_calls` deltas seamlessly.
- **Reasoning Model Support**: Automatically omits `temperature` when `"reasoner"` or `"-r1"` appears in the model ID; preserves and echoes `reasoning_content` across multi-turn tool calls; strips `<think>...</think>` blocks from final answers while retaining them for the live trace.
- **Small-Model Resilience (`hive/runner.py`)**: Recovers tool calls emitted as raw JSON (`{"name": ..., "arguments": ...}`) in assistant text; strips unknown extra tool arguments; feeds JSON/schema/runtime errors back as `"ERROR: ..."` tool observations; emits live `status` events when invoking each tool; and enforces `max_steps = 8` with a forced final-answer turn.
- **Token Usage Telemetry**: Thread-local `start_usage_tracking()` / `finish_usage_tracking()` accumulates exact `prompt_tokens`, `completion_tokens`, and `total_tokens` across nested orchestrator + worker + tool-creator calls (falling back to character-based estimation when local servers omit `usage`).

### 4.2 Hierarchical Orchestrator & Worker Delegation (`hive/orchestrator.py`)
- **Manager-Worker Pattern**: The `Orchestrator` operates over a curated set of meta-tools: `list_agents`, `list_tools`, `create_tool`, `create_agent`, `assign_task`, and `recent_tasks`.
- **Dynamic Worker Creation**: Workers are persisted as `AgentSpec` records (`name`, `description`, `system_prompt`, allowed `tools`, `endpoint`, `model`) in SQLite and can be reused across sessions, chats, API calls, and scheduled jobs.
- **Multi-Model Routing & Fallback**: Supports distinct endpoints/models for the orchestrator, default workers, and tool creator (plus live runtime overrides persisted in `settings`), with automatic fallback if a primary endpoint fails.
- **Output Archival**: Long-form structured deliverables can be written directly into `/data/workspace/` for preview/download in the Workspace Explorer.

### 4.3 Dynamic Tool Creator / Tool Forge (`hive/tool_creator.py`, `hive/tools.py`)
- **Flagship Model Routing**: Configurable via `[tool_creator]` in `config.toml` (defaults to `v4` → `deepseek-chat`).
- **Synthesis Contract**: Prompts the flagship model to emit strict JSON `{name, description, parameters, code, tests}` where `code` defines `def run(**kwargs) -> str`.
- **Static AST Safety Guard (`check_ast_safety`)**:
  - Enforces an explicit stdlib allowlist (`math`, `statistics`, `json`, `csv`, `re`, `datetime`, `collections`, `itertools`, `hashlib`, `xml.etree.ElementTree`, `difflib`, etc.).
  - Blocks `os`, `sys`, `subprocess`, `socket`, `shutil`, `ctypes`, `urllib.request`, `eval`, `exec`, `compile`, `__import__`, direct `open()`, dangerous attributes (`system`, `popen`, `environ`, `rmtree`, etc.), and dunder escapes (`__class__`, `__globals__`, `__subclasses__`, etc.).
  - Injects workspace-confined helpers (`ws_read(rel_path)`, `ws_write(rel_path, content)`, `ws_list()`) into the execution harness.
- **Verification & Self-Repair Loop**: Runs generated unit tests in an isolated temp workspace inside a scrubbed-env subprocess (15s timeout, no API keys in `env`). On AST, schema, or test failure, feeds exact tracebacks back to the synthesis model up to `max_repair_attempts = 3`.
- **Persistence & Hot-Registration**: Verified tools are saved to SQLite (`custom_tools`) and `/data/custom_tools/<name>.py`, hot-registered into `ToolLibrary` immediately for same-turn worker delegation, tracked via `use_count`, protected from overwriting built-ins, and auto-reloaded on startup.

### 4.4 Multi-User Auth, RBAC, Token Quotas & API Key Distribution (`hive/registry.py`, `hive/server.py`)
- **Fail-Closed Authentication**: Accepts HttpOnly session cookies (`hive_session`), `Authorization: Bearer <token>`, or `X-API-Key: <token>` (`HIVE_API_TOKEN` master key, scoped `hive_sk_...` API keys, or `hive_sess_...` session tokens).
- **Bootstrap Admin Security**: On first boot with no admin account, generates `admin_<6hex>` + a random password logged to stdout (`docker logs`), sets `must_reset_credentials = 1`, and rejects all other endpoints (`403 MUST_RESET_CREDENTIALS`) until `POST /api/v1/auth/reset-credentials` sets a new username and password.
- **User Governance & Token Accounting**: Admins can set accounts to `active`, `restricted` (blocks writes & LLM calls with custom reason), or `revoked` (invalidates all sessions immediately & blocks login); configure per-user `token_limit` (`429` on exhaustion) and `default_user_token_limit`; toggle `allow_signup`; and switch active models live (persisted in SQLite `settings`).
- **Scoped API Keys**: Stored strictly as SHA-256 hashes (`api_keys`) with `read` / `write` / `admin` scopes, expiry dates, usage counters, rotation, revocation, and one-click distribution kits (`curl`, Python, JavaScript, `.env`, shareable Markdown, downloadable JSON).

### 4.5 Modern Studio SPA, File Uploads & Workspace Viewer (`hive/dashboard.html`, `hive/server.py`)
- **Glassmorphic Studio Interface (3,893 LOC, zero build step)**:
  - Collapsible sidebar organized into **Workspace** (Chat, Agents, Tool Forge, Files), **Intelligence & Ops** (Scheduler, Inbox, My Bots), and **Platform** (Admin, API Playground).
  - Sticky topbar with live health/model/quota pills and persisted Dark/Light theme toggle (`localStorage`).
  - Streaming chat experience with starter prompt hero cards, auto-growing composer, and a smooth CSS grid-row `.thought-collapse` widget (`Thinking…` → `Thought for Xs`) with pinned sticky-scroll (`scrollPanelIfPinned`, `scrollChatIfPinned`, `overscroll-behavior: contain`) so users can scroll up freely without being yanked back to the bottom.
  - Non-blocking animated glass modal dialogs (`showModal`) replacing native browser alerts/confirms.
- **Strict File Upload Policy**: `POST /api/v1/chat/upload` and `POST /api/v1/chat/upload/stream` (plus Telegram document uploads) accept **`.md`, `.markdown`, `.txt`, and `.csv`** UTF-8 text files only (max 2 MB each, max 5 files per message; binary/null-byte and disallowed extensions rejected with `400`). Files persist under `/data/workspace/uploads/<username>/`.
- **Workspace Explorer**: Offline Markdown renderer (headings, tables, fenced code blocks, blockquotes, lists, safe links) and CSV table viewer (up to 500 rows) with **Preview / Split / Raw-Edit** modes and file download.

### 4.6 Persistent Task Scheduler (`hive/scheduler.py`)
- **Trigger Modes**:
  - Repeating intervals: `5, 10, 15, 20, 30, 45, 60` min (5m–1h) and `240, 480, 720, 960, 1200, 1440` min (4h increments).
  - Timezone-aware daily schedules (`HH:MM`) with `days_of_week` (`0`=Sun..`6`=Sat) and `timezone_offset_minutes`.
- **Execution & Auditability**: Daemon polls every 5s, recovers stuck `running` jobs on restart, enforces owner account status & token quotas before execution (`skipped` if exhausted/restricted), records every execution in `schedule_runs`, and optionally delivers completed outputs to the owner's Telegram Bot (`notify_telegram`) and/or WhatsApp (`notify_whatsapp`).

### 4.7 Optional Modular Communication Channels (`hive/channels/`)
Communication channels are modular I/O plugins on top of the core orchestration platform:
- **Email (`email_client.py`)**:
  - Configurable via `.env` or persistently in SQLite (`PUT /api/v1/inbox/email/config`).
  - `GET /api/v1/inbox/email/unread?refresh=false|true`: Scans unread mail from the last `N` (default 100) messages and caches snapshots + timestamps.
  - `summarize_important_emails` & `batch_prepare_email_drafts` tools/endpoints: Filters newsletters/automated alerts, produces prioritized Markdown briefings (saved in `workspace/output/email_digest/`), and batch-generates editable reply drafts in `inbox_drafts` with threaded `In-Reply-To` / `References` SMTP send.
- **Per-User Telegram Bots (`telegram_bot.py`)**:
  - Each user configures their own `@BotFather` token in **My Bots** (`/api/v1/me/channels`) and pairs via one-time `/link <CODE>`.
  - `TelegramBotManager` runs one long-polling thread per enabled user (no public HTTPS webhook needed) and exposes full platform control via plain chat, `.md`/`.txt`/`.csv` uploads, and 18+ slash commands (`/status`, `/agents`, `/run`, `/tools`, `/create_tool`, `/schedules`, `/schedule`, `/run_schedule`, `/pause_schedule`, `/resume_schedule`, `/del_schedule`, `/files`, `/read`, `/tasks`, `/emails`, `/draft_emails`, `/drafts`, `/send_draft`, `/reset`, `/help`) rendered via `md_to_telegram_html()`.
- **Optional Personal WhatsApp (`whatsapp_client.py` + `waha` Compose profile)**:
  - Runs `devlikeapro/waha:latest` (`NOWEB` lightweight WebSocket engine) with QR PNG pairing in the Studio GUI (`GET /api/v1/inbox/whatsapp/qr`) and `@lid`→`@c.us` canonicalization (`remoteJidAlt`) + `@g.us` group subject resolution.
  - Incoming webhook (`POST /api/v1/webhooks/whatsapp`) buffers messages into `whatsapp_messages` (`processed = 0`) instead of firing prematurely on single-line bursts.
  - `WhatsAppSyncManager` (5s poll loop) groups unread messages per chat and waits for each chat's configurable quiet window (`wait_seconds`, customizable in seconds or minutes, default `120s`, with starvation cap `max(3*wait_s, wait_s+300)`). Once ready, it fetches the chat's customizable context window (`history_limit`: **1–100 messages**, default `20`) plus **all** pending unread messages, injects the chat's optional persona `context` (`whatsapp_chat_settings`), and either generates a single combined draft or sends directly if per-chat `auto_reply` is enabled.
  - Supports structured multi-part scheduled report delivery (`Header` → `Part i/N` ≤ 2,800 chars → `END OF REPORT`) formatted via `md_to_whatsapp()`.

### 4.8 SQLite Schema Summary (`/data/agents.db`, WAL Mode — 14 Tables)
1. `agents` — worker specs (`name`, `description`, `system_prompt`, `tools`, `endpoint`, `model`, `created_at`)
2. `tasks` — worker execution history (`id`, `agent`, `task`, `status`, `result`, `created_at`, `finished_at`)
3. `users` — accounts, PBKDF2 hashes, roles, status (`active`/`restricted`/`revoked`), `must_reset_credentials`, `token_limit`, `tokens_used`, `prompt_tokens`, `completion_tokens`, `request_count`, `restriction_reason`
4. `sessions` — SHA-256 hashed HttpOnly session cookies (`token_hash`, `user_id`, `created_at`, `expires_at`)
5. `chat_messages` — per-user persistent chat history (`id`, `user_id`, `role`, `content`, `files_json`, `logs_json`, `tokens_used`, `created_at`)
6. `api_keys` — SHA-256 hashed scoped API keys (`id`, `name`, `key_prefix`, `key_hash`, `scopes`, `created_at`, `expires_at`, `last_used_at`, `request_count`, `revoked`)
7. `settings` — JSON key-value store (`allow_signup`, `default_user_token_limit`, persisted model overrides, `email_config`, caches)
8. `scheduled_tasks` — recurring jobs (`id`, `user_id`, `owner_name`, `name`, `target_type`, `agent_name`, `prompt`, `trigger_type`, `interval_minutes`, `daily_time`, `days_of_week`, `timezone_offset_minutes`, `enabled`, `next_run_at`, `last_run_at`, `last_status`, `last_result`, `last_error`, `run_count`, `notify_telegram`, `notify_whatsapp`)
9. `schedule_runs` — per-execution audit log & token usage for scheduled jobs
10. `custom_tools` — dynamically synthesized tools (`name`, `description`, `schema_json`, `code`, `tests`, `created_by_model`, `created_by_user`, `use_count`, `created_at`, `updated_at`)
11. `inbox_drafts` — unified editable Email & WhatsApp reply queue (`id`, `channel`, `external_id`, `sender_name`, `sender_addr`, `subject`, `in_reply_to`, `incoming_body`, `priority`, `summary`, `draft_subject`, `draft_reply`, `status`, `error`, `created_at`, `updated_at`, `sent_at`)
12. `whatsapp_messages` — buffered incoming WhatsApp messages (`id`, `chat_id`, `chat_name`, `is_group`, `sender_name`, `body`, `processed`, `created_at`)
13. `whatsapp_chat_settings` — per-chat WhatsApp sync & automation settings (`chat_id`, `chat_name`, `is_group`, `auto_reply`, `wait_seconds`, `history_limit`, `context`, `last_synced_sig`, `last_synced_at`, `last_auto_reply_at`, `auto_reply_count`, `updated_at`)
14. `user_channels` — per-user Telegram bot tokens, chat IDs, link codes, and WhatsApp notification targets

---

## 5. Deployment & Operations

- **Container Topology (`docker-compose.yml`)**:
  - `agent-hive`: Non-root (`uid 1000`), exposes internal port `8000` mapped to `${HIVE_PORT:-8000}`, mounts `hive_data:/data`, includes `HEALTHCHECK` on `/health`, `init: true`, `no-new-privileges:true`, `host.docker.internal` mapping for host Ollama/vLLM, and `develop.watch` (`sync+restart` on `hive/`, `main.py`, `config.toml.example`; `rebuild` on `requirements.txt`, `Dockerfile`).
  - `waha` (`profiles: ["whatsapp"]`): Optional `devlikeapro/waha:latest` (`NOWEB` engine), mounts `waha_sessions:/app/.sessions`, shares `WAHA_API_KEY` with `agent-hive`.
  - `cli` (`profiles: ["cli"]`): Interactive REPL sharing `hive_data:/data`.
- **Commands**:
  - Standard deploy: `docker compose up -d --build`
  - With optional WhatsApp sidecar: `docker compose --profile whatsapp up -d --build`
  - Live dev reload: `docker compose up --watch`
  - Interactive CLI: `docker compose run --rm cli`
  - Mint API key via CLI: `docker compose exec agent-hive python main.py key create "client" --scopes read,write --days 30`

---

## 6. Current Limitations & Research / Enhancement Directions

With the core multi-model orchestration platform, Dynamic Tool Creator, real-time SSE/NDJSON streaming, Studio GUI, and modular channels implemented, the primary open engineering and research frontiers are:

1. **OS / MicroVM / WASM Sandboxing for Synthesized Tools & `run_python`**:
   - Synthesized tools currently rely on static AST allowlisting + scrubbed-env subprocesses inside the `agent-hive` container (no `rlimit` CPU/RAM caps, no kernel namespace isolation, and shared container filesystem with `/data/agents.db`).
   - Next-phase upgrade: Ephemeral container, `bubblewrap` (`bwrap`), Firecracker MicroVM, or WebAssembly (`wasmtime` / Pyodide) execution sandboxes with strict memory/CPU/network quotas and read-only mounts.
2. **Semantic Tool Retrieval, Deduplication & Toolbox Pruning**:
   - All tools in `ToolLibrary` are currently injected linearly into `ORCHESTRATOR_PROMPT`. As synthesized tool libraries grow to dozens or hundreds of tools, prompt token overhead increases and tool selection accuracy degrades.
   - Next-phase upgrade: Hybrid BM25/embedding tool retrieval (inspired by **Voyager** / **CRAFT**), automated deduplication & generalization (**TroVE**), tool versioning, and runtime self-healing on recurring failures.
3. **Per-User Concurrency & Parallel Async Worker Fan-Out**:
   - Real-time SSE/NDJSON streaming is now implemented, but a global `op_lock` still serializes concurrent orchestrator/worker turns across users and subtasks.
   - Next-phase upgrade: Per-user concurrency locks, lock-free read paths, and parallel `asyncio` DAG fan-out across independent worker subtasks.
4. **Human-in-the-Loop Tool Approval & Security Auditing**:
   - Adding an optional admin review workflow in **Tool Forge** (`pending_approval` → inspect AST audit report, source code, and unit test transcript → `approved`) before newly synthesized tools become available to non-admin users.
5. **Model Context Protocol (MCP) Interoperability**:
   - Exposing `agent-hive`'s built-in + synthesized tools as an MCP server and enabling worker agents to consume external MCP tool servers for standardized ecosystem integration.
6. **Empirical Evaluation & Cost-Amortization Benchmarks**:
   - Systematic evaluation comparing (a) static built-in tools, (b) ad-hoc `run_python`, and (c) `agent-hive`'s persistent flagship Tool Creator + reasoning/local Tool Users across `pass@1` accuracy, tool reuse rate, self-repair convergence, prompt-injection/sandbox red-teaming, and token cost amortized over $N$ recurring tasks.

---

## 7. Related Literature & Reference Systems

- **LATM — *Large Language Models as Tool Makers*** (Cai et al., ICLR 2024): Strong Tool Maker (`deepseek-chat`) + cheaper/specialized Tool User (`deepseek-reasoner`/local) with cached reusable Python tools.
- **CREATOR** (Qian et al., EMNLP 2023): Tool creation, decision, execution, and iterative rectification loop.
- **Voyager** (Wang et al., 2023): Iterative self-verification + persistent reusable Skill Library.
- **CRAFT** (Yuan et al., ICLR 2024) & **TroVE** (Wang et al., ICML 2024): Toolbox induction, deduplication, retrieval, and pruning.
- **CodeAct / Hugging Face `smolagents`**: AST-checked Python execution & sandboxed agent runtimes.
- **Agent Control Planes & Studios**: AutoGen Studio, LangGraph Studio, Dify, Flowise, OpenHands, CrewAI.
- **Sandboxing & Isolation**: `ast.NodeVisitor`, `RestrictedPython`, `resource.setrlimit`, `bubblewrap` (`bwrap`), gVisor, Firecracker, WebAssembly (`wasmtime` / Pyodide), MCP servers.
- **Indirect Prompt Injection Defenses** (for external-content & channel agents): Spotlighting, CaMeL, least-privilege tool scoping, and human-in-the-loop draft approval queues.
