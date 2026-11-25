"""Stdlib IMAP/SMTP email client: scan last N messages for unread, filter noise, send threaded replies."""
import email
import imaplib
import os
import re
import smtplib
from email.header import decode_header
from email.message import EmailMessage
from email.utils import parseaddr
from html.parser import HTMLParser


class EmailConfigError(Exception):
    """Email IMAP/SMTP settings are missing or incomplete."""


class _HtmlStrip(HTMLParser):
    SKIP = {"script", "style", "head"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())


NOISE_SENDER_RE = re.compile(
    r"(no-?reply|donotreply|mailer-daemon|notifications?@|newsletter|marketing|unsubscribe|digest@|promo)",
    re.I,
)


def _decode_hdr(val: str) -> str:
    if not val:
        return ""
    out = []
    for chunk, enc in decode_header(val):
        if isinstance(chunk, bytes):
            out.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(str(chunk))
    return "".join(out).strip()


def _extract_body(msg) -> str:
    plain, html_part = [], []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if ctype == "text/plain":
                plain.append(text)
            elif ctype == "text/html":
                html_part.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_part.append(text)
            else:
                plain.append(text)

    if plain:
        return "\n".join(plain).strip()
    if html_part:
        p = _HtmlStrip()
        p.feed("\n".join(html_part))
        return "\n".join(p.parts).strip()
    return ""


def get_email_config(registry=None) -> dict:
    """Resolve IMAP/SMTP config from persisted SQLite settings first, then environment variables."""
    saved = registry.get_setting("email_config", {}) if registry else {}
    if not isinstance(saved, dict):
        saved = {}
    return {
        "imap_host": (saved.get("imap_host") or os.environ.get("EMAIL_IMAP_HOST", "")).strip(),
        "imap_port": int(saved.get("imap_port") or os.environ.get("EMAIL_IMAP_PORT", "993")),
        "smtp_host": (saved.get("smtp_host") or os.environ.get("EMAIL_SMTP_HOST", "")).strip(),
        "smtp_port": int(saved.get("smtp_port") or os.environ.get("EMAIL_SMTP_PORT", "587")),
        "user": (saved.get("user") or os.environ.get("EMAIL_USER", "")).strip(),
        "password": (saved.get("password") or os.environ.get("EMAIL_PASSWORD", "")).strip(),
        "folder": (saved.get("folder") or os.environ.get("EMAIL_FOLDER", "INBOX")).strip() or "INBOX",
    }


def is_email_configured(registry=None) -> bool:
    c = get_email_config(registry)
    return bool(c["imap_host"] and c["user"] and c["password"])


def fetch_unread_from_last(registry=None, scan_last: int = 100, body_chars: int = 1800) -> list[dict]:
    """Scan the last `scan_last` emails in INBOX and return all UNREAD messages (newest first)."""
    cfg = get_email_config(registry)
    if not (cfg["imap_host"] and cfg["user"] and cfg["password"]):
        raise EmailConfigError(
            "Email is not configured. Set EMAIL_IMAP_HOST, EMAIL_SMTP_HOST, EMAIL_USER, "
            "and EMAIL_PASSWORD in .env or configure them in the Inbox tab."
        )
    scan_last = max(1, min(int(scan_last), 500))

    conn = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], timeout=25)
    try:
        conn.login(cfg["user"], cfg["password"])
        status, data = conn.select(cfg["folder"], readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not select folder '{cfg['folder']}'")

        total = int(data[0] or 0)
        if total == 0:
            return []

        start_seq = max(1, total - scan_last + 1)
        seq_range = f"{start_seq}:{total}"
        status, search_data = conn.search(None, f"{seq_range} UNSEEN")
        if status != "OK" or not search_data or not search_data[0]:
            return []

        seq_nums = search_data[0].split()
        messages = []
        for seq in reversed(seq_nums):  # newest first
            st, fetched = conn.fetch(seq, "(UID BODY.PEEK[])")
            if st != "OK" or not fetched:
                continue
            uid = ""
            raw_bytes = None
            for item in fetched:
                if isinstance(item, tuple):
                    meta_hdr = item[0].decode(errors="replace")
                    m = re.search(r"UID\s+(\d+)", meta_hdr)
                    if m:
                        uid = m.group(1)
                    raw_bytes = item[1]
            if not raw_bytes:
                continue
            msg = email.message_from_bytes(raw_bytes)
            from_raw = _decode_hdr(msg.get("From", ""))
            sender_name, sender_addr = parseaddr(from_raw)
            subject = _decode_hdr(msg.get("Subject", "(no subject)"))
            date_str = _decode_hdr(msg.get("Date", ""))
            message_id = (msg.get("Message-ID") or "").strip()
            list_unsub = bool(msg.get("List-Unsubscribe") or msg.get("List-Id"))
            auto_sub = str(msg.get("Auto-Submitted") or "").lower() not in ("", "no")
            is_automated = bool(list_unsub or auto_sub or NOISE_SENDER_RE.search(from_raw))
            body = _extract_body(msg)
            messages.append({
                "uid": uid or seq.decode(errors="replace"),
                "message_id": message_id,
                "from": from_raw,
                "sender_name": sender_name or sender_addr,
                "sender_email": sender_addr,
                "subject": subject,
                "date": date_str,
                "is_automated": is_automated,
                "body": body[:body_chars],
                "body_truncated": len(body) > body_chars,
            })
        return messages
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def send_email_reply(
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: str = "",
    registry=None,
) -> dict:
    """Send an email or threaded reply over SMTP (STARTTLS on 587 or SSL on 465)."""
    cfg = get_email_config(registry)
    if not (cfg["smtp_host"] and cfg["user"] and cfg["password"]):
        raise EmailConfigError(
            "SMTP is not configured. Set EMAIL_SMTP_HOST, EMAIL_USER, and EMAIL_PASSWORD."
        )
    msg = EmailMessage()
    msg["From"] = cfg["user"]
    msg["To"] = to_addr
    subj = subject.strip() or "(no subject)"
    if in_reply_to and not subj.lower().startswith("re:"):
        subj = f"Re: {subj}"
    msg["Subject"] = subj
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)

    if cfg["smtp_port"] == 465:
        with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=25) as srv:
            srv.login(cfg["user"], cfg["password"])
            srv.send_message(msg)
    else:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=25) as srv:
            srv.ehlo()
            srv.starttls()
            srv.ehlo()
            srv.login(cfg["user"], cfg["password"])
            srv.send_message(msg)

    return {"sent": True, "to": to_addr, "subject": subj}
