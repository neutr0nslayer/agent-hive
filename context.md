# `agent-hive` — Comprehensive Project Context & Proposal Reference

> **Purpose of this document:** This file provides a complete, up-to-date, self-contained technical specification of the **`agent-hive`** platform — its implemented architecture, modules, SQLite schema, REST API, Studio GUI, Dynamic Tool Creator, multi-channel integrations (Email, Personal WhatsApp via WAHA, Per-User Telegram Bots), Docker deployment topology, security model, and remaining research/enhancement directions. Feed this document to an LLM when drafting a project proposal, surveying literature, comparing reference implementations, or designing next-phase experiments.

---

## 1. Executive Summary

**`agent-hive`** (GPL-3.0, Python 3.12) is a self-hosted, Docker-deployable **multi-model hierarchical AI agent orchestration platform** with a built-in **Studio GUI**, **FastAPI REST API**, **self-extending Dynamic Tool Creator (`Tool Forge`)**, **persistent Task Scheduler**, and **multi-channel messaging integrations** (Email, Personal WhatsApp, and Per-User Telegram Bots).

### Core Architectural Highlights
- **Hierarchical Multi-Model Orchestration**: A manager agent (`Orchestrator`) interprets requests, discovers/creates specialized worker agents (`AgentSpec`), decomposes work into sequential subtasks, and freely mixes OpenAI-compatible models/endpoints (e.g. **DeepSeek V4 / `deepseek-chat`** for orchestration & tool synthesis, **DeepSeek R1 / `deepseek-reasoner`** or local Ollama/vLLM for workers, with automatic endpoint fallback).
- **Self-Extending Dynamic Tool Creator (`hive/tool_creator.py`)**: When no existing tool fits a task, the orchestrator (or a user via **Tool Forge** / Telegram) invokes `create_tool`. The flagship model (`deepseek-chat` / `[tool_creator]`) synthesizes Python code, a flat JSON parameter schema, and unit tests; validates the code via an **AST import/call safety allowlist**; runs automated tests in a **scrubbed-env subprocess** with up to 3 self-repair iterations; and permanently persists and hot-registers the verified tool for immediate and future reuse.
- **Multi-User Studio GUI & Governance (`hive/server.py`, `hive/dashboard.html`)**: Fail-closed authentication (PBKDF2-HMAC-SHA256 + SQLite HttpOnly sessions), auto-generated bootstrap admin credentials printed in `docker logs` with mandatory first-login username/password reset, Admin User Governance (revoke, restrict with reason, activate, per-user token usage quotas, global signup toggle, live persisted model switching), scoped API key generation & client kit distribution, and full Markdown/CSV workspace preview.
- **Persistent Task Scheduler (`hive/scheduler.py`)**: Supports short intervals (5m–1h), 4-hour increments (4h–24h), and timezone-aware daily times with weekday selection, run history, token billing, and automatic delivery to Telegram and/or multi-part WhatsApp reports.
- **Multi-Channel Automation (`hive/channels/`)**:
  - **Email (IMAP/SMTP)**: Scans unread messages from the last 100 emails, persists fetched unread snapshots + last-fetch timestamps, produces prioritized Markdown briefings of important unread mail, and batch-prepares editable reply drafts for inline edit & threaded SMTP send.
  - **Personal WhatsApp (WAHA `NOWEB` Sidecar)**: QR code pairing directly inside the Studio GUI, incoming webhook auto-drafting, unread chat sync (`noweb.store`), and structured multi-message report delivery (Header + Chunked Body Parts + End Marker) using native WhatsApp formatting.
  - **Per-User Telegram Bots**: Each user configures their own `@BotFather` bot token + one-time `/link <CODE>` pairing (long-polling, no public HTTPS domain needed) to operate **all** of `agent-hive` directly from Telegram with native HTML formatting and `.md`/`.txt`/`.csv` document uploads.
- **Full Persistence Across `docker restart`**: All state lives in `/data` (`hive_data` Docker volume): `/data/agents.db` (SQLite WAL), `/data/workspace/`, and `/data/custom_tools/`, plus `waha_sessions` for WhatsApp pairing.

---

