# `agent-hive` API Documentation

`agent-hive` exposes a **FastAPI** REST API with an interactive **API Documentation & Key Distribution Dashboard**, **Swagger UI**, **ReDoc**, and downloadable **OpenAPI 3.1** schema.

| Resource | URL (default) |
|---|---|
| Interactive Dashboard & Key Distribution UI | `http://localhost:8000/` |
| Swagger UI (interactive explorer) | `http://localhost:8000/docs` |
| ReDoc (clean reference) | `http://localhost:8000/redoc` |
| OpenAPI 3.1 JSON Schema | `http://localhost:8000/openapi.json` |
| Health Check | `http://localhost:8000/health` |

*(If you set `HIVE_PORT` in `.env`, e.g. `HIVE_PORT=8011`, substitute your configured port.)*

---

## 1. Authentication, Accounts & Scopes

`agent-hive` is **fail-closed** and supports both **GUI user sessions** (HttpOnly cookies backed by SQLite) and **API keys** (`Authorization: Bearer <key>` or `X-API-Key: <key>`).

### Initial Admin Bootstrap (Mandatory First-Login Reset)
- On first startup when no admin exists in SQLite (`/data/agents.db`), the server generates a random admin username (`admin_xxxxxx`) and password and prints them once in `docker logs`.
- On first login, the admin **must** call `POST /api/v1/auth/reset-credentials` (enforced by the GUI and API) to set a new username and password before accessing any other endpoint.
- Because users, sessions, chat history, uploaded files, token usage, agents, tasks, API keys, and settings all live in `/data` (`hive_data` Docker volume), everything persists across `docker restart`.

### Auth Endpoints
- `GET /api/v1/auth/status` *(public)* — returns `allow_signup` and `allowed_chat_file_types`
- `POST /api/v1/auth/signup` *(public when enabled)* — create a `user` account with default token quota
- `POST /api/v1/auth/login` *(public)* — sign in and receive persistent `hive_session` HttpOnly cookie
- `POST /api/v1/auth/logout` — invalidate session and clear cookie
- `GET /api/v1/auth/me` — return current user profile, role, status, and token quota
- `POST /api/v1/auth/reset-credentials` — change username & password (required on first admin login)

### API Key Headers & Permission Scopes
Pass an API key on programmatic requests using:
```http
Authorization: Bearer <API_KEY>
```
or `X-API-Key: <API_KEY>`. Master token `HIVE_API_TOKEN` (from `.env`) always has full `admin` scope. Generated keys (`hive_sk_...`) are stored as **SHA-256 hashes** only.

| Scope / Role | Allowed Operations |
|---|---|
| `read` (or `restricted` user) | All `GET` endpoints (agents, tasks, models, tools, chat history, workspace files) |
| `write` (or `active` user) | Chat (`POST /api/v1/chat`, `POST /api/v1/chat/upload`), `POST /api/v1/chat/reset`, agent create/update, `POST /api/v1/run`, workspace write |
| `admin` (or `admin` role) | Full access including user governance (`/api/v1/admin/*`), API keys (`/api/v1/keys`), model switching (`POST /api/v1/models`), and deletions |

### Admin Governance Endpoints (`admin` scope)
- `GET /api/v1/admin/users` — list all users, statuses (`active` / `restricted` / `revoked`), token usage & limits
- `POST /api/v1/admin/users` — create a user directly (`username`, `password`, `role`, `token_limit`)
- `PATCH /api/v1/admin/users/{user_id}` — revoke (immediately clears sessions), restrict (with optional `restriction_reason`), activate, set/clear `token_limit`, or `reset_tokens: true`
- `DELETE /api/v1/admin/users/{user_id}` — delete a user account
- `GET /api/v1/admin/settings` / `PATCH /api/v1/admin/settings` — toggle `allow_signup` and configure `default_user_token_limit` (persisted in SQLite)

---

## 2. Generating & Distributing API Keys

You can generate and share API keys in three ways:

