"""Personal WhatsApp client via WAHA (WhatsApp HTTP API) Docker sidecar with QR code & unread sync."""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .formatting import md_to_whatsapp

MAX_WA_CHUNK = 2800


class WhatsAppError(Exception):
    """WAHA sidecar unreachable or returned an error."""


def get_whatsapp_config(registry=None) -> dict:
    saved = registry.get_setting("whatsapp_config", {}) if registry else {}
    if not isinstance(saved, dict):
        saved = {}
    return {
        "base_url": (saved.get("base_url") or os.environ.get("WAHA_URL", "http://waha:3000")).rstrip("/"),
        "api_key": (saved.get("api_key") or os.environ.get("WAHA_API_KEY", "")).strip(),
        "session": (saved.get("session") or os.environ.get("WAHA_SESSION", "default")).strip() or "default",
        "webhook_url": (
            saved.get("webhook_url")
            or os.environ.get("WAHA_WEBHOOK_URL", "http://agent-hive:8000/api/v1/webhooks/whatsapp")
        ).strip(),
        "webhook_secret": (saved.get("webhook_secret") or os.environ.get("WAHA_WEBHOOK_SECRET", "")).strip(),
    }


def _waha_request(cfg: dict, method: str, path: str, body=None, expect_bytes: bool = False):
    url = f"{cfg['base_url']}{path}"
    headers = {"Accept": "image/png" if expect_bytes else "application/json"}
    if cfg["api_key"]:
        headers["X-Api-Key"] = cfg["api_key"]
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            if expect_bytes:
                return raw, r.headers.get_content_type() or "image/png"
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise WhatsAppError(f"WAHA HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise WhatsAppError(
            f"Could not reach WAHA sidecar at {cfg['base_url']} ({e}). "
            "Start it with: docker compose --profile whatsapp up -d"
        ) from e


def _session_config_payload(cfg: dict) -> dict:
    webhook_cfg = {"url": cfg["webhook_url"], "events": ["message"]}
    if cfg["webhook_secret"]:
        webhook_cfg["customHeaders"] = [{"name": "X-Hive-Webhook-Secret", "value": cfg["webhook_secret"]}]
    return {
        "webhooks": [webhook_cfg],
        "noweb": {
            "store": {
                "enabled": True,
                "fullSync": True,
            }
        },
    }


def get_session_status(registry=None) -> dict:
    cfg = get_whatsapp_config(registry)
    try:
        data = _waha_request(cfg, "GET", f"/api/sessions/{cfg['session']}")
        noweb_store = ((data.get("config") or {}).get("noweb") or {}).get("store") or {}
        return {
            "reachable": True,
            "session": cfg["session"],
            "status": data.get("status", "UNKNOWN"),
            "me": data.get("me"),
            "store_enabled": bool(noweb_store.get("enabled")),
            "base_url": cfg["base_url"],
        }
    except WhatsAppError as e:
        if "HTTP 404" in str(e):
            return {
                "reachable": True,
                "session": cfg["session"],
                "status": "STOPPED",
                "me": None,
                "store_enabled": False,
                "base_url": cfg["base_url"],
            }
        return {
            "reachable": False,
            "session": cfg["session"],
            "status": "OFFLINE",
            "store_enabled": False,
            "error": str(e),
            "base_url": cfg["base_url"],
        }


def start_session(registry=None) -> dict:
    """Start (or create) the WAHA session with webhook + NOWEB chat store enabled."""
    cfg = get_whatsapp_config(registry)
    payload = {
        "name": cfg["session"],
        "start": True,
        "config": _session_config_payload(cfg),
    }
    try:
        return _waha_request(cfg, "POST", "/api/sessions", payload)
    except WhatsAppError as e:
        if "422" in str(e) or "409" in str(e) or "already" in str(e).lower():
            try:
                _waha_request(cfg, "PUT", f"/api/sessions/{cfg['session']}", {"config": _session_config_payload(cfg)})
            except WhatsAppError:
                pass
            return _waha_request(cfg, "POST", f"/api/sessions/{cfg['session']}/start", {})
        raise


def enable_store_and_restart(registry=None) -> dict:
    """Enable NOWEB store on an existing paired session and restart it without losing auth."""
    cfg = get_whatsapp_config(registry)
    _waha_request(cfg, "PUT", f"/api/sessions/{cfg['session']}", {"config": _session_config_payload(cfg)})
    return _waha_request(cfg, "POST", f"/api/sessions/{cfg['session']}/restart", {})


def stop_session(registry=None) -> dict:
    cfg = get_whatsapp_config(registry)
    return _waha_request(cfg, "POST", f"/api/sessions/{cfg['session']}/stop", {})


def fetch_qr_image(registry=None) -> tuple[bytes, str]:
    """Fetch the live QR code PNG bytes from WAHA for pairing personal WhatsApp."""
    cfg = get_whatsapp_config(registry)
    return _waha_request(cfg, "GET", f"/api/{cfg['session']}/auth/qr?format=image", expect_bytes=True)


