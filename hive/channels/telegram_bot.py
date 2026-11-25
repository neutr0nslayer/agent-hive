"""Per-user Telegram Bot manager (pure stdlib long-polling):
lets every user control all of agent-hive exclusively from their own Telegram bot."""
import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .email_client import EmailConfigError, send_email_reply
from .formatting import md_to_telegram_html, md_to_whatsapp
from .whatsapp_client import WhatsAppError, send_whatsapp_message
from ..llm import LLMError, finish_usage_tracking, start_usage_tracking
from ..scheduler import ALLOWED_INTERVAL_MINUTES, compute_next_run, validate_trigger

TG_API = "https://api.telegram.org"
MAX_TG_MSG = 3900
ALLOWED_DOC_EXTS = {".md", ".markdown", ".txt", ".csv"}
SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")

TG_HELP = """agent-hive Telegram Bot Commands:
• Send any plain message to chat with the Orchestrator
• Attach a .md, .txt, or .csv file (with optional caption) to analyze it in chat

Commands:
/status — account quota, role & active models
/agents — list worker agents
/run <agent> <task> — run a worker agent directly
/tools — list built-in & custom tools
/create_tool <name> | <desc> | <requirements> — synthesize a new reusable tool with deepseek-chat
/schedules — list your scheduled tasks
/schedule <mins_or_HH:MM> | <name> | <prompt> — create a schedule (delivers here!)
  (intervals: 5,10,15,20,30,45,60,240,480,720,960,1200,1440 or daily HH:MM)
/run_schedule <id> — run a schedule immediately
/pause_schedule <id> | /resume_schedule <id> | /del_schedule <id>
/files — list workspace/ files
/read <path> — read a workspace file
/tasks — recent worker tasks
/emails — summarize important unread emails (last 100)
/draft_emails [instructions] — batch-prepare editable email reply drafts
/drafts — list pending Email/WhatsApp reply drafts
/send_draft <draft_id> — send a pending reply draft
/reset — clear your chat history
/help — show this help"""


class TelegramError(Exception):
    """Telegram Bot API error."""


