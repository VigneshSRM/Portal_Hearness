"""Local SQLite store: closure proposals (approval queue), audit log and a journal cache.

SQLite (stdlib) in WAL mode lets the CLI, the scheduler and the dashboard share the
same queue safely. Nothing here talks to Redmine.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    issue_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_issue ON proposals(issue_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    outcome TEXT,
    details TEXT
);

CREATE TABLE IF NOT EXISTS journal_cache (
    issue_id INTEGER PRIMARY KEY,
    updated_on TEXT NOT NULL,
    data TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class HarnessStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if str(path) != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ proposals
    def save_proposal(self, data: dict[str, Any]) -> None:
        with self._lock:
            ts = now_iso()
            data.setdefault("created_at", ts)
            data["updated_at"] = ts
            self._conn.execute(
                "INSERT INTO proposals (id, status, issue_id, created_at, updated_at, data) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at, "
                "data=excluded.data",
                (data["id"], data["status"], int(data["issue_id"]), data["created_at"], ts,
                 json.dumps(data, default=str)),
            )
            self._conn.commit()

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT data FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def list_proposals(
        self,
        status: str | list[str] | None = None,
        issue_id: int | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        sql = "SELECT data FROM proposals"
        clauses: list[str] = []
        args: list[Any] = []
        if status:
            statuses = [status] if isinstance(status, str) else list(status)
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            args.extend(statuses)
        if issue_id is not None:
            clauses.append("issue_id = ?")
            args.append(int(issue_id))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def count_proposals(self, status: str) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM proposals WHERE status = ?", (status,)).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------ audit
    def audit(
        self,
        *,
        actor: str,
        action: str,
        target: str = "",
        outcome: str = "ok",
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log (ts, actor, action, target, outcome, details) VALUES (?, ?, ?, ?, ?, ?)",
                (now_iso(), actor or "unknown", action, target, outcome, json.dumps(details or {}, default=str)),
            )
            self._conn.commit()

    def list_audit(self, limit: int = 200, target: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit_log"
        args: list[Any] = []
        if target:
            sql += " WHERE target = ?"
            args.append(target)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["details"] = json.loads(item.get("details") or "{}")
            out.append(item)
        return out

    # ------------------------------------------------------------------ journal cache
    def get_journals(self, issue_id: int, updated_on: str) -> list[dict[str, Any]] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT updated_on, data FROM journal_cache WHERE issue_id = ?", (int(issue_id),)
            ).fetchone()
        if row and row["updated_on"] == updated_on:
            return json.loads(row["data"])
        return None

    def put_journals(self, issue_id: int, updated_on: str, journals: list[dict[str, Any]]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO journal_cache (issue_id, updated_on, data) VALUES (?, ?, ?) "
                "ON CONFLICT(issue_id) DO UPDATE SET updated_on=excluded.updated_on, data=excluded.data",
                (int(issue_id), updated_on, json.dumps(journals, default=str)),
            )
            self._conn.commit()
