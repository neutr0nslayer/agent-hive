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

## 1. Authentication & Scopes

Pass an API key on every request (except `/`, `/health`, `/docs`, `/redoc`, `/openapi.json`) using either header:

```http
Authorization: Bearer <API_KEY>
```
or:
```http
X-API-Key: <API_KEY>
```

### Auth Modes
- **Open mode (initial boot)**: When `HIVE_API_TOKEN` is empty **and** no active keys exist in SQLite, endpoints work without auth so you can immediately open `/` and generate your first admin key.
- **Protected mode (recommended)**: As soon as `HIVE_API_TOKEN` is set in `.env` **or** at least one active API key is generated, authentication and scope enforcement turn on automatically.
- **Master token (`HIVE_API_TOKEN`)**: Always carries full `admin` privileges. Generated keys are stored as **SHA-256 hashes** only (`hive_sk_...`) in SQLite and are shown once at creation time.

### Permission Scopes

| Scope | Allowed Operations |
|---|---|
| `read` | All `GET` endpoints (agents, tasks, models, tools, chat history, workspace files) |
| `write` | `POST /api/v1/chat`, `POST /api/v1/chat/reset`, `POST /api/v1/agents`, `PATCH /api/v1/agents/{name}`, `POST /api/v1/run`, `PUT /api/v1/workspace/files/{path}` |
| `admin` | Full access including API key management (`/api/v1/keys`), model switching (`POST /api/v1/models`), and deletion (`DELETE /api/v1/agents/{name}`, `DELETE /api/v1/workspace/files/{path}`) |

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
Returns all tools currently registered in `ToolLibrary` with descriptions and JSON parameter schemas.

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
| `502` | Upstream LLM endpoint failed or returned an error |
