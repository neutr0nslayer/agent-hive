# `agent-hive` — Comprehensive Project Context & Proposal Reference

> **Purpose of this document:** This file provides a complete, self-contained technical specification of the **`agent-hive`** codebase, its current architecture, design constraints, and the **planned extension (Dynamic Tool Creator / Self-Extending Tool Library)**. Feed this document to an LLM when researching related academic papers, open-source implementations, architectural patterns, security/sandboxing approaches, and evaluation benchmarks for a project proposal.

---

## 1. Executive Summary

**`agent-hive`** is a lightweight, zero-dependency, multi-model hierarchical AI agent orchestration framework written in pure **Python 3.11+ standard library** (GPL-3.0 licensed).

Its core design separates **orchestration** from **task execution**:
- A single **Orchestrator (Manager) Agent** interprets user requests, inspects existing specialized worker agents, creates or updates worker agents on demand, decomposes complex tasks into sequential subtasks, and delegates execution.
- **Worker (Child) Agents** execute self-contained tasks inside a sandboxed `workspace/` directory using an explicitly assigned subset of tools from a central `ToolLibrary`.
- **Heterogeneous Multi-Model Routing** lets the user freely mix models and OpenAI-compatible endpoints — e.g., running a flagship chat model (**DeepSeek V4 / `deepseek-chat`**) as the orchestrator while assigning a reasoning model (**DeepSeek R1 / `deepseek-reasoner`**) or local models (Ollama/vLLM) to workers, with live model switching and automatic fallback.

### Why We Are Extending It (Proposal Focus)
Currently, `agent-hive` uses a **strictly static, hardcoded tool library** (`web_fetch`, `read_file`, `write_file`, `list_files`, and optional `run_python`). Models cannot create new tools.

The proposed extension introduces a **Dynamic Tool Creator (Self-Extending Meta-Tool)**:
1. When the orchestrator or a worker lacks the right tool for a task, the system invokes a **Tool Creator** capability.
2. The Tool Creator uses the **highest-capability available model** (specifically `deepseek-chat` / `v4` in the current setup, configurable) to design, implement, validate, and test a new reusable tool.
3. The newly created tool is **persisted** (code, JSON schema, and metadata) so it becomes a permanent part of the tool library for reuse in future tasks and sessions.

---

## 2. Current System Architecture

```text
┌────────────────────────────────────────────────────────────────────────────┐
│                             User CLI (main.py)                             │
│  - Plain text -> Orchestrator.handle()                                     │
│  - Slash commands: /agents, /show, /run, /model, /delete, /tasks, /reset   │
└────────────────────────────────────┬───────────────────────────────────────┘
                                     │
                                     ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                     Orchestrator (hive/orchestrator.py)                    │
│  - Sliding window conversation memory (history_turns = 6)                  │
│  - Model alias & endpoint resolution (config.toml [models], [endpoints])   │
│  - Meta-tools: list_agents, create_agent, assign_task, recent_tasks        │
│  - Fallback endpoint retry for workers (use_local_fallback)                │
│  - Auto-logs every output to workspace/output/<agent>/*.md                 │
└───────────────┬────────────────────────────────────────┬───────────────────┘
                │ (orchestrator loop)                    │ (worker delegation)
                ▼                                        ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                  Shared Tool-Use Loop (hive/runner.py)                     │
│  - run_loop(): ReAct-style iterative tool execution up to max_steps (8)    │
│  - OpenAI function schema generation & flat JSON schema validation         │
│  - Self-correction: JSON/schema/runtime errors returned as tool text       │
│  - Preserves DeepSeek reasoning_content across multi-turn tool calls       │
│  - Recovers tool calls emitted as plain JSON text by smaller models        │
└───────────────┬────────────────────────────────────────┬───────────────────┘
                │                                        │
                ▼                                        ▼
┌──────────────────────────────────┐      ┌──────────────────────────────────┐
│     LLM Client (hive/llm.py)     │      │   Tool Library (hive/tools.py)   │
│  - Pure urllib HTTP client       │      │  - Static vetted tools:          │
│  - OpenAI /chat/completions API  │      │    web_fetch, read_file,         │
│  - DeepSeek R1 & <think> support │      │    write_file, list_files,       │
│  - API key env/config resolution │      │    [optional: run_python]        │
└──────────────────────────────────┘      │  - Path-traversal guard on ws/   │
                                          │  - Output clipped to 6,000 chars │
                                          └──────────────────────────────────┘
                                                         │
        ┌────────────────────────────────────────────────┴───────────────┐
        ▼                                                                ▼
┌──────────────────────────────────┐      ┌──────────────────────────────────┐
│   SQLite Registry (agents.db)    │      │      Workspace (workspace/)      │
│  - agents table (specs, prompts, │      │  - Sandboxed file root for tools │
│    tools, endpoint, model)       │      │  - workspace/output/<agent>/*.md │
│  - tasks table (history, status, │      │    persistent Markdown outputs   │
│    results, timestamps)          │      └──────────────────────────────────┘
└──────────────────────────────────┘
```