def fetch_unread_chats(registry=None, limit: int = 50) -> dict:
    """Fetch unread WhatsApp chats and their latest unread messages from WAHA NOWEB store."""
    cfg = get_whatsapp_config(registry)
    try:
        chats = _waha_request(cfg, "GET", f"/api/{cfg['session']}/chats/overview?limit={int(limit)}")
    except WhatsAppError as e:
        msg = str(e)
        if "NOWEB store" in msg or "store.enabled" in msg:
            return {"store_enabled": False, "items": [], "hint": "Enable NOWEB store sync to list historical unread WhatsApp chats."}
        raise

    if not isinstance(chats, list):
        return {"store_enabled": True, "items": []}

    items = []
    for ch in chats:
        if not isinstance(ch, dict):
            continue
        chat_id = str(ch.get("id") or "")
        if not chat_id or chat_id.endswith("@broadcast") or chat_id == "status@broadcast":
            continue
        # WAHA overview returns unreadCount either top-level or inside _chat
        unread = int(ch.get("unreadCount") or (ch.get("_chat") or {}).get("unreadCount") or 0)
        if unread <= 0:
            continue
        name = str(ch.get("name") or chat_id.split("@")[0])
        last_msg = ch.get("lastMessage") or {}
        last_body = str(last_msg.get("body") or "")
        last_ts = last_msg.get("timestamp") or ch.get("timestamp")

        # Pull up to min(unread, 5) recent incoming messages for richer context
        recent_texts = []
        try:
            enc_id = urllib.parse.quote(chat_id, safe="")
            m_limit = max(1, min(unread, 5))
            msgs = _waha_request(
                cfg, "GET", f"/api/{cfg['session']}/chats/{enc_id}/messages?limit={m_limit}"
            )
            if isinstance(msgs, list):
                for m in reversed(msgs):
                    if isinstance(m, dict) and not m.get("fromMe"):
                        txt = str(m.get("body") or "").strip()
                        if txt:
                            recent_texts.append(txt)
        except WhatsAppError:
            pass

        body = "\n".join(recent_texts) if recent_texts else last_body
        items.append({
            "id": str(last_msg.get("id") or f"wa_chat_{chat_id}"),
            "chat_id": chat_id,
            "sender_name": name,
            "unread_count": unread,
            "body": body or "(non-text message)",
            "timestamp": float(last_ts) if last_ts else None,
            "source": "waha_store",
        })
    return {"store_enabled": True, "items": items}


def split_whatsapp_chunks(text: str, max_chars: int = MAX_WA_CHUNK) -> list[str]:
    """Split formatted WhatsApp text cleanly at paragraph or line boundaries."""
    txt = str(text or "").strip()
    if not txt:
        return ["(empty)"]
    chunks = []
    while len(txt) > max_chars:
        cut = txt.rfind("\n\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = txt.rfind("\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = txt.rfind(" ", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars
        chunks.append(txt[:cut].rstrip())
        txt = txt[cut:].lstrip("\n ")
    if txt:
        chunks.append(txt)
    return chunks


def _normalize_chat_id(chat_id: str) -> str:
    cid = chat_id.strip()
    if "@" not in cid:
        digits = "".join(ch for ch in cid if ch.isdigit())
        cid = f"{digits}@c.us"
    return cid


def send_whatsapp_message(chat_id: str, text: str, registry=None, format_markdown: bool = True) -> dict:
    """Send a readable text message (auto-splits into ordered messages if longer than MAX_WA_CHUNK)."""
    cfg = get_whatsapp_config(registry)
    cid = _normalize_chat_id(chat_id)
    body = md_to_whatsapp(text) if format_markdown else str(text or "")
    chunks = split_whatsapp_chunks(body, max_chars=MAX_WA_CHUNK)
    last_resp = {}
    for idx, chunk in enumerate(chunks):
        if idx > 0:
            time.sleep(0.4)
        last_resp = _waha_request(cfg, "POST", "/api/sendText", {"session": cfg["session"], "chatId": cid, "text": chunk})
    return last_resp


def send_whatsapp_report(
    chat_id: str,
    *,
    title: str,
    status: str,
    target_label: str,
    tokens_used: int,
    body_markdown: str,
    registry=None,
) -> list:
    """Send a structured multi-message WhatsApp report:
    1) Header message with metadata/details
    2) One or more cleanly chunked body messages
    3) Distinct closing/ending message to mark report boundary
    """
    cfg = get_whatsapp_config(registry)
    cid = _normalize_chat_id(chat_id)
    formatted_body = md_to_whatsapp(body_markdown)
    body_chunks = split_whatsapp_chunks(formatted_body, max_chars=MAX_WA_CHUNK)
    total_parts = len(body_chunks)

    ts_str = time.strftime("%Y-%m-%d %H:%M")
    header_msg = (
        f"*SCHEDULED TASK REPORT*\n"
        f"────────────\n"
        f"• *Task:* {title}\n"
        f"• *Status:* {status.upper()}\n"
        f"• *Target:* {target_label}\n"
        f"• *Tokens:* {tokens_used:,}\n"
        f"• *Time:* {ts_str}\n"
        f"• *Body Messages:* {total_parts}"
    )

    end_msg = (
        f"────────────\n"
        f"*END OF REPORT — {title}*\n"
        f"────────────"
    )

    messages = [header_msg]
    for i, chunk in enumerate(body_chunks, start=1):
        prefix = f"*[Part {i}/{total_parts}]*\n\n" if total_parts > 1 else ""
        messages.append(prefix + chunk)
    messages.append(end_msg)

    responses = []
    for idx, msg in enumerate(messages):
        if idx > 0:
            time.sleep(0.45)
        responses.append(
            _waha_request(cfg, "POST", "/api/sendText", {"session": cfg["session"], "chatId": cid, "text": msg})
        )
    return responses
