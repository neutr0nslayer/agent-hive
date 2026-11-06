"""The tool-use loop shared by the orchestrator and every worker agent."""
import json

from .llm import chat

TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def tool_schemas(handlers):
    return [{"type": "function",
             "function": {"name": n, "description": d, "parameters": schema}}
            for n, (d, schema, _) in handlers.items()]


def _check(args, schema):
    """Validate args against a flat JSON schema. Drops unknown keys (small models add them)."""
    if not isinstance(args, dict):
        return None, "arguments must be a JSON object"
    props = schema.get("properties", {})
    clean = {k: v for k, v in args.items() if k in props}
    for k in schema.get("required", []):
        if k not in clean:
            return None, f"missing required argument '{k}'"
    for k, v in clean.items():
        t = props[k].get("type")
        if t in TYPES and not TYPES[t](v):
            return None, f"argument '{k}' must be of type {t}"
    return clean, None


def _execute(call, handlers):
    if call["name"] not in handlers:
        return f"ERROR: unknown tool '{call['name']}'. Available: {', '.join(handlers)}"
    _, schema, fn = handlers[call["name"]]
    try:
        args = json.loads(call["arguments"] or "{}")
    except json.JSONDecodeError as e:
        return f"ERROR: arguments are not valid JSON ({e}). Call the tool again with valid JSON."
    args, err = _check(args, schema)
    if err:
        return f"ERROR: {err}. Fix the arguments and call the tool again."
    try:
        out = fn(**args)
    except Exception as e:  # tool failures go back to the model, not up the stack
        return f"ERROR: {type(e).__name__}: {e}"
    return out if isinstance(out, str) else json.dumps(out, default=str)


def run_loop(base_url, model, messages, handlers, max_steps=8, log=print, label="agent", api_key="local"):
    """Run until the model answers without a tool call, or the step cap is hit.

    `messages` must already contain the system prompt and the user turn.
    Raises LLMError if the endpoint fails.
    """
    tools = tool_schemas(handlers) if handlers else None
    for _ in range(max_steps):
        content, calls, native, reasoning = chat(
            base_url, model, messages, tools=tools, api_key=api_key
        )
        if not native:  # text-recovered calls only count if they name a real tool
            calls = [c for c in calls if c["name"] in handlers]
        if not calls:
            return content or "(the agent returned an empty answer)"
        asst_msg = {
            "role": "assistant",
            "content": "" if not native else content,
            "tool_calls": [{"id": c["id"], "type": "function",
                            "function": {"name": c["name"],
                                         "arguments": c["arguments"]}}
                           for c in calls],
        }
        if reasoning is not None:
            asst_msg["reasoning_content"] = reasoning
        messages.append(asst_msg)
        for c in calls:
            log(f"[{label}] → {c['name']}({_short(c['arguments'])})")
            result = _execute(c, handlers)
            log(f"[{label}] ← {_short(result)}")
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})

    log(f"[{label}] step limit ({max_steps}) reached; asking for a final answer")
    messages.append({"role": "user", "content":
                     "Step limit reached. Reply now with your best final answer. Do not call tools."})
    content, _, _, _ = chat(base_url, model, messages, api_key=api_key)
    return content or "(no answer after step limit)"


def _short(s, n=120):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n] + "…"
