"""Persistent background task scheduler with interval (5m–1h, 4h increments) & daily time triggers."""
import datetime
import re
import threading
import time

from .channels.whatsapp_client import (
    get_session_status as wa_get_status,
    send_whatsapp_report,
)
from .llm import LLMError, finish_usage_tracking, start_usage_tracking

# 5 min to 1 hour, then 4-hour increments up to 24 hours
ALLOWED_INTERVAL_MINUTES = (5, 10, 15, 20, 30, 45, 60, 240, 480, 720, 960, 1200, 1440)
DAILY_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def validate_trigger(trigger_type: str, interval_minutes=None, daily_time=None,
                     days_of_week=None, timezone_offset_minutes: int = 0):
    """Validate trigger parameters and return normalized values."""
    if trigger_type not in ("interval", "daily"):
        raise ValueError("trigger_type must be 'interval' or 'daily'")
    tz_off = int(timezone_offset_minutes or 0)
    if tz_off < -720 or tz_off > 840:
        raise ValueError("timezone_offset_minutes must be between -720 and 840")

    if trigger_type == "interval":
        if interval_minutes is None:
            raise ValueError("interval_minutes is required for interval trigger")
        iv = int(interval_minutes)
        if iv not in ALLOWED_INTERVAL_MINUTES:
            raise ValueError(
                f"interval_minutes must be one of {list(ALLOWED_INTERVAL_MINUTES)} "
                "(5m-60m or 4h increments)"
            )
        return "interval", iv, None, [0, 1, 2, 3, 4, 5, 6], tz_off

    # daily specific time of day
    dt_str = (daily_time or "").strip()
    if not DAILY_TIME_RE.match(dt_str):
        raise ValueError("daily_time must be in 24-hour 'HH:MM' format (e.g. '09:00', '18:30')")
    if days_of_week is None or len(days_of_week) == 0:
        days = [0, 1, 2, 3, 4, 5, 6]
    else:
        days = sorted({int(d) for d in days_of_week})
        if any(d < 0 or d > 6 for d in days):
            raise ValueError("days_of_week values must be integers 0 (Sun) through 6 (Sat)")
    return "daily", None, dt_str, days, tz_off


def compute_next_run(
    trigger_type: str,
    interval_minutes=None,
    daily_time=None,
    days_of_week=None,
    timezone_offset_minutes: int = 0,
    now_ts: float = None,
) -> float:
    """Compute the next UTC Unix timestamp when a schedule should fire."""
    now = time.time() if now_ts is None else float(now_ts)
    if trigger_type == "interval":
        return now + int(interval_minutes) * 60.0

    # Daily at specific local HH:MM using user's UTC offset (minutes east of UTC)
    # days_of_week uses JS convention: 0=Sun, 1=Mon, ..., 6=Sat
    hour, minute = (int(x) for x in daily_time.split(":"))
    tz = datetime.timezone(datetime.timedelta(minutes=int(timezone_offset_minutes or 0)))
    local_now = datetime.datetime.fromtimestamp(now, tz=tz)
    allowed_days = set(days_of_week if days_of_week else [0, 1, 2, 3, 4, 5, 6])

    for day_offset in range(0, 8):
        cand_date = local_now.date() + datetime.timedelta(days=day_offset)
        # Python weekday(): Mon=0..Sun=6 -> JS day: Sun=0, Mon=1..Sat=6
        js_day = (cand_date.weekday() + 1) % 7
        if js_day not in allowed_days:
            continue
        cand_dt = datetime.datetime(
            cand_date.year, cand_date.month, cand_date.day, hour, minute, 0, tzinfo=tz
        )
        cand_ts = cand_dt.timestamp()
        if cand_ts > now + 1.0:
            return cand_ts
    return now + 86400.0


