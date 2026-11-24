"""SQLite store for agents, tasks, API keys, users, sessions, chat history, and settings."""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

VALID_SCOPES = ("read", "write", "admin")
VALID_ROLES = ("user", "admin")
VALID_USER_STATUSES = ("active", "restricted", "revoked")
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
PBKDF2_ITERS = 260_000
SESSION_TTL_SECONDS = 30 * 86400  # 30 days

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    tools TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    scopes TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL,
    last_used_at REAL,
    request_count INTEGER NOT NULL DEFAULT 0,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    status TEXT NOT NULL DEFAULT 'active',
    must_reset_credentials INTEGER NOT NULL DEFAULT 0,
    token_limit INTEGER,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    request_count INTEGER NOT NULL DEFAULT 0,
    restriction_reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    last_login_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    files_json TEXT NOT NULL DEFAULT '[]',
    logs_json TEXT NOT NULL DEFAULT '[]',
    tokens_used INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class AgentSpec:
    name: str
    description: str
    system_prompt: str
    tools: list = field(default_factory=list)
    endpoint: str = ""
    model: str = ""


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERS)
    return f"pbkdf2_sha256${PBKDF2_ITERS}${salt}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters_s, salt, digest_hex = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iters_s))
        return hmac.compare_digest(dk.hex(), digest_hex)
    except Exception:
        return False


def _normalize_scopes(scopes) -> list[str]:
    if not scopes:
        return ["read", "write"]
    cleaned = [s.strip().lower() for s in scopes if str(s).strip()]
    bad = [s for s in cleaned if s not in VALID_SCOPES]
    if bad:
        raise ValueError(f"invalid scopes {bad}; allowed: {list(VALID_SCOPES)}")
    return list(dict.fromkeys(cleaned))


