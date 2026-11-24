# agent-hive

A lightweight multi-model orchestrator agent that creates worker (child) agents and assigns them tasks on request.
Mix and match models freely — for example, run **DeepSeek V4** (`deepseek-chat`) as your orchestrator and **DeepSeek R1** (`deepseek-reasoner`) for your child agents (or vice-versa, or mix with local Ollama models).
Pure Python 3.11+ standard library — zero `pip` dependencies.

```
you ──► orchestrator (e.g. DeepSeek V4 / deepseek-chat)
            │  list_agents / create_agent / assign_task / recent_tasks
            ▼
        child agents (e.g. DeepSeek R1 / deepseek-reasoner, or per-agent custom models)
            │  web_fetch / read_file / write_file / list_files / [run_python]
            ▼
        workspace/  (sandbox folder)       agents.db  (agent specs + task history)
```

## Quick Start (DeepSeek API)

1. **Copy the example config and environment files**

   ```bash
   cp config.toml.example config.toml
   cp .env.example .env
   ```

2. **Configure your API key**

   Edit `.env`:

   ```env
   DEEPSEEK_API_KEY=sk-your-deepseek-api-key
   ```

3. **Run**

   ```bash
   python main.py
   ```

## Multi-Model Configuration (Orchestrator vs. Child Agents)

In `config.toml`, you can set different models for the `[orchestrator]` and `[workers]` (child agents), and define friendly aliases under `[models]`:

```toml
[endpoints.deepseek]
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"

[models]
v4 = "deepseek-chat"            # DeepSeek V4 / flagship chat
v3 = "deepseek-chat"            # DeepSeek V3 chat
r1 = "deepseek-reasoner"        # DeepSeek R1 reasoning model

[orchestrator]
endpoint = "deepseek"
model = "v4"                    # Orchestrator runs DeepSeek V4 (deepseek-chat)

[workers]
endpoint = "deepseek"
model = "r1"                    # Child agents default to DeepSeek R1 (deepseek-reasoner)
use_local_fallback = false      # Set to true to enable local fallback if primary endpoint fails
fallback_endpoint = ""
fallback_model = ""
```

### Switching Models Live in the CLI

You can inspect or change models at any time without restarting using `/model`:

```
you> /model                        # show current orchestrator, default worker, and per-agent models
you> /model orch v4                # set orchestrator to v4 (deepseek-chat)
you> /model workers r1             # set default model for new child agents to r1 (deepseek-reasoner)
you> /model math_solver r1         # switch a specific child agent to r1
you> /model fast_writer v4         # switch a specific child agent to v4
```

You can also ask the orchestrator in plain English:
```
you> create a child agent called math_coder using model r1 and have it solve ...
```

## Using it

```
you> create an agent that summarizes web pages, then summarize https://example.com into notes/example.md
you> /agents
you> /model
you> /run web_summarizer summarize https://news.ycombinator.com into notes/hn.md
you> /tasks
```

Plain text goes to the orchestrator, which reuses or creates agents. `/run` skips the orchestrator
and is faster when you already know which agent you want. `/help` lists all commands.
Only you can delete agents, using `/delete`. The orchestrator has no delete tool.

## Features & Reliability

- **Zero external dependencies**: built strictly on the Python 3.11+ standard library (`urllib`, `tomllib`, `sqlite3`, `json`), with built-in `.env` loading.
- **DeepSeek Reasoner & Thinking support**: preserves `reasoning_content` across multi-turn tool calls as required by `deepseek-reasoner`, and strips `<think>` blocks from local reasoning models.
- **Fixed tool library**: agents choose from vetted tools in `hive/tools.py` and never write their own.
  To add a capability, add a method and an entry in `ToolLibrary.tools`.
- **Validation and retry**: every tool call is checked against its JSON schema. Errors go back to
  the model as text so it can fix the call. Extra arguments are dropped.
- **Text-call recovery**: if a small local model writes `{"name": ..., "arguments": ...}` as text instead of a
  native tool call, it is still executed when the name matches a real tool.
- **Step cap**: `max_steps` stops infinite loops and forces a final answer.
- **Short results**: tool output is truncated to 6,000 characters to keep context lean.
- **Fallback endpoint**: worker calls can automatically retry on a fallback endpoint if the primary endpoint is unreachable.