### Execution Lifecycle (End-to-End Flow)
1. **Startup (`main.py`)**:
   - Loads `.env` files (stdlib parser; ignores placeholder keys) into `os.environ`.
   - Loads `config.toml` (or falls back to `config.toml.example`) via Python's built-in `tomllib`.
   - Initializes `Registry(db_path)`, `ToolLibrary(ws_path, allow_python)`, and `Orchestrator(cfg, reg, lib, log)`.
2. **User Prompt Handling (`Orchestrator.handle`)**:
   - Builds the orchestrator system prompt dynamically, injecting the current `ToolLibrary.describe()`, available endpoints, configured default worker, and model aliases.
   - Prepends up to `history_turns` prior `(user, assistant)` final-text pairs (intermediate tool traces are omitted to keep context lean).
   - Calls `run_loop()` with the orchestrator's 4 meta-tools (`list_agents`, `create_agent`, `assign_task`, `recent_tasks`).
3. **Worker Creation & Delegation (`t_create_agent`, `t_assign_task` / `run_worker`)**:
   - Orchestrator checks existing agents via `list_agents`.
   - If needed, calls `create_agent`, validating snake_case name (`^[a-z][a-z0-9_]{1,39}$`), ensuring every requested tool exists in `ToolLibrary.names()`, resolving model aliases/endpoints, and upserting an `AgentSpec` into SQLite.
   - Calls `assign_task` (or the user calls `/run NAME TASK` directly to bypass the orchestrator).
   - `run_worker()` creates a `tasks` row (`status="running"`), builds a fresh two-message context (`[system: spec.system_prompt + WORKER_SUFFIX, user: task]`), passes only `ToolLibrary.subset(spec.tools)`, and invokes `run_loop()`.
   - On completion or `LLMError`, updates the `tasks` row (`"done"` or `"failed"`), writes a timestamped Markdown report to `workspace/output/<agent>/`, and returns the result string to the orchestrator.
4. **Tool Execution Loop (`run_loop`)**:
   - Loops up to `max_steps` (default `8`).
   - Calls `llm.chat()`, which returns `(content, calls, native, reasoning_content)`.
   - For text-recovered calls (`native=False`), filters out any JSON object whose `name` is not in `handlers`.
   - If no tool calls remain, returns `content` as the final answer.
   - Otherwise, appends the assistant turn (preserving `reasoning_content` when present), runs each tool sequentially through `_execute()`, logs truncated calls/results, and appends `role: "tool"` messages.
   - If `max_steps` is exhausted, injects a synthetic user message forcing a final text answer with no tools.

---

## 3. Repository & Module Inventory

Total codebase size is ~850 lines of Python across 7 files, making it highly readable and easy to extend.

