"""Personal WhatsApp client via WAHA Docker sidecar: QR pairing, grouped 2-min unread sync,
last-20 chat context fetching, and per-chat auto-reply & optional context support.
"""
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .formatting import md_to_whatsapp

MAX_WA_CHUNK = 2800
WA_DEFAULT_WAIT_SECONDS = 120   # Default per-chat wait (120s) so senders finish typing all messages
WA_SYNC_INTERVAL_SECONDS = 120  # Alias kept for backward compatibility
WA_SYNC_POLL_SECONDS = 15       # Lightweight loop tick to honor custom per-chat wait times promptly
WA_HISTORY_CONTEXT_LIMIT = 20   # Default recent messages of the chat included as conversation context
WA_MAX_HISTORY_LIMIT = 100      # Customizable per-chat conversation context length up to 100 messages


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


def canonical_chat_id(raw_id: str, key_meta: dict = None) -> str:
    """Normalize WhatsApp chat IDs across @c.us, @s.whatsapp.net, @lid (via remoteJidAlt), and @g.us."""
    km = key_meta if isinstance(key_meta, dict) else {}
    remote_jid = str(km.get("remoteJid") or "").strip()
    remote_alt = str(km.get("remoteJidAlt") or "").strip()

    if remote_jid.endswith("@g.us"):
        return remote_jid
    cid = str(raw_id or remote_jid or "").strip()
    if cid.endswith("@g.us"):
        return cid
    # Resolve modern WhatsApp @lid DM identifiers to canonical @c.us phone JID when available
    if cid.endswith("@lid") and remote_alt:
        cid = remote_alt
    if cid.endswith("@s.whatsapp.net"):
        cid = cid[: -len("@s.whatsapp.net")] + "@c.us"
    elif "@" not in cid and cid:
        digits = "".join(ch for ch in cid if ch.isdigit())
        if digits:
            cid = f"{digits}@c.us"
    return cid


def _normalize_chat_id(chat_id: str) -> str:
    return canonical_chat_id(chat_id)


def _resolve_chat_display_name(cfg: dict, registry, chat_id: str, raw_name: str = "") -> str:
    """Resolve human-readable contact or group name (fetches & caches group subject for @g.us)."""
    cleaned = str(raw_name or "").strip()
    fallback_stem = chat_id.split("@")[0]
    if cleaned and cleaned != chat_id and cleaned != fallback_stem:
        return cleaned

    if registry is not None:
        saved = registry.get_whatsapp_chat_setting(chat_id)
        sname = (saved.get("chat_name") or "").strip()
        if sname and sname != chat_id and sname != fallback_stem:
            return sname

    if chat_id.endswith("@g.us"):
        try:
            enc_id = urllib.parse.quote(chat_id, safe="")
            grp = _waha_request(cfg, "GET", f"/api/{cfg['session']}/groups/{enc_id}")
            if isinstance(grp, dict):
                subj = str(grp.get("subject") or grp.get("name") or "").strip()
                if subj:
                    if registry is not None:
                        registry.upsert_whatsapp_chat_setting(chat_id, chat_name=subj, is_group=True)
                    return subj
        except WhatsAppError:
            pass

    return cleaned or fallback_stem


def _extract_waha_msg_entry(m: dict, default_sender: str, is_group: bool) -> dict | None:
    if not isinstance(m, dict):
        return None
    from_me = bool(m.get("fromMe"))
    data = m.get("_data") or {}
    txt = str(m.get("body") or "").strip()
    if not txt:
        if m.get("hasMedia"):
            txt = "[Media attachment]"
        else:
            return None
    if from_me:
        sender = "Me"
    else:
        push = str(data.get("pushName") or m.get("pushName") or "").strip()
        part = str((data.get("key") or {}).get("participantAlt") or m.get("participant") or "").split("@")[0]
        if is_group:
            sender = push or part or "Member"
        else:
            sender = push or default_sender or part or "Contact"
    ts = m.get("timestamp") or data.get("messageTimestamp")
    try:
        ts_val = float(ts) if ts is not None else None
    except (TypeError, ValueError):
        ts_val = None
    return {
        "id": str(m.get("id") or ""),
        "from_me": from_me,
        "sender": sender,
        "body": txt,
        "timestamp": ts_val,
    }