## 2. System Architecture

```text
┌──────────────────────────────────────────────────────────────────────────────────────┐
│                              Clients & Entrypoints                                   │
│  • Studio Web GUI (/) — Chat + .md/.txt/.csv upload, Inbox, Agents, Tool Forge,      │
│    Workspace (MD/CSV preview), Scheduler, My Bots, Admin Dashboard, API Playground   │
│  • REST API (/api/v1/*) + Swagger (/docs) + ReDoc (/redoc) + OpenAPI (/openapi.json) │
│  • Per-User Telegram Bots (long-polling, full command & chat control, HTML format)   │
│  • Interactive CLI & Key Manager (main.py, zero-pip local CLI via lazy imports)      │
└───────────────────────────────┬──────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────────────────────┐
│                       FastAPI Application (hive/server.py)                           │
│  • Unified Fail-Closed Auth: HttpOnly session cookies, API keys (SHA-256), Master    │
│  • RBAC Scopes (read / write / admin) & Per-User Token Quota Enforcement (429)       │
│  • Lifespan Hooks: starts/stops TaskScheduler + TelegramBotManager                   │
└──────────┬────────────────────┬────────────────────┬─────────────────────────────────┘
           │                    │                    │
           ▼                    ▼                    ▼
┌─────────────────────┐ ┌──────────────────┐ ┌─────────────────────────────────────────┐
│ Orchestrator        │ │ TaskScheduler    │ │ Communication Channels (hive/channels/) │
│ (orchestrator.py)   │ │ (scheduler.py)   │ │ • email_client.py (IMAP/SMTP stdlib)    │
│ • list/create_agent │ │ • 5m–1h, 4h step │ │ • whatsapp_client.py (WAHA NOWEB QR +   │
│ • assign_task       │ │   & daily HH:MM  │ │   multi-part report delivery)           │
│ • list/create_tool  │ │ • Quota-checked  │ │ • telegram_bot.py (multi-user polling)  │
│ • Email summary &   │ │ • TG/WA delivery │ │ • formatting.py (MD → TG HTML & WA)     │
│   batch draft flows │ └────────┬─────────┘ └──────────────────┬──────────────────────┘
└──────────┬──────────┘          │                              │
           │                     ▼                              │
           ├────────► ToolCreator (tool_creator.py)             │
           │          • Routes to flagship model (deepseek-chat)│
           │          • AST allowlist + scrubbed unit test loop │
           │          • Auto-repair (3x) → persist & hot-load   │
           ▼                                                    ▼
┌────────────────────────────────────┐   ┌─────────────────────────────────────────────┐
│ Shared Tool Loop & LLM Client      │   │ ToolLibrary (hive/tools.py)                 │
│ • runner.py: ReAct loop, schema    │   │ • Built-in: web_fetch, read/write/list_file,│
│   check, error-as-observation,     │   │   list_unread_emails, summarize_important_  │
│   max_steps cap                    │   │   emails, batch_prepare_email_drafts,       │
│ • llm.py: OpenAI/DeepSeek HTTP,    │   │   [optional: run_python]                    │
│   reasoning_content, <think> strip,│   │ • Custom: dynamically synthesized tools     │
│   text-call recovery, token usage  │   │   executed in scrubbed subprocesses         │
└──────────────────┬─────────────────┘   └──────────────────────┬──────────────────────┘
                   │                                            │
                   ▼                                            ▼
┌────────────────────────────────────┐   ┌─────────────────────────────────────────────┐
│ Persistent Volume (/data)          │   │ Optional Sidecar (docker-compose profile)   │
│ • /data/agents.db (SQLite WAL)     │   │ • waha (devlikeapro/waha:latest, NOWEB)     │
│   11 tables: agents, tasks, users, │   │ • Volume: waha_sessions:/app/.sessions      │
│   sessions, chat_messages,         │   │ • Webhook → agent-hive:8000/api/v1/webhooks │
│   api_keys, settings,              │   └─────────────────────────────────────────────┘
│   scheduled_tasks, schedule_runs,  │
│   custom_tools, inbox_drafts,      │
│   whatsapp_messages, user_channels │
│ • /data/workspace/ (uploads, out)  │
│ • /data/custom_tools/*.py          │
└────────────────────────────────────┘
```