| File | LOC | Responsibility |
|---|---:|---|
| `main.py` | 161 | Interactive REPL, slash commands, stdlib `.env` loader, runtime wiring |
| `hive/orchestrator.py` | 218 | Manager agent, system prompts, meta-tools, model/endpoint resolution, fallback retry, Markdown output archival |
| `hive/tools.py` | 112 | Vetted `ToolLibrary`, workspace path confinement, HTML stripping, output truncation, optional `run_python` |
| `hive/llm.py` | 102 | OpenAI-compatible HTTP client (`urllib`), API key resolution, DeepSeek R1 reasoning support, `<think>` stripping, JSON text-call recovery |
| `hive/runner.py` | 97 | Shared ReAct tool-use loop, flat JSON schema validator, error-as-observation handling, step cap |
| `hive/registry.py` | 88 | SQLite persistence (`agents` and `tasks` tables) and `AgentSpec` dataclass |
| `tests/mock_server.py` | 71 | Scripted local HTTP server mimicking orchestrator + worker models for GPU-free/API-free integration testing |
| `config.toml.example` | 31 | Configuration template for endpoints, model aliases, orchestrator/worker defaults, and runtime limits |
| `.env.example` | 3 | Environment variable template (`DEEPSEEK_API_KEY`) |

---

## 4. Detailed Component Specifications

### 4.1 LLM Client (`hive/llm.py`)
- **Protocol**: Standard OpenAI `POST {base_url}/chat/completions` (`stream: false`, Bearer auth, 600s default timeout). Compatible with DeepSeek API, OpenAI, Ollama (`/v1`), vLLM, LM Studio, etc.
- **API Key Resolution (`resolve_api_key`)**: Supports inline `api_key`, `${ENV_VAR}` interpolation, `api_key_env` lookup, or `"local"` fallback for unauthenticated local servers. Explicitly rejects placeholder strings.
- **Reasoning Model Support**:
  - Automatically omits `temperature` when `"reasoner"` or `"-r1"` appears in the model ID (required by DeepSeek R1).
  - Extracts and returns `msg.get("reasoning_content")` so `run_loop` can echo it back in subsequent multi-turn tool-call exchanges.
  - Strips `<think>...</think>` blocks (`re.compile(r"<think>.*?</think>", re.S)`) from final `content` for local reasoning models (e.g., Qwen3 / DeepSeek-R1-Distill).
- **Text-Call Recovery (`_parse_text_call`)**: Scans `content` with `json.JSONDecoder().raw_decode` for `{"name": "...", "arguments": ...}` (or `"parameters"`), allowing smaller models without reliable native function-calling to still invoke tools.

### 4.2 Tool Execution Loop (`hive/runner.py`)
- **Tool Handler Contract**: Every tool across both orchestrator and workers uses the uniform tuple format:
  ```python
  handlers: dict[str, tuple[str, dict, Callable]]
  # name -> (description, json_schema_dict, python_callable)
  ```
- **Schema Validation (`_check`)**:
  - Validates top-level `properties` and `required` fields against 6 JSON types (`string`, `integer`, `number`, `boolean`, `array`, `object`).
  - Silently drops unknown/hallucinated extra arguments (helpful for small models).
  - *Current limitation*: Flat validation only — does not recursively validate nested `object` properties or `array` item types.
- **Error Recovery (`_execute`)**: Invalid JSON, unknown tool names, schema mismatches, and runtime `Exception`s are caught and returned as `"ERROR: ..."` strings so the model can self-correct on the next step.

### 4.3 Tool Library (`hive/tools.py`)
- **Built-in Tools**:
  1. `web_fetch(url)`: HTTP/HTTPS fetch (`User-Agent: Mozilla/5.0 agent-hive`, 20s timeout, 1 MB read cap). Strips `<script>`, `<style>`, `<noscript>`, `<svg>`, `<head>` via stdlib `HTMLParser` if content-type contains `html`.
  2. `read_file(path)`: Reads UTF-8 text relative to `workspace/`.
  3. `write_file(path, content)`: Creates parent directories and writes text relative to `workspace/`.
  4. `list_files()`: Lists all files recursively inside `workspace/`.
  5. `run_python(code)` *(disabled by default via `allow_python = false`)*: Writes `workspace/.agent_script.py` and executes `[sys.executable, ".agent_script.py"]` with `cwd=workspace` and a 30-second timeout. **Not a real OS sandbox** — runs with host user permissions.
