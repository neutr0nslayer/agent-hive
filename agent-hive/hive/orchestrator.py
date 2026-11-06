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
- name: lowercase_snake_case, short (e.g. web_researcher, file_writer)
- system_prompt: 3-8 sentences covering the agent's role, how it should work, and what a good final answer looks like
- tools: only names from the list above
- leave model and endpoint empty unless the user asks for something specific; available endpoints: {endpoints}
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
            "list_agents": ("List existing worker agents with their descriptions and tools.",
                            {"type": "object", "properties": {}, "required": []},
                            self.t_list_agents),
            "create_agent": ("Create (or update) a worker agent.", {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string", "description": "one line: what it does"},
                    "system_prompt": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "model": {"type": "string"},
                    "endpoint": {"type": "string"},
                },
                "required": ["name", "description", "system_prompt", "tools"]},
                self.t_create_agent),
            "assign_task": ("Give a task to a worker agent and wait for its result.", {
                "type": "object",
                "properties": {"agent": {"type": "string"},
                               "task": {"type": "string",
                                        "description": "self-contained instructions"}},
                "required": ["agent", "task"]},
                self.t_assign_task),
            "recent_tasks": ("Show recent tasks and their results.", {
                "type": "object", "properties": {"limit": {"type": "integer"}}, "required": []},
                self.t_recent_tasks),
        }

    # --- endpoints ---------------------------------------------------------
    def _endpoint(self, endpoint):
        try:
            ep = self.cfg["endpoints"][endpoint]
            return ep["base_url"], resolve_api_key(ep)
        except KeyError:
            raise LLMError(f"unknown endpoint '{endpoint}' (check config.toml)")

    def _run(self, endpoint, model, messages, handlers, label):
        rt = self.cfg["runtime"]
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
        system = ORCHESTRATOR_PROMPT.format(
            library=self.lib.describe(), endpoints=", ".join(self.cfg["endpoints"]))
        msgs = [{"role": "system", "content": system}]
        for u, a in self.history:
            msgs += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
        msgs.append({"role": "user", "content": user_text})
        oc = self.cfg["orchestrator"]
        answer = self._run(oc["endpoint"], oc["model"], msgs, self.meta, "orchestrator")
        self.history.append((user_text, answer))
        self.history = self.history[-self.cfg["runtime"]["history_turns"]:]
        return answer

    def run_worker(self, name, task):
        spec = self.reg.get_agent(name)
        if not spec:
            return f"ERROR: no agent named '{name}'. Call list_agents or create_agent first."
        tid = self.reg.start_task(name, task)
        msgs = [{"role": "system", "content": spec.system_prompt + WORKER_SUFFIX},
                {"role": "user", "content": task}]
        try:
            result = self._run(spec.endpoint, spec.model, msgs,
                               self.lib.subset(spec.tools), name)
        except LLMError as e:
            self.reg.finish_task(tid, "failed", str(e))
            return f"ERROR: task #{tid} failed: {e}"
        self.reg.finish_task(tid, "done", result)
        return f"[task #{tid} done]\n{result}"

    # --- orchestrator tools --------------------------------------------------
    def t_list_agents(self):
        agents = self.reg.list_agents()
        if not agents:
            return "No agents exist yet."
        return json.dumps([{"name": a.name, "description": a.description, "tools": a.tools}
                           for a in agents], indent=1)

    def t_create_agent(self, name, description, system_prompt, tools, model="", endpoint=""):
        name = name.strip().lower()
        if not NAME_RE.match(name):
            return "ERROR: name must be lowercase_snake_case, 2-40 chars, starting with a letter"
        bad = [t for t in tools if t not in self.lib.names()]
        if bad:
            return f"ERROR: unknown tools {bad}. Allowed: {self.lib.names()}"
        w = self.cfg["workers"]
        endpoint = endpoint or w["endpoint"]
        if endpoint not in self.cfg["endpoints"]:
            return f"ERROR: unknown endpoint '{endpoint}'. Use one of {list(self.cfg['endpoints'])}"
        existed = self.reg.get_agent(name) is not None
        self.reg.save_agent(AgentSpec(name, description, system_prompt, list(dict.fromkeys(tools)),
                                      endpoint, model or w["model"]))
        return f"{'Updated' if existed else 'Created'} agent '{name}'."

    def t_assign_task(self, agent, task):
        return self.run_worker(agent, task)

    def t_recent_tasks(self, limit=5):
        rows = self.reg.recent_tasks(max(1, min(limit, 20)))
        for r in rows:
            r["result"] = (r["result"] or "")[:300]
        return json.dumps(rows, indent=1) if rows else "No tasks yet."