class TaskScheduler:
    """Daemon thread that polls SQLite for due scheduled tasks and executes them safely."""

    def __init__(self, orch, op_lock: threading.Lock, log_tls: threading.local, poll_seconds: float = 5.0):
        self.orch = orch
        self.op_lock = op_lock
        self.log_tls = log_tls
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        # Recover any schedules left in 'running' state after an abrupt container restart
        now = time.time()
        for sch in self.orch.reg.list_schedules():
            if sch["last_status"] == "running":
                nxt = compute_next_run(
                    sch["trigger_type"], sch["interval_minutes"], sch["daily_time"],
                    sch["days_of_week"], sch["timezone_offset_minutes"], now_ts=now,
                ) if sch["enabled"] else None
                self.orch.reg.update_schedule(sch["id"], {"last_status": "idle", "next_run_at": nxt})
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="agent-hive-scheduler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _loop(self):
        while not self._stop.is_set():
            try:
                now = time.time()
                due = self.orch.reg.list_due_schedules(now)
                for sch in due:
                    if self._stop.is_set():
                        break
                    self.execute_schedule(sch["id"], scheduled=True)
            except Exception as e:
                print(f"[scheduler] poll error: {e}", flush=True)
            self._stop.wait(self.poll_seconds)

    def execute_schedule(self, schedule_id: str, scheduled: bool = False) -> dict:
        """Execute a scheduled task now, respecting user status & token quotas."""
        sch = self.orch.reg.get_schedule(schedule_id)
        if not sch:
            raise ValueError(f"no schedule '{schedule_id}'")

        started = time.time()
        next_ts = (
            compute_next_run(
                sch["trigger_type"], sch["interval_minutes"], sch["daily_time"],
                sch["days_of_week"], sch["timezone_offset_minutes"], now_ts=started,
            )
            if sch["enabled"]
            else None
        )

        # Enforce owner account status & token quota before burning tokens
        uid = sch.get("user_id")
        if uid:
            owner = self.orch.reg.get_user(uid)
            skip_reason = None
            if not owner:
                skip_reason = "Owner user no longer exists."
            elif owner["status"] == "revoked":
                skip_reason = "Owner account is revoked."
            elif owner["status"] == "restricted":
                skip_reason = "Owner account is restricted by an administrator."
            elif owner["token_limit"] is not None and owner["tokens_used"] >= owner["token_limit"]:
                skip_reason = (
                    f"Owner token quota reached ({owner['tokens_used']:,} / {owner['token_limit']:,})."
                )
            if skip_reason:
                finished = time.time()
                run = self.orch.reg.record_schedule_run(
                    schedule_id, "skipped", skip_reason, [], 0, started, finished
                )
                self.orch.reg.update_schedule(schedule_id, {
                    "last_run_at": finished,
                    "last_status": "skipped",
                    "last_result": "",
                    "last_error": skip_reason,
                    "next_run_at": next_ts,
                })
                return run

        self.orch.reg.update_schedule(schedule_id, {"last_status": "running", "last_error": ""})

        self.log_tls.logs = []
        start_usage_tracking()
        status = "done"
        result = ""
        err_msg = ""
        try:
            with self.op_lock:
                if sch["target_type"] == "worker" and sch["agent_name"]:
                    result = self.orch.run_worker(sch["agent_name"], sch["prompt"])
                else:
                    result = self.orch.handle(sch["prompt"], history_override=[])
        except LLMError as e:
            status = "failed"
            err_msg = str(e)
            result = f"ERROR: {e}"
        except Exception as e:
            status = "failed"
            err_msg = f"{type(e).__name__}: {e}"
            result = f"ERROR: {err_msg}"
        finally:
            logs = list(self.log_tls.logs or [])
            self.log_tls.logs = None

        usage = finish_usage_tracking()
        total_tokens = usage.get("total_tokens", 0)
        if uid and total_tokens > 0:
            self.orch.reg.add_user_tokens(
                uid,
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                total_tokens,
            )

        finished = time.time()
        run = self.orch.reg.record_schedule_run(
            schedule_id, status, result, logs, total_tokens, started, finished
        )
        # Re-read latest schedule in case enabled/trigger changed during execution
        latest = self.orch.reg.get_schedule(schedule_id)
        if latest:
            recomp_next = (
                compute_next_run(
                    latest["trigger_type"], latest["interval_minutes"], latest["daily_time"],
                    latest["days_of_week"], latest["timezone_offset_minutes"], now_ts=finished,
                )
                if latest["enabled"]
                else None
            )
            self.orch.reg.update_schedule(schedule_id, {
                "last_run_at": finished,
                "last_status": status,
                "last_result": result,
                "last_error": err_msg,
                "run_count": latest["run_count"] + 1,
                "next_run_at": recomp_next if scheduled or latest["next_run_at"] is None else latest["next_run_at"],
            })
            self._deliver_notifications(latest, status, result, total_tokens)
        return run

    def _deliver_notifications(self, sch: dict, status: str, result: str, tokens_used: int):
        """Deliver completed scheduled task output to the owner's Telegram bot and/or WhatsApp."""
        uid = sch.get("user_id")
        if not uid:
            return
        want_tg = bool(sch.get("notify_telegram"))
        want_wa = bool(sch.get("notify_whatsapp"))
        if not (want_tg or want_wa):
            return

        header = f"[Scheduled Task: {sch['name']} — {status.upper()} | {tokens_used:,} tok]"
        body = f"{header}\n\n{result}"

        if want_tg:
            try:
                from .channels.telegram_bot import send_user_telegram
                send_user_telegram(self.orch.reg, uid, body)
            except Exception as e:
                print(f"[scheduler] Telegram notify failed for {sch['id']}: {e}", flush=True)

        if want_wa:
            try:
                ch = self.orch.reg.get_user_channels(uid, include_secret=False)
                target = ch.get("whatsapp_target", "").strip()
                if not target:
                    st = wa_get_status(self.orch.reg)
                    me = st.get("me") or {}
                    target = str(me.get("id") or "").strip()
                if target:
                    target_label = (
                        f"worker:{sch['agent_name']}"
                        if sch.get("target_type") == "worker" and sch.get("agent_name")
                        else "orchestrator"
                    )
                    send_whatsapp_report(
                        target,
                        title=sch["name"],
                        status=status,
                        target_label=target_label,
                        tokens_used=tokens_used,
                        body_markdown=result,
                        registry=self.orch.reg,
                    )
            except Exception as e:
                print(f"[scheduler] WhatsApp notify failed for {sch['id']}: {e}", flush=True)