class Registry:
    def __init__(self, path):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(p), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)

    # --- settings (persisted across container restarts) ----------------------
    def get_setting(self, key: str, default=None):
        with self._lock:
            r = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if not r:
                return default
            try:
                return json.loads(r["value"])
            except json.JSONDecodeError:
                return r["value"]

    def set_setting(self, key: str, value):
        with self._lock:
            self.db.execute(
                "INSERT INTO settings (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            self.db.commit()

    def get_all_settings(self) -> dict:
        with self._lock:
            rows = self.db.execute("SELECT key, value FROM settings")
            out = {}
            for r in rows:
                try:
                    out[r["key"]] = json.loads(r["value"])
                except json.JSONDecodeError:
                    out[r["key"]] = r["value"]
            return out

    # --- users & auth --------------------------------------------------------
    @staticmethod
    def _user_row(r) -> dict:
        return {
            "id": r["id"],
            "username": r["username"],
            "role": r["role"],
            "status": r["status"],
            "must_reset_credentials": bool(r["must_reset_credentials"]),
            "token_limit": r["token_limit"],
            "tokens_used": r["tokens_used"],
            "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"],
            "request_count": r["request_count"],
            "restriction_reason": r["restriction_reason"] or "",
            "created_at": r["created_at"],
            "last_login_at": r["last_login_at"],
        }

    def ensure_default_admin(self):
        """Ensure at least one admin exists. Returns (username, password) ONLY if newly created."""
        with self._lock:
            r = self.db.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
            if r:
                return None
            username = f"admin_{secrets.token_hex(3)}"
            password = secrets.token_urlsafe(12)
            uid = f"usr_{secrets.token_hex(6)}"
            self.db.execute(
                "INSERT INTO users (id, username, password_hash, role, status, must_reset_credentials, "
                "token_limit, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (uid, username, _hash_password(password), "admin", "active", 1, None, time.time()),
            )
            self.db.commit()
            return username, password

    def create_user(self, username: str, password: str, role: str = "user", token_limit=None) -> dict:
        uname = username.strip()
        if not USERNAME_RE.match(uname):
            raise ValueError("username must be 3-32 chars (letters, numbers, _, ., -)")
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        if role not in VALID_ROLES:
            raise ValueError(f"role must be one of {VALID_ROLES}")
        if token_limit is not None:
            token_limit = int(token_limit)
            if token_limit < 0:
                raise ValueError("token_limit must be >= 0")
        uid = f"usr_{secrets.token_hex(6)}"
        with self._lock:
            exists = self.db.execute("SELECT 1 FROM users WHERE username=?", (uname,)).fetchone()
            if exists:
                raise ValueError(f"username '{uname}' is already taken")
            self.db.execute(
                "INSERT INTO users (id, username, password_hash, role, status, must_reset_credentials, "
                "token_limit, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (uid, uname, _hash_password(password), role, "active", 0, token_limit, time.time()),
            )
            self.db.commit()
            r = self.db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            return self._user_row(r)

    def authenticate_user(self, username: str, password: str):
        uname = username.strip()
        with self._lock:
            r = self.db.execute("SELECT * FROM users WHERE username=?", (uname,)).fetchone()
            if not r or not _verify_password(password, r["password_hash"]):
                return None
            return self._user_row(r)

    def mark_user_login(self, user_id: str):
        with self._lock:
            self.db.execute("UPDATE users SET last_login_at=? WHERE id=?", (time.time(), user_id))
            self.db.commit()

    def get_user(self, user_id: str):
        with self._lock:
            r = self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return self._user_row(r) if r else None

    def get_user_by_username(self, username: str):
        with self._lock:
            r = self.db.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
            return self._user_row(r) if r else None

    def list_users(self) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM users ORDER BY role DESC, created_at ASC")
            return [self._user_row(r) for r in rows]

    def reset_user_credentials(self, user_id: str, new_username: str, new_password: str, require_different_username=False):
        uname = new_username.strip()
        if not USERNAME_RE.match(uname):
            raise ValueError("username must be 3-32 chars (letters, numbers, _, ., -)")
        if len(new_password) < 8:
            raise ValueError("password must be at least 8 characters")
        with self._lock:
            cur = self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not cur:
                raise ValueError("user not found")
            if require_different_username and uname.lower() == cur["username"].lower():
                raise ValueError("new username must be different from the generated default username")
            clash = self.db.execute(
                "SELECT id FROM users WHERE username=? AND id!=?", (uname, user_id)
            ).fetchone()
            if clash:
                raise ValueError(f"username '{uname}' is already taken")
            self.db.execute(
                "UPDATE users SET username=?, password_hash=?, must_reset_credentials=0 WHERE id=?",
                (uname, _hash_password(new_password), user_id),
            )
            self.db.commit()
            r = self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return self._user_row(r)

    def update_user_admin(
        self,
        user_id: str,
        status=None,
        role=None,
        token_limit="UNSET",
        reset_tokens: bool = False,
        restriction_reason=None,
    ):
        with self._lock:
            cur = self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not cur:
                return None
            new_status = cur["status"] if status is None else status
            if new_status not in VALID_USER_STATUSES:
                raise ValueError(f"invalid status '{new_status}'")
            new_role = cur["role"] if role is None else role
            if new_role not in VALID_ROLES:
                raise ValueError(f"invalid role '{new_role}'")
            new_limit = cur["token_limit"] if token_limit == "UNSET" else token_limit
            if new_limit is not None:
                new_limit = int(new_limit)
                if new_limit < 0:
                    raise ValueError("token_limit must be >= 0")
            new_reason = cur["restriction_reason"] if restriction_reason is None else str(restriction_reason)[:240]
            if new_status == "active" and restriction_reason is None:
                new_reason = ""

            self.db.execute(
                "UPDATE users SET status=?, role=?, token_limit=?, restriction_reason=? WHERE id=?",
                (new_status, new_role, new_limit, new_reason, user_id),
            )
            if reset_tokens:
                self.db.execute(
                    "UPDATE users SET tokens_used=0, prompt_tokens=0, completion_tokens=0 WHERE id=?",
                    (user_id,),
                )
            if new_status == "revoked":
                self.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self.db.commit()
            r = self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return self._user_row(r)

    def add_user_tokens(self, user_id: str, prompt_tokens: int, completion_tokens: int, total_tokens: int):
        with self._lock:
            self.db.execute(
                "UPDATE users SET tokens_used=tokens_used+?, prompt_tokens=prompt_tokens+?, "
                "completion_tokens=completion_tokens+?, request_count=request_count+1 WHERE id=?",
                (int(total_tokens), int(prompt_tokens), int(completion_tokens), user_id),
            )
            self.db.commit()

    def delete_user(self, user_id: str) -> bool:
        with self._lock:
            n = self.db.execute("DELETE FROM users WHERE id=?", (user_id,)).rowcount
            self.db.commit()
            return n > 0

    # --- sessions ------------------------------------------------------------
    def create_session(self, user_id: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
        raw = f"hive_sess_{secrets.token_urlsafe(32)}"
        now = time.time()
        with self._lock:
            self.db.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                (_hash_token(raw), user_id, now, now + ttl_seconds),
            )
            self.db.commit()
        return raw

    def get_session_user(self, raw_token: str):
        if not raw_token:
            return None
        now = time.time()
        h = _hash_token(raw_token)
        with self._lock:
            r = self.db.execute(
                "SELECT u.* FROM sessions s JOIN users u ON s.user_id=u.id "
                "WHERE s.token_hash=? AND s.expires_at > ?",
                (h, now),
            ).fetchone()
            return self._user_row(r) if r else None

    def delete_session(self, raw_token: str):
        if not raw_token:
            return
        with self._lock:
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(raw_token),))
            self.db.commit()

    def delete_user_sessions(self, user_id: str):
        with self._lock:
            self.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self.db.commit()

    # --- persistent per-user chat history ------------------------------------
    def add_chat_message(self, user_id: str, role: str, content: str, files=None, logs=None, tokens_used=0) -> dict:
        now = time.time()
        files_json = json.dumps(files or [])
        logs_json = json.dumps(logs or [])
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO chat_messages (user_id, role, content, files_json, logs_json, tokens_used, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (user_id, role, content, files_json, logs_json, int(tokens_used), now),
            )
            self.db.commit()
            return {
                "id": cur.lastrowid,
                "user_id": user_id,
                "role": role,
                "content": content,
                "files": files or [],
                "logs": logs or [],
                "tokens_used": int(tokens_used),
                "created_at": now,
            }

    def list_chat_messages(self, user_id: str, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM (SELECT * FROM chat_messages WHERE user_id=? ORDER BY id DESC LIMIT ?) "
                "ORDER BY id ASC",
                (user_id, limit),
            )
            return [
                {
                    "id": r["id"],
                    "user_id": r["user_id"],
                    "role": r["role"],
                    "content": r["content"],
                    "files": json.loads(r["files_json"]),
                    "logs": json.loads(r["logs_json"]),
                    "tokens_used": r["tokens_used"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]

    def get_user_chat_pairs(self, user_id: str, max_turns: int = 6) -> list[tuple[str, str]]:
        msgs = self.list_chat_messages(user_id, limit=max_turns * 2 + 4)
        pairs = []
        pending_user = None
        for m in msgs:
            if m["role"] == "user":
                pending_user = m["content"]
            elif m["role"] == "assistant" and pending_user is not None:
                pairs.append((pending_user, m["content"]))
                pending_user = None
        return pairs[-max_turns:]

    def clear_chat_messages(self, user_id: str):
        with self._lock:
            self.db.execute("DELETE FROM chat_messages WHERE user_id=?", (user_id,))
            self.db.commit()

    # --- agents --------------------------------------------------------------
    def save_agent(self, a: AgentSpec):
        with self._lock:
            self.db.execute(
                "INSERT INTO agents VALUES (?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "description=excluded.description, system_prompt=excluded.system_prompt, "
                "tools=excluded.tools, endpoint=excluded.endpoint, model=excluded.model",
                (a.name, a.description, a.system_prompt, json.dumps(a.tools), a.endpoint, a.model,
                 time.time()))
            self.db.commit()

    def get_agent(self, name):
        with self._lock:
            r = self.db.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
            return self._agent(r) if r else None

    def list_agents(self):
        with self._lock:
            return [self._agent(r) for r in self.db.execute("SELECT * FROM agents ORDER BY name")]

    def delete_agent(self, name):
        with self._lock:
            n = self.db.execute("DELETE FROM agents WHERE name=?", (name,)).rowcount
            self.db.commit()
            return n > 0

    @staticmethod
    def _agent(r):
        return AgentSpec(r["name"], r["description"], r["system_prompt"],
                         json.loads(r["tools"]), r["endpoint"], r["model"])

    # --- tasks ---------------------------------------------------------------
    def start_task(self, agent, task):
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO tasks (agent, task, status, created_at) VALUES (?,?,?,?)",
                (agent, task, "running", time.time()))
            self.db.commit()
            return cur.lastrowid

    def finish_task(self, task_id, status, result):
        with self._lock:
            self.db.execute("UPDATE tasks SET status=?, result=?, finished_at=? WHERE id=?",
                            (status, result, time.time(), task_id))
            self.db.commit()

    def get_task(self, task_id):
        with self._lock:
            r = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(r) if r else None

    def recent_tasks(self, limit=10):
        with self._lock:
            return [dict(r) for r in self.db.execute(
                "SELECT id, agent, task, status, result FROM tasks ORDER BY id DESC LIMIT ?",
                (limit,))]

    def list_tasks(self, limit=20, agent=None, status=None):
        clauses, params = [], []
        if agent:
            clauses.append("agent=?")
            params.append(agent)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self.db.execute(
                f"SELECT id, agent, task, status, result, created_at, finished_at "
                f"FROM tasks {where} ORDER BY id DESC LIMIT ?",
                params,
            )
            return [dict(r) for r in rows]

    def task_count(self) -> int:
        with self._lock:
            return int(self.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])

    # --- API keys ------------------------------------------------------------
    @staticmethod
    def _key_row(r) -> dict:
        now = time.time()
        exp = r["expires_at"]
        revoked = bool(r["revoked"])
        expired = bool(exp is not None and exp <= now)
        status = "revoked" if revoked else ("expired" if expired else "active")
        return {
            "id": r["id"],
            "name": r["name"],
            "key_prefix": r["key_prefix"],
            "scopes": json.loads(r["scopes"]),
            "created_at": r["created_at"],
            "expires_at": exp,
            "last_used_at": r["last_used_at"],
            "request_count": r["request_count"],
            "revoked": revoked,
            "status": status,
        }

    def create_api_key(self, name: str, scopes=None, expires_in_days=None):
        """Create and persist a new API key. Returns (raw_key, metadata_dict)."""
        label = name.strip()
        if not label or len(label) > 80:
            raise ValueError("key name must be 1-80 characters")
        norm_scopes = _normalize_scopes(scopes)
        now = time.time()
        expires_at = None
        if expires_in_days is not None:
            days = float(expires_in_days)
            if days <= 0:
                raise ValueError("expires_in_days must be positive")
            expires_at = now + days * 86400.0
        key_id = f"key_{secrets.token_hex(6)}"
        raw_key = f"hive_sk_{secrets.token_urlsafe(32)}"
        prefix = f"{raw_key[:14]}..."
        key_hash = _hash_token(raw_key)
        with self._lock:
            self.db.execute(
                "INSERT INTO api_keys (id, name, key_prefix, key_hash, scopes, created_at, expires_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (key_id, label, prefix, key_hash, json.dumps(norm_scopes), now, expires_at),
            )
            self.db.commit()
            row = self.db.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
        return raw_key, self._key_row(row)

    def list_api_keys(self) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM api_keys ORDER BY created_at DESC")
            return [self._key_row(r) for r in rows]

    def get_api_key(self, key_id: str):
        with self._lock:
            r = self.db.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
            return self._key_row(r) if r else None

    def revoke_api_key(self, key_id: str) -> bool:
        with self._lock:
            n = self.db.execute(
                "UPDATE api_keys SET revoked=1 WHERE id=? AND revoked=0", (key_id,)
            ).rowcount
            self.db.commit()
            return n > 0

    def delete_api_key(self, key_id: str) -> bool:
        with self._lock:
            n = self.db.execute("DELETE FROM api_keys WHERE id=?", (key_id,)).rowcount
            self.db.commit()
            return n > 0

    def rotate_api_key(self, key_id: str):
        """Revoke an existing key and mint a replacement with the same name & scopes."""
        with self._lock:
            old = self.db.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
            if not old:
                return None
            remaining_days = None
            if old["expires_at"] is not None:
                rem = old["expires_at"] - time.time()
                if rem > 0:
                    remaining_days = rem / 86400.0
            self.db.execute("UPDATE api_keys SET revoked=1 WHERE id=?", (key_id,))
            self.db.commit()
        return self.create_api_key(old["name"], json.loads(old["scopes"]), remaining_days)

    def has_active_api_keys(self) -> bool:
        now = time.time()
        with self._lock:
            r = self.db.execute(
                "SELECT 1 FROM api_keys WHERE revoked=0 AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
                (now,),
            ).fetchone()
            return r is not None

    def verify_api_key(self, raw_key: str):
        """Validate a raw API key, update usage counters, and return metadata or None."""
        key_hash = _hash_token(raw_key)
        now = time.time()
        with self._lock:
            r = self.db.execute("SELECT * FROM api_keys WHERE key_hash=?", (key_hash,)).fetchone()
            if not r:
                return None
            if r["revoked"] or (r["expires_at"] is not None and r["expires_at"] <= now):
                return None
            self.db.execute(
                "UPDATE api_keys SET last_used_at=?, request_count=request_count+1 WHERE id=?",
                (now, r["id"]),
            )
            self.db.commit()
            r2 = self.db.execute("SELECT * FROM api_keys WHERE id=?", (r["id"],)).fetchone()
            return self._key_row(r2)