---

## 3. Repository & Module Inventory

Total codebase size is ~9,790 lines across Python backend, single-file Studio SPA, Docker configs, and documentation.

| File | LOC | Responsibility |
|---|---:|---|
| `main.py` | 297 | Interactive CLI, `key create/list/rotate/revoke` manager, `--serve` FastAPI entrypoint, `.env` loader & env overrides |
| `hive/server.py` | 1,929 | FastAPI REST API (50+ endpoints), Auth/RBAC, `.md`/`.txt`/`.csv` upload validation, quotas, Inbox, Scheduler, Tool Forge, My Bots |
| `hive/registry.py` | 1,211 | Thread-safe SQLite WAL persistence (13 tables), PBKDF2 password hashing, SHA-256 keys/sessions, migrations |
| `hive/orchestrator.py` | 467 | Manager agent, meta-tools (`list_agents`, `list_tools`, `create_tool`, `create_agent`, `assign_task`, `recent_tasks`), email/WhatsApp AI workflows, fallback routing, Markdown output archival |
| `hive/tool_creator.py` | 342 | Dynamic Tool Creator: `deepseek-chat` synthesis, AST import/call allowlist, scrubbed-env unit test runner & self-repair loop |
| `hive/scheduler.py` | 281 | Persistent background daemon scheduler (5m–1h, 4h increments, timezone-aware daily triggers), quota checks, Telegram & multi-part WhatsApp delivery |
| `hive/tools.py` | 258 | Built-in vetted `ToolLibrary` + dynamic custom tool loading/hot-registration, path-traversal guard, 6,000-char output clipping, scrubbed `run_python` |
| `hive/llm.py` | 135 | Stdlib OpenAI/DeepSeek HTTP client, API key resolution, `reasoning_content` & `<think>` handling, JSON text-call recovery, thread-local token usage accumulator |
| `hive/runner.py` | 97 | Shared ReAct tool-use loop, flat JSON schema validation (extra-key stripping), error-as-observation retry, `max_steps` cap |
| `hive/channels/telegram_bot.py` | 617 | Multi-user Telegram long-polling bot manager (`TelegramBotManager`), `/link` pairing, full slash command & `.md`/`.txt`/`.csv` document router |
| `hive/channels/whatsapp_client.py` | 301 | WAHA `NOWEB` sidecar client: QR PNG proxy, `noweb.store` unread chat sync, ordered multi-part report delivery (`Header + Parts + End`) |
| `hive/channels/formatting.py` | 275 | GitHub-flavored Markdown converters → Telegram HTML (`parse_mode="HTML"`) and native WhatsApp syntax (`*bold*`, `_italic_`, bullet tables) |
| `hive/channels/email_client.py` | 223 | Stdlib IMAP/SMTP client: scan last `N` messages for unread, automated/newsletter detection, HTML stripping, threaded `In-Reply-To` SMTP replies |
| `hive/dashboard.html` | 2,635 | Zero-build Studio SPA (Auth, First-Login Reset, Chat + Drag/Drop Uploads + Trace, Separated Email/WhatsApp Inbox, Agents, Tool Forge, Workspace MD/CSV Preview, Scheduler, My Bots, Admin Dashboard, API Playground) |
| `hive/favicon.svg` | 20 | Honeycomb/hive SVG favicon served at `/favicon.svg` and `/favicon.ico` |
| `Dockerfile` / `docker-compose.yml` | 120 | Non-root `python:3.12-slim` image, `HEALTHCHECK`, `develop.watch` (`sync+restart` & `rebuild`), optional `waha` & `cli` Compose profiles |
| `requirements.txt` | 3 | `fastapi`, `uvicorn[standard]`, `python-multipart` (CLI remains zero-pip locally via lazy server import) |
| `API_DOCUMENTATION.md` | 447 | Full REST API reference, scopes, key distribution guide, status codes, and client examples |
| `tests/mock_server.py` | 71 | Local scripted OpenAI-compatible mock server for API-key-free smoke testing |

---