- **Safety & Context Controls**:
  - Path containment via `(self.ws / rel).resolve().is_relative_to(self.ws)` blocks `../` path traversal.
  - All tool outputs clipped to `MAX_OUT = 6000` characters (`_clip`).

### 4.4 Orchestrator (`hive/orchestrator.py`)
- **Meta-Tools**:
  - `list_agents()`: Returns JSON list of saved agents (`name`, `description`, `model`, `endpoint`, `tools`).
  - `create_agent(name, description, system_prompt, tools, model="", endpoint="")`: Upserts a worker spec.
  - `assign_task(agent, task, model="", endpoint="")`: Runs a worker with optional one-off model/endpoint override.
  - `recent_tasks(limit=5)`: Returns up to 20 recent tasks (results truncated to 300 chars).
- **Governance Boundary**: The orchestrator intentionally has **no `delete_agent` tool**; only the human user can delete agents via `/delete`.
- **Model Aliasing**: `[models]` in `config.toml` supports string targets (`v4 = "deepseek-chat"`), endpoint-prefixed strings (`local_qwen = "ollama/qwen3:8b"`), or inline tables (`{ model = "...", endpoint = "..." }`).
- **Fallback Mechanism**: If a worker call raises `LLMError` and `workers.use_local_fallback = true`, it automatically retries the full loop on `fallback_endpoint` / `fallback_model`. (Orchestrator calls do not silently fall back.)
- **Output Archival**: `_save_output_md()` saves every orchestrator and worker result to `workspace/output/<agent_name>/<prefix>_<timestamp>_<slug>.md`.

### 4.5 Persistence (`hive/registry.py`)
SQLite database (`agents.db`) with two tables:
```sql
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    tools TEXT NOT NULL,         -- JSON list of tool names
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT NOT NULL,        -- "running" | "done" | "failed"
    result TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
```

### 4.6 Configuration (`config.toml.example`) & CLI (`main.py`)
- **Config sections**:
  - `[endpoints.<name>]`: `base_url`, `api_key_env` (or `api_key`).
  - `[models]`: Friendly aliases (`v4`, `v3`, `r1`, `chat`, `reasoner`).
  - `[orchestrator]`: `endpoint`, `model`.
  - `[workers]`: `endpoint`, `model`, `use_local_fallback`, `fallback_endpoint`, `fallback_model`.
  - `[runtime]`: `max_steps = 8`, `workspace = "workspace"`, `db_path = "agents.db"`, `allow_python = false`, `history_turns = 6`.
- **CLI Commands**: `/agents`, `/show NAME`, `/run NAME TASK`, `/model [orch|workers|AGENT] MODEL [EP]`, `/delete NAME`, `/tasks`, `/reset`, `/help`, `/quit`.

---

## 5. Design Philosophy & Current Constraints

When evaluating implementations or designing the proposal, keep these architectural properties in mind:
1. **Minimalism & Zero-Dependency Core**: Built purely on Python 3.11+ stdlib (`urllib`, `tomllib`, `sqlite3`, `json`, `html.parser`, `subprocess`, `ast`). Proposals should decide whether to remain pure-stdlib or justify minimal optional dependencies (e.g., for sandboxing, embeddings, or containers).
2. **Heterogeneous Model Economics**: Uses stronger models sparingly for planning/synthesis and specialized/cheaper/local models for routine worker execution.
3. **Human-in-the-Loop Governance**: Destructive or privileged actions (deleting agents, enabling code execution) are explicitly gated by the human operator.
4. **Current Limitations to Be Aware Of**:
   - **Static tools only**: Cannot acquire new deterministic capabilities at runtime.
   - **No real code sandbox**: `run_python` executes with host user privileges.
   - **Synchronous/sequential execution**: One tool call / one worker at a time (no async or parallel fan-out).
   - **Stateless workers**: Workers start fresh every task and do not retain private multi-turn history.
   - **No automated test suite**: Only a manual `tests/mock_server.py` smoke harness exists.

