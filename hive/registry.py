"""SQLite store for agent specs, task history, and API keys."""
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

VALID_SCOPES = ("read", "write", "admin")

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
"""


@dataclass
class AgentSpec:
    name: str
    description: str
    system_prompt: str
    tools: list = field(default_factory=list)
    endpoint: str = ""
    model: str = ""


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


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
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)

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
        key_hash = _hash_key(raw_key)
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
        key_hash = _hash_key(raw_key)
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