## 4. Detailed Subsystem Specifications

### 4.1 LLM Client, Token Telemetry & Shared Tool Loop (`hive/llm.py`, `hive/runner.py`)
- **OpenAI-Compatible Protocol**: `POST {base_url}/chat/completions` (`urllib.request`, `stream: false`, 600s timeout). Works with DeepSeek, OpenAI, Ollama (`/v1`), vLLM, etc.
- **Reasoning Support**: Omits `temperature` when `"reasoner"` or `"-r1"` is in the model ID; echoes `reasoning_content` across multi-turn tool calls; strips `<think>...</think>` blocks.
- **Small-Model Resilience**: Recovers tool calls written as plain JSON (`{"name": ..., "arguments": ...}`) in assistant text; drops unknown extra tool arguments; feeds JSON/schema/runtime errors back as `"ERROR: ..."` tool outputs; enforces `max_steps = 8` with a forced final answer turn.
- **Token Usage Tracking**: Thread-local `start_usage_tracking()` / `finish_usage_tracking()` accumulates exact `prompt_tokens`, `completion_tokens`, and `total_tokens` across nested orchestrator + worker calls (with character-length fallback when local models omit `usage`).

### 4.2 Dynamic Tool Creator — Implemented (`hive/tool_creator.py`, `hive/tools.py`)
The originally proposed **Dynamic Tool Creator** is now implemented end-to-end:
- **Flagship Model Routing**: Configurable via `[tool_creator]` in `config.toml` (defaults to `v4` → `deepseek-chat`).
- **Synthesis Contract**: Prompts the flagship model to output strict JSON `{name, description, parameters, code, tests}` where `code` defines `def run(**kwargs) -> str`.
- **Static AST Safety Guard (`check_ast_safety`)**:
  - Enforces an explicit stdlib allowlist (`math`, `statistics`, `json`, `csv`, `re`, `datetime`, `collections`, `itertools`, `hashlib`, `xml.etree.ElementTree`, `difflib`, etc.).
  - Blocks `os`, `sys`, `subprocess`, `socket`, `shutil`, `ctypes`, `urllib.request`, `eval`, `exec`, `compile`, `__import__`, direct `open()`, dangerous attributes (`system`, `popen`, `environ`, `rmtree`, etc.), and dunder attributes (`__class__`, `__globals__`, etc.).
  - Injects workspace-confined helpers (`ws_read(rel_path)`, `ws_write(rel_path, content)`, `ws_list()`) inside the execution harness.
- **Verification & Self-Repair Loop**: Runs generated unit tests in an isolated temp workspace inside a scrubbed-env subprocess (15s timeout, no API keys in `env`). On AST/schema/test failure, feeds errors/tracebacks back to `deepseek-chat` up to `max_repair_attempts = 3`.
- **Persistence & Hot-Reload**: Persists verified tools in SQLite `custom_tools` + `/data/custom_tools/<name>.py`, hot-registers into `ToolLibrary` immediately for same-turn worker assignment, tracks `use_count`, protects built-in tools from overwrite/deletion, and reloads automatically on startup.

### 4.3 Multi-User Auth, RBAC, Token Quotas & API Key Distribution (`hive/registry.py`, `hive/server.py`)
- **Fail-Closed Auth**: Accepts HttpOnly session cookies (`hive_session`), `Authorization: Bearer <token>`, or `X-API-Key: <token>` (`HIVE_API_TOKEN` master key, generated `hive_sk_...` keys, or `hive_sess_...` tokens).
- **Default Admin Bootstrap**: First boot with no admin generates `admin_<6hex>` + random password printed in `docker logs`, sets `must_reset_credentials = 1`, and blocks all other endpoints (`403 MUST_RESET_CREDENTIALS`) until `POST /api/v1/auth/reset-credentials` sets a new username & password.
- **User Governance**: Admins can `active` / `restrict` (blocks writes & LLM calls with custom reason) / `revoke` (kills all sessions immediately & blocks login), set/clear/reset per-user `token_limit` (`429` on exhaustion), toggle `allow_signup`, set `default_user_token_limit`, and switch models live (persisted in SQLite `settings`).
- **API Keys**: Stored as SHA-256 hashes only (`api_keys` table) with `read`/`write`/`admin` scopes, expiry, usage counters, rotation, revocation, and one-click distribution kits (`curl`, Python, JS, `.env`, shareable Markdown, downloadable JSON).

