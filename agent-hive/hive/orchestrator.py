"""The manager agent: creates worker agents and assigns them tasks."""
import json
import re

from .llm import LLMError, resolve_api_key
from .registry import AgentSpec
from .runner import run_loop

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

ORCHESTRATOR_PROMPT = """You are the orchestrator of a team of AI agents.
You do not do the work yourself. You manage agents:
1. For a work request, call list_agents to see which agents exist.
2. If an existing agent fits, call assign_task with a clear, self-contained task description.
3. If none fits, call create_agent, then assign_task.
4. Split big requests into several assign_task calls, one step at a time, and pass earlier results into later tasks when needed.
5. When the work is done, report the results to the user briefly and plainly.
If the user is only chatting or asking about the team, answer directly without tools.

Tools that worker agents can be given (give each agent only what it needs):
{library}

Rules for create_agent:
- name: lowercase_snake_case, short (e.g. web_researcher, deep_reasoner, file_writer)
- system_prompt: 3-8 sentences covering the agent's role, how it should work, and what a good final answer looks like
- tools: only names from the list above
- model & endpoint: leave empty to use the worker default ({default_worker}), OR pick a specific model/alias or endpoint if the task or user calls for it (e.g. reasoning vs fast chat).
  Available endpoints: {endpoints}
  Available model aliases: {models}
"""

WORKER_SUFFIX = """

You work inside a sandboxed workspace folder; always use relative file paths.
Call tools only when needed. When you are finished, reply with a concise final answer and no tool call.
If something fails, say what failed instead of guessing."""