def fetch_chat_context(
    registry,
    chat_id: str,
    *,
    chat_name: str = "",
    history_limit: int = WA_HISTORY_CONTEXT_LIMIT,
    waha_unread_count: int = 0,
    local_unread_msgs: list[dict] = None,
) -> dict:
    """Fetch up to history_limit (1..100) recent messages of a WhatsApp chat AND all unread messages."""
    cfg = get_whatsapp_config(registry)
    cid = canonical_chat_id(chat_id)
    is_group = cid.endswith("@g.us")
    resolved_name = _resolve_chat_display_name(cfg, registry, cid, chat_name)
    local_unreads = list(local_unread_msgs or [])
    hist_lim = max(1, min(WA_MAX_HISTORY_LIMIT, int(history_limit or WA_HISTORY_CONTEXT_LIMIT)))

    # Ensure we pull enough messages to cover BOTH the chat context window (up to 100) AND ALL unread messages
    needed_for_unread = max(int(waha_unread_count or 0) + 6, len(local_unreads) + 6)
    fetch_limit = min(250, max(hist_lim, needed_for_unread, 20))

    chronological: list[dict] = []
    try:
        enc_id = urllib.parse.quote(cid, safe="")
        raw_msgs = _waha_request(
            cfg, "GET", f"/api/{cfg['session']}/chats/{enc_id}/messages?limit={fetch_limit}"
        )
        if isinstance(raw_msgs, list):
            # WAHA returns newest first; reverse to chronological (oldest -> newest)
            for m in reversed(raw_msgs):
                entry = _extract_waha_msg_entry(m, resolved_name, is_group)
                if entry:
                    chronological.append(entry)
    except WhatsAppError:
        pass

    if not chronological and registry is not None:
        chronological = registry.list_chat_whatsapp_history_local(cid, limit=max(hist_lim, 20))

    # Merge any local webhook unread messages not yet present in chronological list
    seen_ids = {x["id"] for x in chronological if x.get("id")}
    for lm in local_unreads:
        lid = lm.get("id") or ""
        if lid and lid in seen_ids:
            continue
        # Also avoid exact duplicate body at nearly identical timestamp
        dup = any(
            (not c["from_me"])
            and c["body"] == lm.get("body")
            and c.get("timestamp")
            and lm.get("timestamp")
            and abs(float(c["timestamp"]) - float(lm["timestamp"])) <= 5
            for c in chronological[-15:]
        )
        if not dup:
            chronological.append({
                "id": lid,
                "from_me": False,
                "sender": lm.get("sender") or resolved_name,
                "body": lm.get("body") or "",
                "timestamp": lm.get("timestamp"),
            })
            if lid:
                seen_ids.add(lid)

    # Determine ALL unread messages (read every single unread message, even if > 20!)
    unread_list: list[dict] = []
    unread_seen_ids: set[str] = set()

    if waha_unread_count > 0 and chronological:
        incoming_only = [m for m in chronological if not m.get("from_me")]
        for m in incoming_only[-int(waha_unread_count):]:
            unread_list.append(m)
            if m.get("id"):
                unread_seen_ids.add(m["id"])

    for lm in local_unreads:
        lid = lm.get("id") or ""
        if lid and lid in unread_seen_ids:
            continue
        dup = any(
            u["body"] == lm.get("body")
            and u.get("timestamp")
            and lm.get("timestamp")
            and abs(float(u["timestamp"]) - float(lm["timestamp"])) <= 5
            for u in unread_list
        )
        if not dup:
            unread_list.append({
                "id": lid,
                "from_me": False,
                "sender": lm.get("sender") or resolved_name,
                "body": lm.get("body") or "",
                "timestamp": lm.get("timestamp"),
            })
            if lid:
                unread_seen_ids.add(lid)

    # Sort unread_list chronologically
    unread_list.sort(key=lambda x: float(x.get("timestamp") or 0))
    last_history = chronological[-hist_lim:]

    return {
        "chat_id": cid,
        "chat_name": resolved_name,
        "is_group": is_group,
        "history_limit": hist_lim,
        "recent_history": last_history,
        "unread_messages": unread_list,
    }