## `run_python` Warning

> [!WARNING]
> Setting `allow_python = true` (in `config.toml`) or `HIVE_ALLOW_PYTHON=true` (in Docker) lets worker agents execute arbitrary Python code with the process user's permissions.

`run_python` enforces a 30-second timeout, runs with `workspace/` as its working directory, and strips host/API environment variables from the child process — **it is not a full sandbox**. Even inside the included Docker container (see [Docker Deployment](#docker-deployment)), executed code can still read `/data` (including `agents.db`), access the container filesystem as the `hive` user, and make outbound network requests (including to `host.docker.internal` when enabled in `docker-compose.yml`). Only enable it inside an isolated container or VM and restrict container networking when running untrusted prompts.

## Docker Deployment

`agent-hive` ships with a non-root `Dockerfile` and `docker-compose.yml` supporting both a long-running **HTTP JSON API server** (default for cloud/VPS deployment) and the **interactive CLI**.

### 1. Deploy the HTTP API server

```bash
cp .env.example .env
# Edit .env with your DEEPSEEK_API_KEY (and optionally HIVE_API_TOKEN)

docker compose up -d --build
```

Verify health and send a request:

```bash
curl http://localhost:8000/health

# Chat with the orchestrator (add -H "Authorization: Bearer $HIVE_API_TOKEN" if auth is enabled)
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "List your agents"}'
```

**Available HTTP endpoints:**
- `GET /health` — container healthcheck (`{"status": "ok"}`)
- `POST /chat` — `{"message": "..."}` → orchestrator response & logs
- `GET /agents` / `GET /agents/{name}` / `DELETE /agents/{name}` — inspect or delete agents
- `POST /run` — `{"agent": "...", "task": "...", "model": "", "endpoint": ""}` → run a worker directly
- `GET /tasks?limit=10` — recent task history
- `GET /model` / `POST /model` — inspect or live-switch models (`{"target": "orch|workers|AGENT", "model": "r1"}`)
- `POST /reset` — clear orchestrator conversation memory

### 2. Run the interactive CLI in Docker

Shares the same persistent `/data` volume (`agents.db` and `workspace/`):

```bash
docker compose run --rm cli
```

### 3. Configuration & Environment Variables

- Mount a custom `config.toml` at `/app/config.toml:ro` (uncomment in `docker-compose.yml`), or use environment overrides:
  - `HIVE_CONFIG` — path to config file
  - `HIVE_DB_PATH` — SQLite path (default `/data/agents.db` in Docker)
  - `HIVE_WORKSPACE` — workspace directory (default `/data/workspace` in Docker)
  - `HIVE_ALLOW_PYTHON` — `true` / `false` (safer inside a container than on your host)
  - `HIVE_API_TOKEN` — optional Bearer token protecting all HTTP endpoints except `/health`
  - `HIVE_PORT` — host/container listen port (default `8000`)
- To reach a local Ollama/vLLM server running on the Docker host, point your endpoint `base_url` in `config.toml` to `http://host.docker.internal:11434/v1`.

## Testing without an API key or GPU

`python tests/mock_server.py 18080` starts a scripted fake OpenAI-compatible model server for local testing.

## Files

| File | Purpose |
|---|---|
| `main.py` | CLI, `--serve` HTTP entrypoint, and `.env` loader |
| `Dockerfile` / `docker-compose.yml` | non-root container image & deployment compose stack |
| `config.toml.example` | template for endpoints, models, and runtime limits (copy to `config.toml`) |
| `.env.example` | template for environment variables (`DEEPSEEK_API_KEY`, `HIVE_API_TOKEN`) |
| `hive/orchestrator.py` | manager agent, its tools, and endpoint fallback |
| `hive/server.py` | stdlib HTTP JSON API server for container/cloud deployments |
| `hive/runner.py` | tool-use loop, schema validation, step cap |
| `hive/tools.py` | tool library for workers |
| `hive/registry.py` | thread-safe SQLite store (WAL mode) for agents and tasks |
| `hive/llm.py` | OpenAI/DeepSeek-compatible client, auth resolution, reasoning & `<think>` handling |
