"""Minimal client for OpenAI-compatible chat APIs (DeepSeek, Ollama, vLLM, OpenAI). Stdlib only."""
import json
import os
import re
import urllib.error
import urllib.request

THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class LLMError(Exception):
    """Endpoint unreachable or returned something unusable."""


def resolve_api_key(endpoint_cfg):
    """Resolve API key from endpoint config, environment variable, or default."""
    if not isinstance(endpoint_cfg, dict):
        return os.environ.get("DEEPSEEK_API_KEY", "local")
    if endpoint_cfg.get("api_key"):
        val = str(endpoint_cfg["api_key"]).strip()
        if val.startswith("${") and val.endswith("}"):
            env_name = val[2:-1].strip()
            val = os.environ.get(env_name, "")
            if not val:
                raise LLMError(f"missing environment variable '{env_name}' (set it in .env)")
            return val
        return val
    env_name = endpoint_cfg.get("api_key_env")
    if env_name:
        val = os.environ.get(env_name, "").strip()
        if not val:
            raise LLMError(f"missing environment variable '{env_name}' (set it in .env)")
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