### Option A — Web Dashboard (`/`)
1. Open `http://localhost:8000/` and enter your `HIVE_API_TOKEN` (or an `admin` key) in the top bar.
2. Open the **Key Generator & Distribution** tab.
3. Choose a client label, scopes (`read`, `write`, `admin`), and optional expiration in days, then click **Generate API Key**.
4. Use the one-click buttons to:
   - **Copy Key** (`hive_sk_...`)
   - **Copy Shareable Invite (MD)** (ready to paste into Slack/email with base URL, docs links, and curl test)
   - **Copy cURL / Python / JavaScript / `.env` snippets**
   - **Download Client Kit (`.json`)**
5. Rotate or revoke keys anytime from the **Issued API Keys** table.

### Option B — Docker / CLI (One-Command Generation)
```bash
# Generate a 30-day read/write key and print copy-ready .env & curl snippets
docker compose exec agent-hive python main.py key create "frontend-app" --scopes read,write --days 30 --base-url https://hive.example.com

# Output machine-readable JSON kit
docker compose exec agent-hive python main.py key create "partner-a" --scopes read,write --json

# List, rotate, or revoke keys
docker compose exec agent-hive python main.py key list
docker compose exec agent-hive python main.py key rotate key_abc123def456
docker compose exec agent-hive python main.py key revoke key_abc123def456
```

### Option C — REST API (`POST /api/v1/keys`)
```bash
curl -X POST "http://localhost:8000/api/v1/keys" \
  -H "Authorization: Bearer $HIVE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "partner-app", "scopes": ["read", "write"], "expires_in_days": 30}'
```

The response includes both `api_key` and a complete `distribution` object containing `base_url`, `docs_url`, `openapi_url`, and ready-to-share `curl`, `python`, `javascript`, `env`, and `markdown` snippets.

---

## 3. Endpoint Reference

### System

#### `GET /health` *(Public)*
Returns service status, uptime, active auth state, counts, and resolved orchestrator/worker models.
```json
{
  "status": "ok",
  "version": "1.0.0",
  "uptime_seconds": 42.3,
  "auth_enabled": true,
  "master_token_configured": true,
  "counts": { "agents": 2, "tasks": 5, "tools": 4, "api_keys": 3 },
  "models": {
    "orchestrator": "deepseek-chat @ deepseek",
    "workers_default": "deepseek-reasoner @ deepseek"
  }
}
```

---

### Orchestrator

#### `POST /api/v1/chat` *(Scope: `write`, alias: `POST /chat`)*
Send a natural-language request to the orchestrator agent.

**Request:**
```json
{
  "message": "Create a web_summarizer agent and summarize https://example.com into notes/example.md",
  "reset_before": false
}
```
**Response (`200`):**
```json
{
  "reply": "Created web_summarizer and saved the summary to notes/example.md.",
  "logs": [
    "[orchestrator:deepseek-chat] → list_agents({})",
    "[orchestrator:deepseek-chat] ← No agents exist yet."
  ]
}
```

#### `POST /api/v1/chat/upload` *(Scope: `write`, `multipart/form-data`)*
Send a chat prompt with up to 5 attached files. **Strictly allows `.md`, `.markdown`, `.txt`, and `.csv` UTF-8 text files only** (max 2 MB each); all other file types return `400`. Uploaded files are persisted in `workspace/uploads/<username>/` and made available to worker agents.

**Fields:** `message` (form text), `reset_before` (bool, optional), `files` (one or more `.md`/`.txt`/`.csv` files).

#### `GET /api/v1/chat/history` *(Scope: `read`)*
Returns the orchestrator's sliding-window conversation memory.
```json
{
  "turns": 1,
  "max_turns": 6,
  "history": [{ "user": "List your agents", "assistant": "No agents exist yet." }]
}
```

#### `POST /api/v1/chat/reset` *(Scope: `write`, alias: `POST /reset`)*
Clears the orchestrator's in-memory conversation history.
```json
{ "status": "cleared" }
```

---

### Agents

#### `GET /api/v1/agents` *(Scope: `read`, alias: `GET /agents`)*
List all persisted worker agents.