### 4.4 Chatbox File Uploads & Workspace Viewer (`hive/server.py`, `hive/dashboard.html`)
- **Strict Chat Upload Policy**: `POST /api/v1/chat/upload` (and Telegram document handler) strictly allows **`.md`, `.markdown`, `.txt`, and `.csv`** UTF-8 text files only (max 2 MB each, max 5 files per message; binary/null-byte and disallowed extensions rejected with `400`). Saved persistently to `workspace/uploads/<username>/`.
- **Workspace Preview**: Built-in offline Markdown renderer (headings, tables, fenced code blocks, blockquotes, lists, inline styles, safe links) and CSV table renderer (up to 500 rows) with **Preview / Split / Raw-Edit** modes and file download.

### 4.5 Persistent Task Scheduler (`hive/scheduler.py`)
- **Triggers**:
  - Repeating intervals: `5, 10, 15, 20, 30, 45, 60` min (5m–1h) and `240, 480, 720, 960, 1200, 1440` min (4-hour increments).
  - Daily specific local time (`HH:MM`) with `days_of_week` (`0`=Sun..`6`=Sat) and client `timezone_offset_minutes`.
- **Execution & Delivery**: Daemon thread polls every 5s, recovers stuck `running` states after crashes, enforces owner status & token quota before running (`skipped` if exceeded/restricted), records every run in `schedule_runs`, and pushes finished outputs to the owner's **Telegram Bot** (`notify_telegram`) and/or **WhatsApp** (`notify_whatsapp`).

### 4.6 Multi-Channel Integrations (`hive/channels/`)
- **Email (`email_client.py`)**:
  - Configurable via `.env` or persistently in SQLite (`PUT /api/v1/inbox/email/config`).
  - `GET /api/v1/inbox/email/unread?refresh=false|true`: Scans unread mail from the last `N` (default 100) messages, caches the snapshot + `fetched_at` timestamp in SQLite until next fetch.
  - `POST /api/v1/inbox/email/summarize` & `summarize_important_emails` tool: Filters newsletters/automated alerts, outputs a prioritized Markdown briefing (cached + saved in `workspace/output/email_digest/`).
  - `POST /api/v1/inbox/email/batch-draft` & `batch_prepare_email_drafts` tool: Batch-generates editable reply drafts for all actionable unread emails at once into `inbox_drafts`; supports inline editing, single send, and batch send (`In-Reply-To` / `References` threading).
- **Personal WhatsApp (`whatsapp_client.py` + `waha` sidecar)**:
  - Runs `devlikeapro/waha:latest` under Compose profile `whatsapp` using the lightweight **`NOWEB`** WebSocket engine (no headless Chromium timeouts) and shared `WAHA_API_KEY`.
  - Proxies live QR PNG to the Studio GUI (`GET /api/v1/inbox/whatsapp/qr`), supports `noweb.store` full unread sync (`POST /api/v1/inbox/whatsapp/enable-sync`), receives incoming messages at `POST /api/v1/webhooks/whatsapp` (persisted in `whatsapp_messages` + auto-drafted into `inbox_drafts`).
  - Delivers scheduled reports as a structured multi-message sequence (`Header Details` → `Chunked Body [Part i/N]` ≤ 2,800 chars → `END OF REPORT` footer) formatted with `md_to_whatsapp()`.
- **Per-User Telegram Bots (`telegram_bot.py`)**:
  - Each user saves their own `@BotFather` token in **My Bots** (`/api/v1/me/channels`) and pairs securely via `/link <CODE>`.
  - `TelegramBotManager` runs one long-polling daemon thread per enabled user (no public HTTPS webhook required) and supports plain orchestrator chat, `.md`/`.txt`/`.csv` uploads, and 18+ slash commands (`/status`, `/agents`, `/run`, `/tools`, `/create_tool`, `/schedules`, `/schedule`, `/run_schedule`, `/pause_schedule`, `/resume_schedule`, `/del_schedule`, `/files`, `/read`, `/tasks`, `/emails`, `/draft_emails`, `/drafts`, `/send_draft`, `/reset`, `/help`) rendered via `md_to_telegram_html()`.