class Orchestrator:
    def __init__(self, cfg, registry, library, log=print):
        self.cfg, self.reg, self.lib, self.log = cfg, registry, library, log
        self.history = []  # past (user, assistant) exchanges, final texts only
        self.meta = {
            "list_agents": ("List existing worker agents with their descriptions, models, and tools.",
                            {"type": "object", "properties": {}, "required": []},
                            self.t_list_agents),
            "create_agent": ("Create (or update) a worker agent.", {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string", "description": "one line: what it does"},
                    "system_prompt": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "model": {"type": "string", "description": "optional model ID or alias (e.g. r1, v4, deepseek-reasoner, deepseek-chat)"},
                    "endpoint": {"type": "string", "description": "optional endpoint name"},
                },
                "required": ["name", "description", "system_prompt", "tools"]},
                self.t_create_agent),
            "assign_task": ("Give a task to a worker agent and wait for its result.", {
                "type": "object",
                "properties": {
                    "agent": {"type": "string"},
                    "task": {"type": "string", "description": "self-contained instructions"},
                    "model": {"type": "string", "description": "optional one-off model override for this task"},
                    "endpoint": {"type": "string", "description": "optional one-off endpoint override for this task"},
                },
                "required": ["agent", "task"]},
                self.t_assign_task),
            "recent_tasks": ("Show recent tasks and their results.", {
                "type": "object", "properties": {"limit": {"type": "integer"}}, "required": []},
                self.t_recent_tasks),
        }

    # --- endpoints & models ------------------------------------------------
    def resolve_model(self, model_name, endpoint=None):
        """Resolve a friendly model alias (like 'r1', 'v4', 'v3') from [models] in config.toml."""
        aliases = self.cfg.get("models", {})
        if not model_name:
            return model_name, endpoint
        key = model_name.strip()
        target = aliases.get(key) or aliases.get(key.lower())
        if isinstance(target, dict):
            return target.get("model", key), (endpoint or target.get("endpoint"))
        if isinstance(target, str):
            if "/" in target and target.split("/", 1)[0] in self.cfg["endpoints"]:
                ep, m = target.split("/", 1)
                return m, (endpoint or ep)
            return target, endpoint
        return key, endpoint

    def _endpoint(self, endpoint):
        try:
            ep = self.cfg["endpoints"][endpoint]
            return ep["base_url"], resolve_api_key(ep)
        except KeyError:
            raise LLMError(f"unknown endpoint '{endpoint}' (check config.toml)")

    def _run(self, endpoint, model, messages, handlers, label):
        rt = self.cfg["runtime"]
        model, endpoint = self.resolve_model(model, endpoint)
        url, key = self._endpoint(endpoint)
        try:
            return run_loop(url, model, list(messages), handlers,
                            rt["max_steps"], self.log, label, api_key=key)
        except LLMError as e:
            fb = self.cfg["workers"].get("fallback_endpoint")
            if not fb or fb == endpoint or label == "orchestrator":
                raise
            self.log(f"[{label}] {endpoint} failed ({e}); retrying on {fb}")
            fb_url, fb_key = self._endpoint(fb)
            return run_loop(fb_url, model, list(messages), handlers,
                            rt["max_steps"], self.log, label, api_key=fb_key)

    # --- user entry point ---------------------------------------------------
    def handle(self, user_text):
        oc = self.cfg["orchestrator"]
        w = self.cfg["workers"]
        aliases = self.cfg.get("models", {})
        alias_desc = ", ".join(f"{k} -> {v}" for k, v in aliases.items()) or "(none; pass any raw model ID)"
        system = ORCHESTRATOR_PROMPT.format(
            library=self.lib.describe(),
            endpoints=", ".join(self.cfg["endpoints"]),
            default_worker=f"{w['model']} @ {w['endpoint']}",
            models=alias_desc,
        )
        msgs = [{"role": "system", "content": system}]
        for u, a in self.history:
            msgs += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
        msgs.append({"role": "user", "content": user_text})
        oc_model, oc_ep = self.resolve_model(oc["model"], oc["endpoint"])
        answer = self._run(oc_ep, oc_model, msgs, self.meta, f"orchestrator:{oc_model}")
        self.history.append((user_text, answer))
        self.history = self.history[-self.cfg["runtime"]["history_turns"]:]
        return answer

    def run_worker(self, name, task, model_override="", endpoint_override=""):
        spec = self.reg.get_agent(name)
        if not spec:
            return f"ERROR: no agent named '{name}'. Call list_agents or create_agent first."
        ep = endpoint_override or spec.endpoint
        raw_model = model_override or spec.model
        model, ep = self.resolve_model(raw_model, ep)
        tid = self.reg.start_task(name, task)
        msgs = [{"role": "system", "content": spec.system_prompt + WORKER_SUFFIX},
                {"role": "user", "content": task}]
        try:
            result = self._run(ep, model, msgs,
                               self.lib.subset(spec.tools), f"{name}:{model}")
        except LLMError as e:
            self.reg.finish_task(tid, "failed", str(e))
            return f"ERROR: task #{tid} failed: {e}"
        self.reg.finish_task(tid, "done", result)
        return f"[task #{tid} done ({model} @ {ep})]\n{result}"

    # --- orchestrator tools --------------------------------------------------
    def t_list_agents(self):
        agents = self.reg.list_agents()
        if not agents:
            return "No agents exist yet."
        return json.dumps([{"name": a.name, "description": a.description,
                            "model": a.model, "endpoint": a.endpoint, "tools": a.tools}
                           for a in agents], indent=1)

    def t_create_agent(self, name, description, system_prompt, tools, model="", endpoint=""):
        name = name.strip().lower()
        if not NAME_RE.match(name):
            return "ERROR: name must be lowercase_snake_case, 2-40 chars, starting with a letter"
        bad = [t for t in tools if t not in self.lib.names()]
        if bad:
            return f"ERROR: unknown tools {bad}. Allowed: {self.lib.names()}"
        w = self.cfg["workers"]
        resolved_model, resolved_ep = self.resolve_model(model or w["model"], endpoint or None)
        endpoint = resolved_ep or w["endpoint"]
        if endpoint not in self.cfg["endpoints"]:
            return f"ERROR: unknown endpoint '{endpoint}'. Use one of {list(self.cfg['endpoints'])}"
        existed = self.reg.get_agent(name) is not None
        self.reg.save_agent(AgentSpec(name, description, system_prompt, list(dict.fromkeys(tools)),
                                      endpoint, resolved_model))
        return f"{'Updated' if existed else 'Created'} agent '{name}' [{resolved_model} @ {endpoint}]."

    def t_assign_task(self, agent, task, model="", endpoint=""):
        return self.run_worker(agent, task, model_override=model, endpoint_override=endpoint)

    def t_recent_tasks(self, limit=5):
        rows = self.reg.recent_tasks(max(1, min(limit, 20)))
        for r in rows:
            r["result"] = (r["result"] or "")[:300]
        return json.dumps(rows, indent=1) if rows else "No tasks yet."