---

## 6. Proposed Feature: Dynamic Tool Creator (Self-Extending Tool Library)

This section defines the core feature for the **project proposal**.

### 6.1 Problem Statement
In `agent-hive`, worker agents can only compose a fixed set of 4–5 primitive tools (`web_fetch`, `read_file`, `write_file`, `list_files`, optional `run_python`). If a user asks for a task requiring a specialized, repeatable operation — such as parsing structured formats (CSV, XML, YAML, BibTeX), querying a specific REST API, computing statistical/financial metrics, transforming logs, generating charts/SVG, or validating schemas — the system must either fail, approximate the result purely in LLM text (error-prone), or re-generate ad-hoc Python snippets from scratch on every run (wasteful and unverified).

### 6.2 Core Objective & Requirements
Implement a **Tool Creator ("tool-building tool" / meta-tool)** subsystem with the following requirements:

1. **On-Demand Capability Gap Detection**:
   - When the orchestrator (or a worker unable to complete a task with its assigned tools) cannot find an existing tool suitable for the job, it invokes the **Tool Creator** rather than failing or improvising unreliable text outputs.
2. **Highest-Capability Model Routing (`deepseek-chat`)**:
   - Tool generation is a high-leverage, accuracy-critical software engineering step.
   - The Tool Creator must explicitly route synthesis, test generation, and self-repair to the **highest-capability available model** — specifically **`deepseek-chat`** (`v4` in the current `config.toml`, configurable via a dedicated `[tool_creator]` config section or model capability ranking) — even if the requesting worker runs on a smaller or cheaper model.
3. **Persistent Tool Storage & Future Reuse**:
   - Newly built tools must be **saved persistently** (e.g., Python source module(s) under a managed directory such as `custom_tools/` or `workspace/tools/`, plus structured metadata & JSON schemas in `agents.db`).
   - Saved tools must be automatically **hot-loaded immediately** for the current task and **reloaded on startup** for all future sessions so the orchestrator can discover, inspect, and assign them to any worker agent just like built-in tools.

### 6.3 Proposed Conceptual Workflow

```text
User Request
     │
     ▼
Orchestrator inspects existing tools & agents
     │
     ├── Existing tool(s) sufficient ──► create/reuse worker ──► assign_task
     │
     └── Missing capability detected
              │
              ▼
       Tool Creator Meta-Tool (routed to flagship model: deepseek-chat)
              │
              ├─ 1. Check catalog for near-duplicate / reusable tool
              ├─ 2. Synthesize tool specification:
              │      - unique snake_case name
              │      - concise description
              │      - strict JSON Schema for parameters
              │      - Python implementation (stdlib / allowed imports)
              │      - self-contained unit tests (happy path + edge cases)
              ├─ 3. Static safety & AST validation
              │      (block forbidden imports, shell escape, path escape)
              ├─ 4. Sandboxed test execution & iterative self-repair loop
              │      (if tests fail, feed tracebacks back to deepseek-chat up to N retries)
              ├─ 5. Optional human-in-the-loop approval gate (configurable)
              ├─ 6. Persist tool (source file + SQLite metadata + schema + tests)
              └─ 7. Hot-register into ToolLibrary.tools
                        │
                        ▼
       Orchestrator assigns newly created tool to a worker & executes task
```

### 6.4 Natural Integration Points in `agent-hive`