### 4.7 SQLite Schema Summary (`/data/agents.db`, WAL Mode)
13 tables persisted in `/data/agents.db`:
- `agents` — worker specs (`name`, `description`, `system_prompt`, `tools`, `endpoint`, `model`, `created_at`)
- `tasks` — worker run history (`id`, `agent`, `task`, `status`, `result`, `created_at`, `finished_at`)
- `users` — accounts (`id`, `username`, `password_hash`, `role`, `status`, `must_reset_credentials`, `token_limit`, `tokens_used`, `prompt_tokens`, `completion_tokens`, `request_count`, `restriction_reason`, `created_at`, `last_login_at`)
- `sessions` — hashed login cookies (`token_hash`, `user_id`, `created_at`, `expires_at`)
- `chat_messages` — per-user persistent chat turns (`id`, `user_id`, `role`, `content`, `files_json`, `logs_json`, `tokens_used`, `created_at`)
- `api_keys` — hashed API keys (`id`, `name`, `key_prefix`, `key_hash`, `scopes`, `created_at`, `expires_at`, `last_used_at`, `request_count`, `revoked`)
- `settings` — JSON key-value store (`allow_signup`, `default_user_token_limit`, persisted model overrides, `email_config`, `email_unread_cache`, `email_summary_cache`)
- `scheduled_tasks` — recurring jobs (`id`, `user_id`, `owner_name`, `name`, `target_type`, `agent_name`, `prompt`, `trigger_type`, `interval_minutes`, `daily_time`, `days_of_week`, `timezone_offset_minutes`, `enabled`, `next_run_at`, `last_run_at`, `last_status`, `last_result`, `last_error`, `run_count`, `notify_telegram`, `notify_whatsapp`, `created_at`, `updated_at`)
- `schedule_runs` — execution logs & token usage per schedule run
- `custom_tools` — dynamically synthesized tools (`name`, `description`, `schema_json`, `code`, `tests`, `created_by_model`, `created_by_user`, `use_count`, `created_at`, `updated_at`)
- `inbox_drafts` — unified editable Email & WhatsApp reply queue (`id`, `channel`, `external_id`, `sender_name`, `sender_addr`, `subject`, `in_reply_to`, `incoming_body`, `priority`, `summary`, `draft_subject`, `draft_reply`, `status`, `error`, `created_at`, `updated_at`, `sent_at`)
- `whatsapp_messages` — locally logged incoming WhatsApp unread messages
- `user_channels` — per-user Telegram bot tokens, chat IDs, link codes, and WhatsApp notification targets

---

## 5. Deployment & Operations

- **Container Topology (`docker-compose.yml`)**:
  - `agent-hive`: Non-root (`uid 1000`), exposes internal port `8000` mapped to `${HIVE_PORT:-8000}`, mounts `hive_data:/data`, includes `HEALTHCHECK` on `/health`, `init: true`, `no-new-privileges:true`, `host.docker.internal` mapping for host Ollama/vLLM, and `develop.watch` (`sync+restart` on `hive/`, `main.py`, `config.toml.example`; `rebuild` on `requirements.txt`, `Dockerfile`).
  - `waha` (`profiles: ["whatsapp"]`): `devlikeapro/waha:latest` (`NOWEB` engine), mounts `waha_sessions:/app/.sessions`, shares `WAHA_API_KEY` with `agent-hive`.
  - `cli` (`profiles: ["cli"]`): Interactive REPL sharing `hive_data:/data`.
- **Commands**:
  - Standard deploy: `docker compose up -d --build`
  - With Personal WhatsApp: `docker compose --profile whatsapp up -d --build`
  - Live dev reload: `docker compose up --watch`
  - Interactive CLI: `docker compose run --rm cli`
  - Mint API key via CLI: `docker compose exec agent-hive python main.py key create "client" --scopes read,write --days 30`

---