#### `POST /api/v1/agents` *(Scope: `write`)*
Create or overwrite a worker agent directly without going through the orchestrator.

**Request:**
```json
{
  "name": "doc_writer",
  "description": "Writes clean Markdown documentation inside the workspace",
  "system_prompt": "You write concise, accurate Markdown documentation.",
  "tools": ["read_file", "write_file", "list_files"],
  "model": "v4",
  "endpoint": "deepseek"
}
```
**Response (`201`):** `{ "agent": { ... } }`

#### `GET /api/v1/agents/{name}` *(Scope: `read`, alias: `GET /agents/{name}`)*
Fetch a single worker agent's full specification.

#### `PATCH /api/v1/agents/{name}` *(Scope: `write`)*
Partially update an existing agent (`description`, `system_prompt`, `tools`, `model`, `endpoint`).

#### `DELETE /api/v1/agents/{name}` *(Scope: `admin`, alias: `DELETE /agents/{name}`)*
Delete a worker agent from SQLite.

---

### Tasks & Direct Worker Execution

#### `POST /api/v1/run` *(Scope: `write`, alias: `POST /run`)*
Run a specific worker agent directly (bypasses the orchestrator for lower latency).

**Request:**
```json
{
  "agent": "doc_writer",
  "task": "Write a brief welcome note to welcome.md",
  "model": "",
  "endpoint": ""
}
```
**Response (`200`):**
```json
{
  "result": "[task #1 done (deepseek-chat @ deepseek) | saved: output/doc_writer/...]\nDone.",
  "logs": ["[doc_writer:deepseek-chat] → write_file(...)"]
}
```

#### `GET /api/v1/tasks` *(Scope: `read`, alias: `GET /tasks`)*
List task execution history.

**Query parameters:**
- `limit` (`1`–`200`, default `20`)
- `agent` *(optional)* — filter by worker agent name
- `status` *(optional)* — filter by `running`, `done`, or `failed`

#### `GET /api/v1/tasks/{task_id}` *(Scope: `read`)*
Retrieve the full record (including full result and timestamps) for a specific task ID.

---

### Models & Tools

#### `GET /api/v1/models` *(Scope: `read`, alias: `GET /model`)*
Returns resolved orchestrator model, default worker model, aliases, configured endpoints, and per-agent models.

#### `POST /api/v1/models` *(Scope: `admin`, alias: `POST /model`)*
Live-switch the model/endpoint for the orchestrator, default workers, or a named agent.

**Request:**
```json
{
  "target": "orch",
  "model": "r1",
  "endpoint": "deepseek"
}
```
*(`target` accepts `"orch"`, `"workers"`, or any existing agent name.)*

#### `GET /api/v1/tools` *(Scope: `read`)*
Returns all built-in and dynamically synthesized custom tools (including JSON schemas, creator model, source code, tests, and usage count).

#### `POST /api/v1/tools/create` *(Scope: `write`)*
Invoke the **Dynamic Tool Creator** (`deepseek-chat` / `[tool_creator]` flagship model) to synthesize, AST-verify, unit-test, repair, and permanently save a new reusable Python tool.
```json
{
  "name": "csv_stats",
  "description": "Compute mean, median, min, max for a CSV column",
  "requirements": "Accept path and column; use ws_read(path); return Markdown table."
}
```

#### `POST /api/v1/tools/{name}/test` *(Scope: `write`)*
Execute any registered tool directly with `{"arguments": {...}}`.

#### `DELETE /api/v1/tools/{name}` *(Scope: `admin`)*
Delete a custom tool (built-in vetted tools cannot be deleted).

---

### Inbox — Email (Batch Drafts + Important Summary) & Personal WhatsApp (QR)

#### `GET /api/v1/inbox/drafts` *(Scope: `read`)*
List editable reply drafts (`?channel=email|whatsapp&status=pending|sent|dismissed`).

#### `PATCH /api/v1/inbox/drafts/{draft_id}` *(Scope: `write`)*
Edit `draft_subject`, `draft_reply`, or set `status` (`pending` / `dismissed`).

