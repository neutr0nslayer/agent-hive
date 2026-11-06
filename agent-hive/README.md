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

1. **Configure your API key**

   Copy `.env.example` to `.env` and add your DeepSeek API key:

   ```bash
   cp .env.example .env
   ```

   Edit `.env`:

   ```env
   DEEPSEEK_API_KEY=sk-your-deepseek-api-key
   ```

2. **Run**

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
local_8b = "desktop/qwen3:8b"   # endpoint/model shorthand

[orchestrator]
endpoint = "deepseek"
model = "v4"                    # Orchestrator runs DeepSeek V4 (deepseek-chat)

[workers]
endpoint = "deepseek"
model = "r1"                    # Child agents default to DeepSeek R1 (deepseek-reasoner)
fallback_endpoint = "desktop"
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

## `run_python` warning

`allow_python = true` lets agents run Python with your user's permissions. It has a timeout and
uses the workspace as its working directory, but it is **not a sandbox**. Enable it only inside a
container or VM, e.g. run `agent-hive` in Docker with only the workspace mounted.

## Testing without an API key or GPU

`python tests/mock_server.py 18080` starts a scripted fake OpenAI-compatible model server for local testing.

## Files

| File | Purpose |
|---|---|
| `main.py` | CLI and `.env` loader |
| `config.toml` | endpoints, models, runtime limits |
| `.env.example` | template for environment variables (`DEEPSEEK_API_KEY`) |
| `hive/orchestrator.py` | manager agent, its tools, and endpoint fallback |
| `hive/runner.py` | tool-use loop, schema validation, step cap |
| `hive/tools.py` | tool library for workers |
| `hive/registry.py` | SQLite store for agents and tasks |
| `hive/llm.py` | OpenAI/DeepSeek-compatible client, auth resolution, reasoning & `<think>` handling |