## 6. Current Limitations & Research / Proposal Opportunities

While the core platform and v1 Dynamic Tool Creator are implemented and working in Docker, the following open engineering and research challenges make strong topics for a formal project proposal, thesis, or v3 roadmap:

1. **Stronger OS / MicroVM / WASM Sandboxing for Synthesized Tools & `run_python`**:
   - Currently, custom tools rely on AST allowlisting + scrubbed-env subprocesses inside the `agent-hive` container (no `rlimit` memory/CPU caps, no kernel namespace isolation, and same container filesystem as `/data/agents.db`).
   - Proposal upgrade: Ephemeral container / `bubblewrap` (`bwrap`) / Firecracker / WebAssembly (`Pyodide`/`wasmtime`) sandbox with strict CPU/RAM/network quotas and read-only mounts.
2. **Semantic Tool Retrieval, Deduplication & Toolbox Pruning**:
   - Currently, every tool in `ToolLibrary` is injected into `ORCHESTRATOR_PROMPT`. As users synthesize dozens or hundreds of tools, prompt token overhead grows and tool selection accuracy degrades.
   - Proposal upgrade: Embedding/BM25 tool retrieval (inspired by **Voyager** / **CRAFT**), automated deduplication & generalization of overlapping tools (**TroVE**), version history, and automatic repair when runtime failures occur.
3. **Human-in-the-Loop Tool Approval & Security Auditing**:
   - Adding an optional approval gate in **Tool Forge** (`pending_approval` → admin reviews AST report, source code, and test output → `approved`) before newly synthesized tools become globally usable by non-admin users.
4. **Streaming (SSE / WebSockets) & Parallel Async Worker Execution**:
   - Currently `op_lock` serializes orchestrator/worker turns and `POST /api/v1/chat` blocks until the entire multi-step loop finishes.
   - Proposal upgrade: Per-user concurrency locks, Server-Sent Events streaming live tool calls & tokens into the Studio GUI, and parallel `asyncio` fan-out across independent worker subtasks.
5. **Model Context Protocol (MCP) Interoperability**:
   - Exposing `agent-hive`'s built-in + synthesized tools as an MCP server (or letting workers consume external MCP tool servers) for standardized ecosystem integration.
6. **Empirical Evaluation & Cost Amortization Benchmarks**:
   - Quantitative evaluation comparing: (a) static built-in tools baseline, (b) ad-hoc `run_python`, and (c) `agent-hive`'s persistent `deepseek-chat` Tool Creator + cheaper/reasoning (`deepseek-reasoner` / local Ollama) Tool Users across pass@1 accuracy, tool reuse rate, repair-loop convergence, security red-teaming against prompt injection/escape attempts, and token cost amortized over $N$ recurring tasks.

---

## 7. Related Literature & Reference Systems (Search Anchors for LLMs)

- **LATM — *Large Language Models as Tool Makers*** (Cai et al., ICLR 2024): Strong Tool Maker (`deepseek-chat`) + cheaper Tool User (`deepseek-reasoner`/local) with cached reusable Python tools.
- **CREATOR** (Qian et al., EMNLP 2023): Tool creation, decision, execution, and rectification loop.
- **Voyager** (Wang et al., 2023): Iterative self-verification + persistent reusable Skill Library.
- **CRAFT** (Yuan et al., ICLR 2024) & **TroVE** (Wang et al., ICML 2024): Toolbox induction, deduplication, retrieval, and pruning.
- **CodeAct / Hugging Face `smolagents`**: AST-checked Python execution & sandboxed runtimes.
- **Agent Control Planes / Studios**: AutoGen Studio, LangGraph Studio, Dify, Flowise, OpenHands, CrewAI.
- **Sandboxing & Isolation**: `ast.NodeVisitor`, `RestrictedPython`, `resource.setrlimit`, `bubblewrap` (`bwrap`), gVisor, Firecracker, WebAssembly (`wasmtime` / Pyodide), MCP stdio/container servers.
- **Indirect Prompt Injection Defenses** (for Email/WhatsApp agents): Spotlighting, CaMeL, least-privilege tool scoping, human-in-the-loop draft approval queues.
