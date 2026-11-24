"""Minimal client for OpenAI-compatible chat APIs (DeepSeek, Ollama, vLLM, OpenAI). Stdlib only."""
import json
import os
import re
import threading
import urllib.error
import urllib.request

THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_usage_tls = threading.local()


def start_usage_tracking():
    """Begin accumulating token usage for the current thread."""
    _usage_tls.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def finish_usage_tracking() -> dict:
    """Stop accumulating and return token usage dict for the current thread."""
    u = getattr(_usage_tls, "usage", None) or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    _usage_tls.usage = None
    return dict(u)


def _record_usage(data, messages, content):
    acc = getattr(_usage_tls, "usage", None)
    if acc is None:
        return
    u = data.get("usage") if isinstance(data, dict) else None
    if isinstance(u, dict) and (u.get("total_tokens") or u.get("prompt_tokens") or u.get("completion_tokens")):
        pt = int(u.get("prompt_tokens") or 0)
        ct = int(u.get("completion_tokens") or 0)
        tt = int(u.get("total_tokens") or (pt + ct))
    else:
        pt = max(1, len(json.dumps(messages, default=str)) // 4)
        ct = max(1, len(content or "") // 4)
        tt = pt + ct
    acc["prompt_tokens"] += pt
    acc["completion_tokens"] += ct
    acc["total_tokens"] += tt


class LLMError(Exception):
    """Endpoint unreachable or returned something unusable."""


PLACEHOLDERS = {"your_deepseek_api_key_here", "sk-your-deepseek-api-key", ""}


def resolve_api_key(endpoint_cfg):
    """Resolve API key from endpoint config, environment variable, or default."""
    if not isinstance(endpoint_cfg, dict):
        return os.environ.get("DEEPSEEK_API_KEY", "local")
    if endpoint_cfg.get("api_key"):
        val = str(endpoint_cfg["api_key"]).strip()
        if val.startswith("${") and val.endswith("}"):
            env_name = val[2:-1].strip()
            val = os.environ.get(env_name, "").strip()
            if val in PLACEHOLDERS:
                raise LLMError(f"missing or placeholder '{env_name}' — set your real DeepSeek API key in .env")
            return val
        return val
    env_name = endpoint_cfg.get("api_key_env")
    if env_name:
        val = os.environ.get(env_name, "").strip()
        if val in PLACEHOLDERS:
            raise LLMError(f"missing or placeholder '{env_name}' — set your real DeepSeek API key in .env")
        return val
    return "local"


def chat(base_url, model, messages, tools=None, temperature=0.2, timeout=600, api_key="local"):
    """Return (content, tool_calls, native, reasoning_content).

    tool_calls: list of {"id", "name", "arguments"(json str)}.
    native: False when calls were recovered from plain text (small models do this).
    reasoning_content: optional reasoning string returned by thinking models (e.g. deepseek-reasoner / R1).
    """
    payload = {"model": model, "messages": messages, "stream": False}
    # DeepSeek R1 / reasoner models ignore or reject temperature when reasoning
    if "reasoner" not in model.lower() and "-r1" not in model.lower():
        payload["temperature"] = temperature
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise LLMError(f"{base_url} returned HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise LLMError(f"could not reach {base_url}: {e}") from e

    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"unexpected response from {base_url}: {str(data)[:300]}") from e

    content = THINK_RE.sub("", msg.get("content") or "").strip()
    _record_usage(data, messages, content)
    reasoning_content = msg.get("reasoning_content")
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments", "{}")
        if not isinstance(args, str):
            args = json.dumps(args)
        calls.append({"id": tc.get("id") or f"call_{i}", "name": fn.get("name", ""),
                      "arguments": args})
    if calls:
        return content, calls, True, reasoning_content
    return content, _parse_text_call(content), False, reasoning_content


def _parse_text_call(text):
    """Recover a tool call a model wrote as JSON text, e.g. {"name": ..., "arguments": {...}}."""
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[m.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("name"), str):
            args = obj.get("arguments", obj.get("parameters", {}))
            if isinstance(args, (dict, str)):
                return [{"id": "call_text", "name": obj["name"],
                         "arguments": args if isinstance(args, str) else json.dumps(args)}]
    return []