#### `POST /api/v1/inbox/drafts/{draft_id}/send` *(Scope: `write`)*
Send a single edited Email (SMTP) or WhatsApp (WAHA) reply and mark it `sent`.

#### `POST /api/v1/inbox/drafts/send-batch` *(Scope: `write`)*
Send multiple approved/edited drafts in one call: `{"draft_ids": ["drf_...", "..."]}`.

#### `POST /api/v1/inbox/email/summarize` *(Scope: `write`)*
Scan unread emails from the **last 100** INBOX messages, filter automated noise, and return a prioritized Markdown briefing (also saved to `workspace/output/email_digest/`).

#### `POST /api/v1/inbox/email/batch-draft` *(Scope: `write`)*
Scan unread emails from the **last 100** messages and batch-create editable reply drafts for all important/actionable emails at once (`{"scan_last": 100, "instructions": "concise, polite"}`).

#### `GET` / `PUT /api/v1/inbox/email/config` *(Scope: `admin`)*
Inspect (password masked) or save persisted IMAP/SMTP credentials in SQLite.

#### Personal WhatsApp (WAHA QR Sidecar)
Start the optional sidecar with `docker compose --profile whatsapp up -d`, then:
- `GET /api/v1/inbox/whatsapp/status` *(read)* — WAHA session state (`STOPPED`, `SCAN_QR_CODE`, `WORKING`, `OFFLINE`)
- `POST /api/v1/inbox/whatsapp/start` / `stop` *(admin)* — start/stop session and register webhook
- `GET /api/v1/inbox/whatsapp/qr` *(admin)* — live QR PNG image to scan with WhatsApp → *Linked Devices*
- `POST /api/v1/webhooks/whatsapp` — receives incoming personal WhatsApp messages from WAHA and creates editable reply drafts automatically

---

### Workspace Files

All paths are strictly confined to `workspace/` (path traversal is rejected with `400`).

#### `GET /api/v1/workspace/files` *(Scope: `read`)*
Lists all files in `workspace/` (including `output/<agent>/*.md` archival logs) with sizes and modification timestamps.

#### `GET /api/v1/workspace/files/{file_path}` *(Scope: `read`)*
Reads a UTF-8 text file from `workspace/`.

#### `PUT /api/v1/workspace/files/{file_path}` *(Scope: `write`)*
Creates or overwrites a text file in `workspace/`.
```json
{ "content": "# Hello workspace\n" }
```

#### `DELETE /api/v1/workspace/files/{file_path}` *(Scope: `admin`)*
Deletes a file from `workspace/`.

---

### Task Scheduler (Persistent)

Recurring jobs are persisted in SQLite (`scheduled_tasks` & `schedule_runs`), survive `docker restart`, enforce owner status & token quotas, and run in a background daemon thread.

**Supported Triggers:**
- `trigger_type: "interval"` — `interval_minutes` in **`5, 10, 15, 20, 30, 45, 60`** (5 min to 1 hour) or **`240, 480, 720, 960, 1200, 1440`** (4-hour increments: 4h, 8h, 12h, 16h, 20h, 24h)
- `trigger_type: "daily"` — `daily_time` in 24-hour `HH:MM` format, optional `days_of_week` (`0`=Sun .. `6`=Sat), and `timezone_offset_minutes` (UTC offset in minutes, e.g. `+360` for UTC+6)