def format_messages_transcript(messages: list[dict], is_group: bool = False) -> str:
    lines = []
    for m in messages or []:
        who = "Me" if m.get("from_me") else (m.get("sender") or ("Member" if is_group else "Them"))
        ts = m.get("timestamp")
        ts_str = time.strftime("%H:%M", time.localtime(float(ts))) if ts else ""
        prefix = f"[{ts_str}] {who}" if ts_str else who
        lines.append(f"{prefix}: {m.get('body', '')}")
    return "\n".join(lines)


def compute_unread_signature(chat_id: str, unread_messages: list[dict]) -> str:
    parts = [chat_id, str(len(unread_messages))]
    for m in unread_messages:
        parts.append(f"{m.get('id', '')}|{m.get('timestamp', '')}|{m.get('body', '')[:80]}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:20]


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


def mark_chat_read_waha(registry, chat_id: str):
    """Mark chat read in both local SQLite buffer and WAHA session (best-effort)."""
    cid = canonical_chat_id(chat_id)
    if registry is not None:
        registry.mark_whatsapp_read(chat_id=cid)
    cfg = get_whatsapp_config(registry)
    try:
        enc_id = urllib.parse.quote(cid, safe="")
        _waha_request(cfg, "POST", f"/api/{cfg['session']}/chats/{enc_id}/messages/read", {})
    except WhatsAppError:
        pass


def fetch_known_chats(registry=None, limit: int = 80) -> list[dict]:
    """Return known WhatsApp DMs & Groups merged with their per-chat auto_reply and context settings."""
    cfg = get_whatsapp_config(registry)
    saved_list = registry.list_whatsapp_chat_settings() if registry else []
    by_id: dict[str, dict] = {s["chat_id"]: dict(s) for s in saved_list}
    ordered_ids: list[str] = []

    try:
        chats = _waha_request(cfg, "GET", f"/api/{cfg['session']}/chats/overview?limit={int(limit)}")
        if isinstance(chats, list):
            for ch in chats:
                if not isinstance(ch, dict):
                    continue
                raw_id = str(ch.get("id") or "")
                if not raw_id or raw_id.endswith("@broadcast") or raw_id == "status@broadcast":
                    continue
                cid = canonical_chat_id(raw_id)
                if cid in ordered_ids:
                    continue
                ordered_ids.append(cid)
                raw_name = str(ch.get("name") or "")
                name = _resolve_chat_display_name(cfg, registry, cid, raw_name)
                unread = int(ch.get("unreadCount") or (ch.get("_chat") or {}).get("unreadCount") or 0)
                last_msg = ch.get("lastMessage") or {}
                last_ts = last_msg.get("timestamp") or (ch.get("_chat") or {}).get("conversationTimestamp")
                cur = by_id.get(cid) or (
                    registry.get_whatsapp_chat_setting(cid) if registry else {
                        "chat_id": cid, "chat_name": name, "is_group": cid.endswith("@g.us"),
                        "auto_reply": False, "context": "", "auto_reply_count": 0, "last_auto_reply_at": None,
                    }
                )
                cur = dict(cur)
                if name and (not cur.get("chat_name") or cur["chat_name"] == cid.split("@")[0]):
                    cur["chat_name"] = name
                cur["is_group"] = bool(cur.get("is_group") or cid.endswith("@g.us"))
                cur.setdefault("wait_seconds", WA_DEFAULT_WAIT_SECONDS)
                cur.setdefault("history_limit", WA_HISTORY_CONTEXT_LIMIT)
                cur["unread_count"] = unread
                cur["last_message_preview"] = str(last_msg.get("body") or "")[:120]
                cur["last_activity_at"] = float(last_ts) if last_ts else None
                by_id[cid] = cur
    except WhatsAppError:
        pass

    # Ensure all saved per-chat configs (even older chats not in top overview) are included
    for cid, s in list(by_id.items()):
        if cid not in ordered_ids:
            s.setdefault("wait_seconds", WA_DEFAULT_WAIT_SECONDS)
            s.setdefault("history_limit", WA_HISTORY_CONTEXT_LIMIT)
            s.setdefault("unread_count", 0)
            s.setdefault("last_message_preview", "")
            s.setdefault("last_activity_at", s.get("updated_at"))
            ordered_ids.append(cid)

    # Sort: Auto-reply enabled first or active chats in overview order
    return [by_id[cid] for cid in ordered_ids]


def fetch_unread_chats(registry=None, limit: int = 60, history_limit: int = None) -> dict:
    """Fetch ALL unread WhatsApp chats grouped by chat_id, including ALL unread messages
    plus up to `history_limit` (per-chat customizable up to 100) messages of conversation history.
    """
    cfg = get_whatsapp_config(registry)
    local_grouped = registry.list_grouped_unread_whatsapp() if registry else {}

    store_enabled = False
    hint = ""
    waha_by_chat: dict[str, dict] = {}

    try:
        chats = _waha_request(cfg, "GET", f"/api/{cfg['session']}/chats/overview?limit={int(limit)}")
        if isinstance(chats, list):
            store_enabled = True
            for ch in chats:
                if not isinstance(ch, dict):
                    continue
                raw_id = str(ch.get("id") or "")
                if not raw_id or raw_id.endswith("@broadcast") or raw_id == "status@broadcast":
                    continue
                unread = int(ch.get("unreadCount") or (ch.get("_chat") or {}).get("unreadCount") or 0)
                if unread <= 0:
                    continue
                cid = canonical_chat_id(raw_id)
                last_msg = ch.get("lastMessage") or {}
                last_ts = last_msg.get("timestamp") or ch.get("timestamp") or (ch.get("_chat") or {}).get("conversationTimestamp")
                waha_by_chat[cid] = {
                    "chat_id": cid,
                    "raw_name": str(ch.get("name") or ""),
                    "unread_count": unread,
                    "last_body": str(last_msg.get("body") or ""),
                    "last_id": str(last_msg.get("id") or f"wa_chat_{cid}"),
                    "timestamp": float(last_ts) if last_ts else None,
                }
    except WhatsAppError as e:
        msg = str(e)
        if "NOWEB store" in msg or "store.enabled" in msg:
            hint = "Enable NOWEB store sync to list historical unread WhatsApp chats."
        else:
            hint = msg

    all_chat_ids = list(dict.fromkeys(list(waha_by_chat.keys()) + list(local_grouped.keys())))
    items = []

    for cid in all_chat_ids:
        winfo = waha_by_chat.get(cid)
        linfo = local_grouped.get(cid)
        raw_name = (winfo or {}).get("raw_name") or (linfo or {}).get("chat_name") or ""
        w_unread = int((winfo or {}).get("unread_count") or 0)
        l_msgs = (linfo or {}).get("messages") or []

        chat_cfg = (
            registry.get_whatsapp_chat_setting(cid)
            if registry
            else {
                "auto_reply": False,
                "wait_seconds": WA_DEFAULT_WAIT_SECONDS,
                "history_limit": WA_HISTORY_CONTEXT_LIMIT,
                "context": "",
                "last_synced_sig": "",
            }
        )
        eff_hl = max(
            1,
            min(
                WA_MAX_HISTORY_LIMIT,
                int(history_limit if history_limit is not None else (chat_cfg.get("history_limit") or WA_HISTORY_CONTEXT_LIMIT)),
            ),
        )

        ctx = fetch_chat_context(
            registry,
            cid,
            chat_name=raw_name,
            history_limit=eff_hl,
            waha_unread_count=w_unread,
            local_unread_msgs=l_msgs,
        )
        unread_msgs = ctx["unread_messages"]
        if not unread_msgs and winfo and winfo.get("last_body"):
            unread_msgs = [{
                "id": winfo["last_id"],
                "from_me": False,
                "sender": ctx["chat_name"],
                "body": winfo["last_body"],
                "timestamp": winfo.get("timestamp"),
            }]
        if not unread_msgs:
            continue

        total_unread = max(w_unread, len(unread_msgs))
        wait_s = int(
            chat_cfg.get("wait_seconds")
            if chat_cfg.get("wait_seconds") is not None
            else WA_DEFAULT_WAIT_SECONDS
        )
        unread_transcript = format_messages_transcript(unread_msgs, is_group=ctx["is_group"])
        history_transcript = format_messages_transcript(ctx["recent_history"], is_group=ctx["is_group"])
        latest_ts = (
            unread_msgs[-1].get("timestamp")
            or (winfo or {}).get("timestamp")
            or (linfo or {}).get("last_ts")
            or time.time()
        )
        sig = compute_unread_signature(cid, unread_msgs)
        already_synced = bool(chat_cfg.get("last_synced_sig") == sig)
        elapsed = max(0.0, time.time() - float(latest_ts)) if latest_ts else float(wait_s)
        wait_rem = 0 if already_synced else max(0, int(round(wait_s - elapsed)))

        items.append({
            "id": f"wa_group_{cid}",
            "chat_id": cid,
            "sender_name": ctx["chat_name"],
            "chat_name": ctx["chat_name"],
            "is_group": ctx["is_group"],
            "unread_count": total_unread,
            "unread_messages": unread_msgs,
            "recent_history": ctx["recent_history"],
            "history_count": len(ctx["recent_history"]),
            "history_limit": eff_hl,
            "body": unread_transcript or "(non-text message)",
            "history_text": history_transcript,
            "timestamp": float(latest_ts) if latest_ts else None,
            "source": "grouped_sync" if (winfo and linfo) else ("waha_store" if winfo else "webhook"),
            "auto_reply": bool(chat_cfg.get("auto_reply")),
            "wait_seconds": wait_s,
            "wait_remaining_seconds": wait_rem,
            "context": chat_cfg.get("context") or "",
            "signature": sig,
            "already_synced": already_synced,
        })

    items.sort(key=lambda x: float(x.get("timestamp") or 0), reverse=True)
    return {"store_enabled": store_enabled, "items": items, "hint": hint}


class WhatsAppSyncManager:
    """Background sync worker that honors each chat's customizable `wait_seconds` (default 2 min),
    groups all unread messages + last 20 chat messages + per-chat context, and either auto-replies
    (when enabled for that chat) or prepares a single grouped draft.
    """

    def __init__(self, orch, op_lock: threading.Lock, poll_seconds: int = WA_SYNC_POLL_SECONDS):
        self.orch = orch
        self.reg = orch.reg
        self.op_lock = op_lock
        self.poll_seconds = int(poll_seconds)
        self.default_wait_seconds = WA_DEFAULT_WAIT_SECONDS
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sync_lock = threading.Lock()
        self.last_sync_at: float | None = None
        self.next_sync_at: float | None = None
        self.next_ready_at: float | None = None
        self.waiting_chats: int = 0
        self.syncing: bool = False
        self.last_summary: dict = {"synced_chats": 0, "auto_replied": 0, "drafts_updated": 0, "waiting_chats": 0}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.next_sync_at = time.time() + self.poll_seconds
        self._thread = threading.Thread(target=self._loop, name="hive-wa-sync", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def status_dict(self) -> dict:
        return {
            "poll_seconds": self.poll_seconds,
            "default_wait_seconds": self.default_wait_seconds,
            "interval_seconds": self.default_wait_seconds,
            "last_sync_at": self.last_sync_at,
            "next_sync_at": self.next_sync_at,
            "next_ready_at": self.next_ready_at,
            "waiting_chats": self.waiting_chats,
            "syncing": self.syncing,
            "last_summary": self.last_summary,
        }

    def _loop(self):
        while not self._stop.wait(5.0):
            now = time.time()
            if self.next_sync_at is not None and now >= self.next_sync_at:
                try:
                    self.sync_once(manual=False)
                except Exception as e:
                    self.orch.log(f"[whatsapp-sync] error: {e}")
                finally:
                    self.next_sync_at = time.time() + self.poll_seconds

    def sync_once(self, manual: bool = False) -> dict:
        if not self._sync_lock.acquire(blocking=manual):
            return {"busy": True, **self.status_dict()}
        self.syncing = True
        now = time.time()
        synced_chats = 0
        auto_replied = 0
        drafts_updated = 0
        waiting_chats = 0
        nearest_ready_at: float | None = None
        errors = []
        try:
            st = get_session_status(self.reg)
            if st.get("status") != "WORKING":
                self.last_sync_at = now
                self.waiting_chats = 0
                self.next_ready_at = None
                if manual:
                    self.next_sync_at = time.time() + self.poll_seconds
                return {
                    "skipped": True,
                    "reason": f"WhatsApp session is {st.get('status')}",
                    **self.status_dict(),
                }

            res = fetch_unread_chats(self.reg, limit=60, history_limit=None)
            grouped_items = res.get("items") or []

            for item in grouped_items:
                cid = item["chat_id"]
                sig = item["signature"]
                chat_cfg = self.reg.get_whatsapp_chat_setting(cid)
                wait_s = int(
                    chat_cfg.get("wait_seconds")
                    if chat_cfg.get("wait_seconds") is not None
                    else self.default_wait_seconds
                )

                # Skip if this exact unread batch was already processed (unless manual force-sync has no draft yet)
                if not manual and chat_cfg.get("last_synced_sig") == sig:
                    continue
                if manual and chat_cfg.get("last_synced_sig") == sig and not chat_cfg.get("auto_reply"):
                    existing_drf = self.reg.find_draft_by_external_id("whatsapp", f"wa_chat:{cid}")
                    if existing_drf and existing_drf.get("status") == "pending":
                        continue

                # Enforce this chat's customizable wait_seconds since its latest message arrived
                latest_ts = float(item.get("timestamp") or 0)
                first_ts = float(
                    (item["unread_messages"][0].get("timestamp") if item.get("unread_messages") else 0)
                    or latest_ts
                )
                max_hold = max(wait_s * 3, wait_s + 300)
                if not manual and wait_s > 0 and latest_ts > 0:
                    now_check = time.time()
                    age = now_check - latest_ts
                    batch_age = now_check - first_ts if first_ts > 0 else age
                    if age < wait_s and batch_age < max_hold:
                        waiting_chats += 1
                        ready_at = latest_ts + wait_s
                        if nearest_ready_at is None or ready_at < nearest_ready_at:
                            nearest_ready_at = ready_at
                        continue

                synced_chats += 1
                auto_on = bool(chat_cfg.get("auto_reply"))
                custom_ctx = chat_cfg.get("context") or ""

                try:
                    with self.op_lock:
                        drf = self.orch.draft_whatsapp_reply(
                            sender_name=item["chat_name"],
                            chat_id=cid,
                            is_group=item["is_group"],
                            unread_messages=item["unread_messages"],
                            recent_history=item["recent_history"],
                            history_limit=int(chat_cfg.get("history_limit") or WA_HISTORY_CONTEXT_LIMIT),
                            incoming_text=item["body"],
                            external_id=f"wa_chat:{cid}",
                            chat_context=custom_ctx,
                            auto_mode=auto_on,
                        )

                    if auto_on and drf.get("draft_reply", "").strip():
                        send_whatsapp_message(cid, drf["draft_reply"], registry=self.reg)
                        mark_chat_read_waha(self.reg, cid)
                        self.reg.update_inbox_draft(
                            drf["id"],
                            {
                                "status": "sent",
                                "error": "",
                                "sent_at": time.time(),
                                "summary": f"[Auto-Replied] {drf.get('summary') or ''}".strip(),
                            },
                        )
                        self.reg.upsert_whatsapp_chat_setting(
                            cid,
                            chat_name=item["chat_name"],
                            is_group=item["is_group"],
                            last_synced_sig=sig,
                            last_synced_at=time.time(),
                            bump_auto_reply=True,
                        )
                        auto_replied += 1
                    else:
                        self.reg.mark_whatsapp_processed(cid)
                        self.reg.upsert_whatsapp_chat_setting(
                            cid,
                            chat_name=item["chat_name"],
                            is_group=item["is_group"],
                            last_synced_sig=sig,
                            last_synced_at=time.time(),
                        )
                        drafts_updated += 1
                except Exception as e:
                    errors.append(f"{item['chat_name']}: {e}")
                    self.orch.log(f"[whatsapp-sync] failed on {cid}: {e}")

            self.last_sync_at = time.time()
            self.waiting_chats = waiting_chats
            self.next_ready_at = nearest_ready_at
            if manual:
                self.next_sync_at = self.last_sync_at + self.poll_seconds
            self.last_summary = {
                "synced_chats": synced_chats,
                "auto_replied": auto_replied,
                "drafts_updated": drafts_updated,
                "waiting_chats": waiting_chats,
                "unread_chats_total": len(grouped_items),
                "errors": errors,
            }
            return {**self.last_summary, **self.status_dict()}
        finally:
            self.syncing = False
            self._sync_lock.release()


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