| Existing Component | Proposed Changes |
|---|---|
| `hive/tools.py` (`ToolLibrary`) | Separate `builtin_tools` (immutable, protected namespace) from `custom_tools` (dynamically registered and loaded from disk/DB). Add `register_tool()`, `unregister_tool()`, `reload_custom_tools()`, and execution wrapper enforcing output clipping (`MAX_OUT`), timeout, and sandbox boundaries. |
| `hive/tool_creator.py` *(new module)* | Encapsulate the synthesis prompt, flagship model invocation (`deepseek-chat`), structured output parsing, AST/static safety checks, sandboxed test runner, iterative repair loop, and persistence handoff. |
| `hive/registry.py` (`Registry`) | Add a `tools` table in SQLite (`name`, `description`, `schema_json`, `code_path` / `code`, `tests`, `created_by_model`, `version`, `status`, `use_count`, `failure_count`, `created_at`, `updated_at`) for cataloging, telemetry, and lifecycle management. |
| `hive/orchestrator.py` (`Orchestrator`) | Add meta-tools such as `create_tool` (and optionally `list_tools` / `inspect_tool`). Update `ORCHESTRATOR_PROMPT` so the orchestrator knows when to reuse vs. build a tool. Refresh tool descriptions dynamically so newly created tools are immediately assignable via `create_agent`. Optionally support worker-to-orchestrator escalation when a worker hits a missing capability. |
| `config.toml` | Add a `[tool_creator]` section: `enabled = true`, `endpoint = "deepseek"`, `model = "v4"` (`deepseek-chat`), `max_repair_attempts = 3`, `require_approval = true/false`, `sandbox_mode = "subprocess" | "docker"`, `tools_dir = "custom_tools"`. |
| `main.py` | Add CLI governance commands: `/tools` (list built-in vs. custom tools), `/show-tool NAME` (inspect generated source + schema), `/delete-tool NAME` (human-only deletion, mirroring `/delete`), and optional interactive approval prompts. |

### 6.5 Key Technical Challenges & Design Decisions for the Proposal

1. **Code Synthesis Reliability & Verification**:
   - How to guarantee the generated tool conforms to `agent-hive`'s `(description, json_schema, callable)` interface?
   - How to combine **LLM-generated unit tests**, **schema-to-signature verification** (ensuring Python function kwargs match JSON schema keys and types), and **iterative self-debugging** when tests fail?
2. **Security & Sandboxing of LLM-Generated Code**:
   - Executing dynamically synthesized Python poses prompt-injection and host-compromise risks.
   - Trade-offs between:
     - **AST static analysis + import allowlist** (lightweight, zero-dep, bypassable if not paired with OS isolation),
     - **Subprocess isolation** with `resource.setrlimit` (CPU/RAM caps), clean env vars (no API keys leaked!), and restricted working directory,
     - **Container / microVM / WASM sandboxing** (Docker, Bubblewrap/`bwrap`, Firecracker, WebAssembly/Pyodide).
   - Preventing generated tools from accessing `.env`, `agents.db`, or files outside `workspace/`.
3. **Tool Catalog Growth, Discovery & Deduplication**:
   - As the library grows from 5 tools to 50+ tools, dumping every tool's full schema into the orchestrator's prompt causes token bloat and tool-selection degradation.
   - Strategies: tool categorization/tagging, keyword/BM25 or vector retrieval over tool descriptions, consolidation/refactoring of overlapping tools, and versioning.
4. **Model Tiering & Cost/Latency Economics**:
   - **Amortization hypothesis** (inspired by LATM): Paying higher upfront inference cost once with a flagship model (`deepseek-chat`) to synthesize a deterministic Python tool allows cheaper/faster/reasoning worker models to execute repeated tasks reliably at much lower cost and higher accuracy.
5. **Tool Maintenance, Failure Tracking & Self-Healing**:
   - Tracking `use_count` and `failure_count` in SQLite; flagging flaky tools or triggering automated tool repair/versioning when runtime exceptions occur.
6. **Evaluation Methodology for a Proposal**:
   - Benchmarks comparing **static-tool baseline** vs. **ad-hoc `run_python`** vs. **persistent Tool Creator** across metrics:
     - Task completion accuracy / pass@1,
     - Tool reuse rate across recurring tasks,
     - Token cost & latency amortized over $N$ tasks,
     - First-pass synthesis success rate & repair-loop convergence,
     - Security resistance against adversarial/unsafe tool requests.