def tg_call(token: str, method: str, payload: dict = None, timeout: int = 35) -> dict:
    url = f"{TG_API}/bot{token}/{method}"
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise TelegramError(f"Telegram HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise TelegramError(f"Telegram network error: {e}") from e
    if not res.get("ok"):
        raise TelegramError(res.get("description") or str(res)[:200])
    return res.get("result")


def tg_send_message(token: str, chat_id: str, text: str) -> list:
    """Convert Markdown to clean Telegram HTML and send (auto-chunking safely)."""
    txt = str(text or "(empty)").strip() or "(empty)"
    # Chunk before HTML conversion so every chunk has balanced HTML tags (limit < 3600 to allow tag overhead)
    max_raw = 3200
    chunks = []
    while len(txt) > max_raw:
        cut = txt.rfind("\n\n", 0, max_raw)
        if cut < max_raw // 2:
            cut = txt.rfind("\n", 0, max_raw)
        if cut < max_raw // 2:
            cut = max_raw
        chunks.append(txt[:cut])
        txt = txt[cut:].lstrip("\n")
    chunks.append(txt)

    sent = []
    for ch in chunks:
        html_body = md_to_telegram_html(ch)
        try:
            sent.append(tg_call(
                token,
                "sendMessage",
                {
                    "chat_id": str(chat_id),
                    "text": html_body[:4096],
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=20,
            ))
        except TelegramError:
            # Fallback to clean readable plain/WhatsApp-style text if Telegram rejects any HTML
            plain = md_to_whatsapp(ch)[:4096]
            sent.append(tg_call(token, "sendMessage", {"chat_id": str(chat_id), "text": plain}, timeout=20))
    return sent


def tg_download_file(token: str, file_id: str) -> bytes:
    info = tg_call(token, "getFile", {"file_id": file_id}, timeout=20)
    fpath = info.get("file_path")
    if not fpath:
        raise TelegramError("Telegram did not return file_path")
    url = f"{TG_API}/file/bot{token}/{fpath}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read(2 * 1024 * 1024 + 1)


def send_user_telegram(registry, user_id: str, text: str) -> bool:
    """Push a notification message to a user's configured Telegram bot & chat ID."""
    cfg = registry.get_user_channels(user_id, include_secret=True)
    if not (cfg["telegram_bot_token"] and cfg["telegram_chat_id"]):
        return False
    tg_send_message(cfg["telegram_bot_token"], cfg["telegram_chat_id"], text)
    return True


class _UserBotWorker:
    """Dedicated long-polling thread for a single user's Telegram bot."""

    def __init__(self, manager, user_id: str, token: str):
        self.mgr = manager
        self.user_id = user_id
        self.token = token
        self._stop = threading.Event()
        self._thread = None
        self.offset = 0
        self.bot_username = ""
        self.state = "starting"
        self.last_error = ""

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"tg-bot-{self.user_id}", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def status_dict(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive() and not self._stop.is_set()),
            "state": self.state,
            "bot_username": self.bot_username,
            "last_error": self.last_error,
        }

    def _run(self):
        try:
            me = tg_call(self.token, "getMe", timeout=15)
            self.bot_username = me.get("username", "")
            self.state = "polling"
            self.last_error = ""
        except Exception as e:
            self.state = "error"
            self.last_error = str(e)
            return

        while not self._stop.is_set():
            try:
                updates = tg_call(
                    self.token,
                    "getUpdates",
                    {"offset": self.offset, "timeout": 20, "allowed_updates": ["message"]},
                    timeout=30,
                )
                self.state = "polling"
                self.last_error = ""
                for upd in (updates or []):
                    self.offset = max(self.offset, int(upd.get("update_id", 0)) + 1)
                    msg = upd.get("message")
                    if msg:
                        self.mgr.handle_incoming(self.user_id, self.token, msg)
            except Exception as e:
                self.last_error = str(e)
                self.state = "retrying"
                self._stop.wait(5.0)
        self.state = "stopped"


class TelegramBotManager:
    """Manages per-user Telegram bots (one long-polling worker per user with an enabled bot)."""

    def __init__(self, orch, scheduler, op_lock: threading.Lock, log_tls: threading.local):
        self.orch = orch
        self.scheduler = scheduler
        self.op_lock = op_lock
        self.log_tls = log_tls
        self._lock = threading.Lock()
        self._workers: dict[str, _UserBotWorker] = {}

    def start_all(self):
        for cfg in self.orch.reg.list_enabled_telegram_users():
            self.sync_user(cfg["user_id"])

    def stop_all(self):
        with self._lock:
            for w in self._workers.values():
                w.stop()
            self._workers.clear()

    def sync_user(self, user_id: str):
        """Start, restart (if token changed), or stop a user's bot worker."""
        user = self.orch.reg.get_user(user_id)
        cfg = self.orch.reg.get_user_channels(user_id, include_secret=True)
        should_run = bool(
            user
            and user["status"] != "revoked"
            and cfg["telegram_enabled"]
            and cfg["telegram_bot_token"]
        )
        with self._lock:
            cur = self._workers.get(user_id)
            if not should_run:
                if cur:
                    cur.stop()
                    del self._workers[user_id]
                return
            if cur and cur.token == cfg["telegram_bot_token"] and cur.status_dict()["running"]:
                return
            if cur:
                cur.stop()
            worker = _UserBotWorker(self, user_id, cfg["telegram_bot_token"])
            self._workers[user_id] = worker
            worker.start()

    def user_status(self, user_id: str) -> dict:
        with self._lock:
            w = self._workers.get(user_id)
            return w.status_dict() if w else {"running": False, "state": "stopped", "bot_username": "", "last_error": ""}

    # --- Incoming Telegram Message & Command Router --------------------------

    def handle_incoming(self, user_id: str, token: str, msg: dict):
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        if not chat_id:
            return
        text = (msg.get("text") or msg.get("caption") or "").strip()
        doc = msg.get("document")

        user = self.orch.reg.get_user(user_id)
        if not user or user["status"] == "revoked":
            tg_send_message(token, chat_id, "Your agent-hive account is revoked.")
            return

        cfg = self.orch.reg.get_user_channels(user_id, include_secret=True)
        bound_chat = cfg["telegram_chat_id"]

        # Pairing check: require either matching configured chat_id OR `/link <CODE>`
        if text.startswith("/link"):
            code = text.partition(" ")[2].strip().upper()
            if code and code == cfg["telegram_link_code"].upper():
                self.orch.reg.save_user_channels(user_id, telegram_chat_id=chat_id, regenerate_link_code=True)
                tg_send_message(
                    token, chat_id,
                    f"Linked Telegram chat {chat_id} to agent-hive user '{user['username']}'.\n"
                    "Send /help to see everything you can do!"
                )
            else:
                tg_send_message(token, chat_id, "Invalid link code. Check the My Bots tab in agent-hive.")
            return

        if not bound_chat:
            tg_send_message(
                token, chat_id,
                "This bot is not linked to your chat yet.\n"
                "Open agent-hive → My Bots, copy your link code, and send:\n"
                "/link YOUR_CODE"
            )
            return

        if str(bound_chat) != chat_id:
            tg_send_message(token, chat_id, "Unauthorized Telegram chat for this bot.")
            return

        try:
            reply = self._dispatch(user, token, chat_id, text, doc)
        except Exception as e:
            reply = f"Error: {e}"
        if reply:
            tg_send_message(token, chat_id, reply)

    def _check_write_and_quota(self, user: dict) -> str:
        fresh = self.orch.reg.get_user(user["id"])
        if not fresh or fresh["status"] == "revoked":
            return "Your account is revoked."
        if fresh["status"] == "restricted":
            r = f" Reason: {fresh['restriction_reason']}" if fresh["restriction_reason"] else ""
            return f"Your account is restricted by an administrator.{r}"
        lim = fresh["token_limit"]
        if lim is not None and fresh["tokens_used"] >= lim:
            return f"Token limit reached ({fresh['tokens_used']:,} / {lim:,}). Ask an admin to raise or reset your quota."
        return ""

    def _record_usage(self, user_id: str, usage: dict):
        if usage.get("total_tokens", 0) > 0:
            self.orch.reg.add_user_tokens(
                user_id,
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("total_tokens", 0),
            )

    def _dispatch(self, user: dict, token: str, chat_id: str, text: str, doc: dict = None) -> str:
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower() if cmd.startswith("/") else ""
        rest = rest.strip()

        if cmd in ("/start", "/help"):
            return TG_HELP

        if cmd == "/status":
            fresh = self.orch.reg.get_user(user["id"])
            lim = "unlimited" if fresh["token_limit"] is None else f"{fresh['token_limit']:,}"
            oc = self.orch.cfg["orchestrator"]
            w = self.orch.cfg["workers"]
            om, oep = self.orch.resolve_model(oc["model"], oc["endpoint"])
            wm, wep = self.orch.resolve_model(w["model"], w["endpoint"])
            return (
                f"User: {fresh['username']} ({fresh['role']}, {fresh['status']})\n"
                f"Tokens: {fresh['tokens_used']:,} / {lim}\n"
                f"Orchestrator: {om} @ {oep}\n"
                f"Workers: {wm} @ {wep}\n"
                f"Agents: {len(self.orch.reg.list_agents())} | Tools: {len(self.orch.lib.tools)}"
            )

        if cmd == "/agents":
            agents = self.orch.reg.list_agents()
            if not agents:
                return "No worker agents yet. Send any task and the orchestrator will create one."
            return "\n".join(
                f"• {a.name} [{a.model}] — {a.description} (tools: {', '.join(a.tools)})"
                for a in agents
            )

        if cmd == "/tools":
            return self.orch.lib.describe()

        if cmd == "/files":
            return self.orch.lib.list_files()

        if cmd == "/read":
            if not rest:
                return "Usage: /read path/in/workspace.md"
            try:
                return self.orch.lib.read_file(rest)
            except Exception as e:
                return f"Could not read '{rest}': {e}"

        if cmd == "/tasks":
            rows = self.orch.reg.recent_tasks(10)
            if not rows:
                return "No worker tasks yet."
            return "\n".join(
                f"#{t['id']} {t['agent']} [{t['status']}]: {t['task'][:60]}" for t in rows
            )

        if cmd == "/reset":
            self.orch.reg.clear_chat_messages(user["id"])
            return "Your conversation history has been cleared."

        if cmd == "/schedules":
            items = self.orch.reg.list_schedules(
                user_id=None if user["role"] == "admin" else user["id"]
            )
            if not items:
                return "No scheduled tasks yet. Use:\n/schedule 60 | Hourly Check | Summarize workspace"
            lines = []
            for s in items:
                trig = f"every {s['interval_minutes']}m" if s["trigger_type"] == "interval" else f"daily {s['daily_time']}"
                state = "active" if s["enabled"] else "paused"
                lines.append(f"• {s['id']} — {s['name']} [{state}, {trig}] runs={s['run_count']} last={s['last_status']}")
            return "\n".join(lines)

        if cmd == "/schedule":
            err = self._check_write_and_quota(user)
            if err:
                return err
            parts = [p.strip() for p in rest.split("|", 2)]
            if len(parts) < 3:
                return (
                    "Usage: /schedule <minutes_or_HH:MM> | <name> | <prompt>\n"
                    f"Allowed minutes: {list(ALLOWED_INTERVAL_MINUTES)}\n"
                    "Example: /schedule 09:00 | Morning Digest | Summarize important unread emails"
                )
            when, sname, sprompt = parts
            try:
                if ":" in when:
                    trig, iv, dt_str, days, tz_off = validate_trigger("daily", daily_time=when)
                else:
                    trig, iv, dt_str, days, tz_off = validate_trigger("interval", interval_minutes=int(when))
            except Exception as e:
                return f"Invalid trigger: {e}"
            nxt = compute_next_run(trig, iv, dt_str, days, tz_off)
            sch = self.orch.reg.create_schedule(
                user_id=user["id"],
                owner_name=user["username"],
                name=sname,
                target_type="orchestrator",
                agent_name="",
                prompt=sprompt,
                trigger_type=trig,
                interval_minutes=iv,
                daily_time=dt_str,
                days_of_week=days,
                timezone_offset_minutes=tz_off,
                enabled=True,
                next_run_at=nxt,
                notify_telegram=True,
                notify_whatsapp=False,
            )
            return f"Created schedule {sch['id']} ('{sch['name']}'). Results will be delivered here in Telegram."

        if cmd in ("/pause_schedule", "/resume_schedule", "/del_schedule", "/run_schedule"):
            if not rest:
                return f"Usage: {cmd} <schedule_id>"
            sch = self.orch.reg.get_schedule(rest)
            if not sch:
                return f"No schedule '{rest}'."
            if user["role"] != "admin" and sch.get("user_id") != user["id"]:
                return "You can only manage your own schedules."
            if cmd == "/del_schedule":
                self.orch.reg.delete_schedule(rest)
                return f"Deleted schedule {rest}."
            if cmd == "/pause_schedule":
                self.orch.reg.update_schedule(rest, {"enabled": False, "next_run_at": None})
                return f"Paused schedule {rest}."
            if cmd == "/resume_schedule":
                nxt = compute_next_run(
                    sch["trigger_type"], sch["interval_minutes"], sch["daily_time"],
                    sch["days_of_week"], sch["timezone_offset_minutes"],
                )
                self.orch.reg.update_schedule(rest, {"enabled": True, "next_run_at": nxt})
                return f"Resumed schedule {rest}."
            # /run_schedule
            err = self._check_write_and_quota(user)
            if err:
                return err
            tg_send_message(token, chat_id, f"Running schedule '{sch['name']}'…")
            run = self.scheduler.execute_schedule(rest, scheduled=False)
            return f"[{sch['name']} — {run['status']} | {run['tokens_used']} tok]\n\n{run['result']}"

        if cmd == "/create_tool":
            err = self._check_write_and_quota(user)
            if err:
                return err
            parts = [p.strip() for p in rest.split("|", 2)]
            if len(parts) < 3:
                return "Usage: /create_tool <snake_name> | <short desc> | <detailed requirements>"
            tname, tdesc, treqs = parts
            tg_send_message(token, chat_id, f"Synthesizing & testing tool '{tname}' with deepseek-chat…")
            start_usage_tracking()
            try:
                with self.op_lock:
                    saved = self.orch.tool_creator.create_tool(tname, tdesc, treqs, created_by=user["username"])
            finally:
                self._record_usage(user["id"], finish_usage_tracking())
            return f"Tool '{saved['name']}' created & saved using {saved['created_by_model']}!"

        if cmd == "/run":
            err = self._check_write_and_quota(user)
            if err:
                return err
            ag_name, _, task_txt = rest.partition(" ")
            if not ag_name or not task_txt.strip():
                return "Usage: /run <agent_name> <task>"
            tg_send_message(token, chat_id, f"Running worker '{ag_name}'…")
            start_usage_tracking()
            try:
                with self.op_lock:
                    out = self.orch.run_worker(ag_name.strip(), task_txt.strip())
            finally:
                self._record_usage(user["id"], finish_usage_tracking())
            return out

        if cmd == "/emails":
            err = self._check_write_and_quota(user)
            if err:
                return err
            tg_send_message(token, chat_id, "Scanning unread emails from last 100 messages…")
            start_usage_tracking()
            try:
                with self.op_lock:
                    res = self.orch.summarize_important_emails(scan_last=100)
            except EmailConfigError as e:
                return str(e)
            finally:
                self._record_usage(user["id"], finish_usage_tracking())
            return res["summary_markdown"]

        if cmd == "/draft_emails":
            err = self._check_write_and_quota(user)
            if err:
                return err
            tg_send_message(token, chat_id, "Preparing reply drafts for important unread emails…")
            start_usage_tracking()
            try:
                with self.op_lock:
                    res = self.orch.batch_prepare_email_drafts(scan_last=100, instructions=rest)
            except EmailConfigError as e:
                return str(e)
            finally:
                self._record_usage(user["id"], finish_usage_tracking())
            lines = [res["message"]]
            for d in res["drafts"]:
                lines.append(f"• {d['id']} → {d['sender_name']} ({d['draft_subject']}): {d['draft_reply'][:120]}")
            return "\n".join(lines)

        if cmd == "/drafts":
            drafts = self.orch.reg.list_inbox_drafts(status="pending", limit=20)
            if not drafts:
                return "No pending Email/WhatsApp reply drafts."
            return "\n\n".join(
                f"[{d['channel'].upper()}] {d['id']} → {d['sender_name']} ({d['subject']})\n"
                f"Draft: {d['draft_reply'][:260]}\n"
                f"Send with: /send_draft {d['id']}"
                for d in drafts
            )

        if cmd == "/send_draft":
            err = self._check_write_and_quota(user)
            if err:
                return err
            if not rest:
                return "Usage: /send_draft <draft_id>"
            drf = self.orch.reg.get_inbox_draft(rest)
            if not drf:
                return f"No draft '{rest}'."
            try:
                if drf["channel"] == "email":
                    send_email_reply(
                        to_addr=drf["sender_addr"],
                        subject=drf["draft_subject"] or drf["subject"],
                        body=drf["draft_reply"],
                        in_reply_to=drf["in_reply_to"],
                        registry=self.orch.reg,
                    )
                elif drf["channel"] == "whatsapp":
                    send_whatsapp_message(drf["sender_addr"], drf["draft_reply"], registry=self.orch.reg)
                    self.orch.reg.mark_whatsapp_read(chat_id=drf["sender_addr"])
                self.orch.reg.update_inbox_draft(rest, {"status": "sent", "error": "", "sent_at": time.time()})
                return f"Sent {drf['channel']} reply to {drf['sender_name']}!"
            except (EmailConfigError, WhatsAppError, Exception) as e:
                self.orch.reg.update_inbox_draft(rest, {"error": str(e)})
                return f"Send failed: {e}"

        if cmd:
            return f"Unknown command '{cmd}'. Send /help for available commands."

        # --- Plain text (and/or .md/.txt/.csv document) -> Orchestrator Chat ---
        err = self._check_write_and_quota(user)
        if err:
            return err

        attached_files = []
        prompt_text = text
        if doc:
            fname = Path(doc.get("file_name") or "file.txt").name
            ext = Path(fname).suffix.lower()
            if ext not in ALLOWED_DOC_EXTS:
                return f"Unsupported file '{fname}'. Only .md, .txt, and .csv files are allowed."
            raw_bytes = tg_download_file(token, doc["file_id"])
            if len(raw_bytes) > 2 * 1024 * 1024 or b"\x00" in raw_bytes:
                return f"File '{fname}' is too large (>2 MB) or binary."
            try:
                fcontent = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return f"File '{fname}' is not valid UTF-8 text."
            safe_owner = SAFE_NAME_RE.sub("_", user["username"]).strip("_") or "tg"
            safe_file = SAFE_NAME_RE.sub("_", fname).strip("_") or f"file{ext}"
            rel = f"uploads/{safe_owner}/{time.strftime('%Y%m%d_%H%M%S')}_{safe_file}"
            dest = self.orch.lib._path(rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(fcontent, encoding="utf-8")
            attached_files.append({"filename": fname, "workspace_path": rel, "size_bytes": len(raw_bytes), "ext": ext})
            preview = fcontent[:4000]
            prompt_text = (
                f"{text or f'Analyze attached file {fname}'}\n\n"
                f"--- Attached File: `{fname}` (workspace: `{rel}`) ---\n{preview}\n--- End ---"
            )

        if not prompt_text:
            return "Send a message or attach a .md, .txt, or .csv file."

        self.log_tls.logs = []
        start_usage_tracking()
        try:
            with self.op_lock:
                pairs = self.orch.reg.get_user_chat_pairs(
                    user["id"], max_turns=self.orch.cfg["runtime"].get("history_turns", 6)
                )
                reply = self.orch.handle(prompt_text, history_override=pairs)
        except LLMError as e:
            return f"Model error: {e}"
        finally:
            logs = list(self.log_tls.logs or [])
            self.log_tls.logs = None
            usage = finish_usage_tracking()
            self._record_usage(user["id"], usage)

        self.orch.reg.add_chat_message(user["id"], "user", text or f"Uploaded {attached_files[0]['filename']}", files=attached_files)
        self.orch.reg.add_chat_message(
            user["id"], "assistant", reply, logs=logs, tokens_used=usage.get("total_tokens", 0)
        )
        return reply