#### `GET /api/v1/schedules` *(Scope: `read`)*
List scheduled tasks (admins see all users' schedules; regular users see their own).

#### `POST /api/v1/schedules` *(Scope: `write`)*
Create a scheduled task targeting either `orchestrator` or a specific `worker` agent.
```json
{
  "name": "Morning Digest",
  "target_type": "orchestrator",
  "prompt": "Summarize workspace updates into notes/morning.md",
  "trigger_type": "daily",
  "daily_time": "09:00",
  "days_of_week": [1, 2, 3, 4, 5],
  "timezone_offset_minutes": 360
}
```

#### `PATCH /api/v1/schedules/{schedule_id}` *(Scope: `write`)*
Pause/resume (`enabled: false/true`) or update trigger, prompt, or target.

#### `POST /api/v1/schedules/{schedule_id}/run` *(Scope: `write`)*
Immediately execute a scheduled task on demand and record the run.

#### `GET /api/v1/schedules/{schedule_id}/runs` *(Scope: `read`)*
Retrieve execution history (`status`, `result`, `logs`, `tokens_used`, timestamps) for a schedule.

#### `DELETE /api/v1/schedules/{schedule_id}` *(Scope: `write` owner / `admin`)*
Delete a scheduled task and its run history.

*(Set `"notify_telegram": true` and/or `"notify_whatsapp": true` when creating or updating a schedule to push finished results straight to the owner's Telegram bot and/or WhatsApp.)*

---

### My Bots — Per-User Telegram Bot & WhatsApp Schedule Alerts

Every signed-in user can configure their own personal Telegram bot (full control of `agent-hive` directly from Telegram via long-polling — no public HTTPS domain required) and their own WhatsApp phone number for receiving scheduled task notifications.

- `GET /api/v1/me/channels` *(read)* — get current user's saved bot/WhatsApp settings, pairing `/link CODE`, and live Telegram worker status
- `PUT /api/v1/me/channels` *(write)* — save `telegram_bot_token`, `telegram_chat_id`, `telegram_enabled`, `whatsapp_target`, `whatsapp_enabled` and immediately start/stop the user's Telegram bot worker
- `POST /api/v1/me/channels/test-telegram` *(write)* — send a test message through the user's Telegram bot
- `POST /api/v1/me/channels/test-whatsapp` *(write)* — send a test WhatsApp alert to the user's WhatsApp target (or WAHA linked self-chat)

---

### API Keys

#### `GET /api/v1/keys` *(Scope: `admin`)*
Lists all issued keys with prefix, scopes, status (`active` / `expired` / `revoked`), `request_count`, `created_at`, `expires_at`, and `last_used_at`.

#### `POST /api/v1/keys` *(Scope: `admin`)*
Generates a new API key (`hive_sk_...`) and returns the one-time secret plus ready-to-distribute snippets (`distribution.snippets`).

#### `POST /api/v1/keys/{key_id}/rotate` *(Scope: `admin`)*
Revokes `key_id` and immediately issues a replacement key with the same label, scopes, and remaining expiry.

#### `DELETE /api/v1/keys/{key_id}` *(Scope: `admin`)*
Immediately revokes the key.

---

## 4. Client Code Examples

### Python (`requests`)
```python
import requests

BASE_URL = "http://localhost:8000"
HEADERS = {"Authorization": "Bearer hive_sk_your_key_here"}

# Create an agent
requests.post(f"{BASE_URL}/api/v1/agents", headers=HEADERS, json={
    "name": "notes_bot",
    "description": "Manages notes in workspace",
    "system_prompt": "You organize concise Markdown notes.",
    "tools": ["read_file", "write_file", "list_files"],
}).raise_for_status()

# Run it directly
res = requests.post(f"{BASE_URL}/api/v1/run", headers=HEADERS, json={
    "agent": "notes_bot",
    "task": "Create notes/todo.md with 3 items",
})
print(res.json()["result"])
```

### JavaScript (`fetch`)
```javascript
const BASE_URL = "http://localhost:8000";
const API_KEY = "hive_sk_your_key_here";

const resp = await fetch(`${BASE_URL}/api/v1/chat`, {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${API_KEY}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ message: "List your agents" }),
});
console.log(await resp.json());
```

---

## 5. HTTP Status Codes

| Status | Meaning |
|---|---|
| `200` / `201` | Success / Resource created |
| `400` | Validation or domain error (invalid agent name, unknown tool/endpoint, workspace path escape) |
| `401` | Missing, invalid, expired, or revoked API key |
| `403` | Valid API key lacking the required scope (`read`, `write`, or `admin`) |
| `404` | Resource (agent, task, file, key) not found |
| `422` | Request JSON failed Pydantic schema validation |
| `429` | User token usage limit reached |
| `502` | Upstream LLM endpoint failed or returned an error |