---

## 7. Related Literature, Frameworks & Reference Implementations (Search Anchors for LLMs)

Use the following keywords, academic papers, and engineering paradigms when asking an LLM to survey prior art and locate relevant implementations for the proposal:

### Academic Papers & Concepts
- **LATM — *Large Language Models as Tool Makers*** (Cai et al., ICLR 2024): Directly models the **Tool Maker (strong/flagship LLM)** vs. **Tool User (weaker/cheaper LLM)** split with reusable cached Python tools — closely aligned with using `deepseek-chat` to build tools for worker agents.
- **CREATOR — *Tool Creation for Disentangling Abstract and Concrete Reasoning of Large Language Models*** (Qian et al., EMNLP 2023): Framework for creation, decision, execution, and rectification of reusable tools.
- **Voyager — *An Open-Ended Embodied Agent with Large Language Models*** (Wang et al., 2023): Iterative prompting, execution-feedback self-verification, and an ever-growing persistent **Skill Library** indexed for retrieval and reuse.
- **CRAFT — *Customizing LLMs by Creating and Retrieving from Specialized Toolsets*** (Yuan et al., ICLR 2024): Automated tool creation, abstraction/generalization, deduplication, and retrieval.
- **ToolMaker** / **SkillWeaver** / **Alita** / **TroVE** (*Inducing Verifiable and Efficient Toolboxes for Solving Programmatic Tasks*, Wang et al., ICML 2024): Toolbox induction, verification, and library pruning/maintenance.
- **Self-Debugging / Reflexion**: Feeding runtime tracebacks and test failures back into an LLM for iterative code repair.

### Open-Source & Industry Engineering Patterns
- **Model Context Protocol (MCP)**: Standardized tool schema discovery and decoupled process isolation (running generated tools as isolated subprocess/stdio servers).
- **CodeAct / Hugging Face `smolagents`**: Treating Python code as first-class agent actions with AST interpreters (`LocalPythonExecutor`) or sandboxed runtimes (E2B, Docker, Pyodide).
- **Dynamic Tool Registries & Skill Libraries** in frameworks like **CrewAI**, **AutoGen / AG2**, **LangGraph**, **OpenHands**, and **MetaGPT**.
- **Python Sandboxing Approaches**: `ast.NodeVisitor` static validation, `RestrictedPython`, `subprocess` + `resource.setrlimit` + scrubbed `env`, `bubblewrap` (`bwrap`), Docker ephemeral containers, and WebAssembly (`wasmtime` / `pyodide`).

---

## 8. Suggested Prompts to Use with This `context.md`

When feeding `context.md` into an LLM to build your project proposal, you can ask:

1. **Literature & Implementation Survey**:
   > "Read `context.md`. Find the most relevant academic papers and open-source implementations for the proposed Dynamic Tool Creator in `agent-hive` (especially Tool Maker / Tool User architectures like LATM, Voyager skill libraries, and lightweight Python tool sandboxing). Compare how they handle synthesis, verification, persistence, and security."
2. **Architecture & Proposal Drafting**:
   > "Using `context.md`, draft a structured project proposal (Problem Statement, Objectives, Proposed Architecture integrated into `agent-hive`'s modules, Security/Threat Model, Evaluation Plan, and Milestones) for adding the `deepseek-chat`-powered persistent Tool Creator."
3. **Sandboxing Trade-off Analysis**:
   > "Given `agent-hive`'s pure-Python zero-dependency philosophy in `context.md`, compare practical sandboxing implementations for dynamically generated Python tools (AST checks + isolated subprocess vs. bubblewrap vs. Docker vs. WASM) and recommend a phased implementation."
4. **Evaluation & Experiment Design**:
   > "Design a benchmark experiment comparing `agent-hive` with fixed tools vs. ad-hoc `run_python` vs. the proposed persistent Tool Creator (using `deepseek-chat` as Tool Maker and `deepseek-reasoner`/local models as Tool Users), including datasets, baselines, and quantitative metrics."
