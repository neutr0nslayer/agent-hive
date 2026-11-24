"""SQLite store for agent specs and task history."""
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

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
"""


@dataclass
class AgentSpec:
    name: str
    description: str
    system_prompt: str
    tools: list = field(default_factory=list)
    endpoint: str = ""
    model: str = ""


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

    # agents
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

    # tasks
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

    def recent_tasks(self, limit=10):
        with self._lock:
            return [dict(r) for r in self.db.execute(
                "SELECT id, agent, task, status, result FROM tasks ORDER BY id DESC LIMIT ?",
                (limit,))]
