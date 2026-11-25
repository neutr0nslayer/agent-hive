"""Minimal client for OpenAI-compatible chat APIs (DeepSeek, Ollama, vLLM, OpenAI). Stdlib only."""
import json
import os
import re
import threading
import urllib.error
import urllib.request

THINK_RE = re.compile(r"<think>(.*?)</think>", re.S)
_usage_tls = threading.local()
_stream_tls = threading.local()


def start_usage_tracking():
    """Begin accumulating token usage for the current thread."""
    _usage_tls.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def finish_usage_tracking() -> dict:
    """Stop accumulating and return token usage dict for the current thread."""
    u = getattr(_usage_tls, "usage", None) or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    _usage_tls.usage = None
    return dict(u)


def start_stream_emitter(fn):
    """Attach a real-time event callback `fn(event_dict)` for the current thread."""
    _stream_tls.emitter = fn
    _stream_tls.label = "agent"


def stop_stream_emitter():
    _stream_tls.emitter = None
    _stream_tls.label = "agent"


def set_stream_label(label: str):
    _stream_tls.label = label or "agent"


def emit_stream_event(event: dict):
    fn = getattr(_stream_tls, "emitter", None)
    if fn is not None:
        try:
            fn(event)
        except Exception:
            pass


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


def _consume_sse_response(resp, label: str):
    """Read an OpenAI/DeepSeek text/event-stream response line-by-line and emit live deltas."""
    content_parts = []
    reasoning_parts = []
    tc_by_idx = {}
    announced_tools = set()
    usage_data = None
    in_think = False
    think_buf = ""

    def _emit_content_or_think(piece: str):
        nonlocal in_think, think_buf
        think_buf += piece
        while think_buf:
            if not in_think:
                idx = think_buf.find("<think>")
                if idx == -1:
                    # Hold back partial '<think' prefix at the end if any
                    keep = 0
                    for k in range(min(6, len(think_buf)), 0, -1):
                        if "<think>".startswith(think_buf[-k:]):
                            keep = k
                            break
                    out = think_buf[:-keep] if keep else think_buf
                    think_buf = think_buf[-keep:] if keep else ""
                    if out and label.startswith("orchestrator:"):
                        emit_stream_event({"type": "content_delta", "label": label, "delta": out})
                    break
                before = think_buf[:idx]
                if before and label.startswith("orchestrator:"):
                    emit_stream_event({"type": "content_delta", "label": label, "delta": before})
                think_buf = think_buf[idx + 7:]
                in_think = True
            else:
                idx = think_buf.find("</think>")
                if idx == -1:
                    keep = 0
                    for k in range(min(7, len(think_buf)), 0, -1):
                        if "</think>".startswith(think_buf[-k:]):
                            keep = k
                            break
                    out = think_buf[:-keep] if keep else think_buf
                    think_buf = think_buf[-keep:] if keep else ""
                    if out:
                        emit_stream_event({"type": "reasoning_delta", "label": label, "delta": out})
                    break
                inner = think_buf[:idx]
                if inner:
                    emit_stream_event({"type": "reasoning_delta", "label": label, "delta": inner})
                think_buf = think_buf[idx + 8:]
                in_think = False

    for raw_line in resp:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        if chunk.get("usage"):
            usage_data = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice0 = choices[0] or {}
        delta = choice0.get("delta") if "delta" in choice0 else (choice0.get("message") or {})
        if not isinstance(delta, dict):
            continue

        r_piece = delta.get("reasoning_content")
        if r_piece:
            reasoning_parts.append(r_piece)
            emit_stream_event({"type": "reasoning_delta", "label": label, "delta": r_piece})

        c_piece = delta.get("content")
        if c_piece:
            content_parts.append(c_piece)
            _emit_content_or_think(c_piece)

        for tc in (delta.get("tool_calls") or []):
            idx = tc.get("index", 0)
            entry = tc_by_idx.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                entry["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                entry["name"] += fn["name"]
                if idx not in announced_tools and entry["name"]:
                    announced_tools.add(idx)
                    emit_stream_event({
                        "type": "status",
                        "label": label,
                        "text": f"{label} → calling {entry['name']}…",
                    })
            if fn.get("arguments"):
                arg_part = fn["arguments"]
                entry["arguments"] += arg_part if isinstance(arg_part, str) else json.dumps(arg_part)

    # Flush any held-back tail buffer
    if think_buf:
        if in_think:
            emit_stream_event({"type": "reasoning_delta", "label": label, "delta": think_buf})
        elif label.startswith("orchestrator:"):
            emit_stream_event({"type": "content_delta", "label": label, "delta": think_buf})

    raw_content = "".join(content_parts)
    reasoning_content = "".join(reasoning_parts) if reasoning_parts else None
    calls = []
    for i, k in enumerate(sorted(tc_by_idx)):
        e = tc_by_idx[k]
        calls.append({
            "id": e["id"] or f"call_{i}",
            "name": e["name"],
            "arguments": e["arguments"] or "{}",
        })
    return raw_content, calls, reasoning_content, {"usage": usage_data} if usage_data else {}


def chat(base_url, model, messages, tools=None, temperature=0.2, timeout=600, api_key="local"):
    """Return (content, tool_calls, native, reasoning_content).

    Automatically streams SSE chunks in real time when a thread-local stream emitter is active.
    """
    streaming_active = getattr(_stream_tls, "emitter", None) is not None
    label = getattr(_stream_tls, "label", "agent")

    payload = {"model": model, "messages": messages, "stream": bool(streaming_active)}
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
            ctype = (r.headers.get_content_type() or "").lower()
            if streaming_active and "event-stream" in ctype:
                raw_content, calls, reasoning_content, usage_wrap = _consume_sse_response(r, label)
                content = THINK_RE.sub("", raw_content).strip()
                _record_usage(usage_wrap, messages, content)
                if calls:
                    return content, calls, True, reasoning_content
                return content, _parse_text_call(content), False, reasoning_content
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

    raw_msg_content = msg.get("content") or ""
    think_matches = THINK_RE.findall(raw_msg_content)
    content = THINK_RE.sub("", raw_msg_content).strip()
    _record_usage(data, messages, content)
    reasoning_content = msg.get("reasoning_content")

    if streaming_active:
        r_emit = reasoning_content or ("\n".join(t.strip() for t in think_matches if t.strip()))
        if r_emit:
            emit_stream_event({"type": "reasoning_delta", "label": label, "delta": r_emit})

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
