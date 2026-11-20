"""State: runs, proposals, reviewer decisions and an audit log of every CRM write (SQLite).

Idempotency lives here. Every proposal has a fingerprint built from what it changes (kind + account +
target values). When a run produces a proposal whose fingerprint is already stored:
    pending              -> refresh its evidence, keep it in the queue (no duplicate row)
    approved / applied   -> skip (and the CRM already reflects it, so it usually isn't produced at all)
    rejected             -> skip: a reviewer said no, and we never ask again
    failed               -> skip: stays in the queue for a retry, not re-proposed as new
Pending proposals that a new run no longer produces (the data changed or someone fixed it by hand)
are marked obsolete so nobody approves a stale change.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats_json TEXT,
    classification_json TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    location_slug TEXT,
    account_id TEXT,
    confidence TEXT,
    payload_json TEXT NOT NULL,          -- actions, before, evidence
    status TEXT NOT NULL DEFAULT 'pending',
    created_run INTEGER,
    last_seen_run INTEGER,
    decided_at TEXT,
    decision_comment TEXT,
    result_json TEXT
);
CREATE TABLE IF NOT EXISTS writes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER,
    at TEXT NOT NULL,
    method TEXT, path TEXT, request_json TEXT, status INTEGER, response_json TEXT
);
"""
DECIDED = ("approved", "applied", "rejected", "failed")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or config.STATE_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    # ------------------------------------------------------------------ runs
    def start_run(self) -> int:
        with self.tx() as db:
            return db.execute("INSERT INTO runs (started_at) VALUES (?)", (now(),)).lastrowid

    def finish_run(self, run_id: int, stats: dict, classification: list[dict]) -> None:
        with self.tx() as db:
            db.execute("UPDATE runs SET finished_at=?, stats_json=?, classification_json=? WHERE id=?",
                       (now(), json.dumps(stats), json.dumps(classification), run_id))

    def last_run(self) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()

    def runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ------------------------------------------------------------------ proposals
    def sync(self, run_id: int, proposals: list) -> dict:
        """Merge one run's proposals into the queue. Returns counts for the run summary."""
        stats = {"new": 0, "still_pending": 0, "already_decided": 0, "obsolete": 0}
        seen = set()
        with self.tx() as db:
            for p in proposals:
                seen.add(p.fingerprint)
                payload = json.dumps({"actions": p.actions, "before": p.before, "evidence": p.evidence})
                row = db.execute("SELECT id, status FROM proposals WHERE fingerprint=?", (p.fingerprint,)).fetchone()
                if row is None:
                    db.execute("""INSERT INTO proposals (fingerprint, kind, title, summary, location_slug, account_id,
                                  confidence, payload_json, status, created_run, last_seen_run)
                                  VALUES (?,?,?,?,?,?,?,?, 'pending', ?, ?)""",
                               (p.fingerprint, p.kind, p.title, p.summary, p.location_slug, p.account_id,
                                p.confidence, payload, run_id, run_id))
                    stats["new"] += 1
                elif row["status"] in ("pending", "obsolete"):
                    db.execute("""UPDATE proposals SET title=?, summary=?, confidence=?, payload_json=?, status='pending',
                                  last_seen_run=? WHERE id=?""", (p.title, p.summary, p.confidence, payload, run_id, row["id"]))
                    stats["still_pending"] += 1
                else:
                    db.execute("UPDATE proposals SET last_seen_run=? WHERE id=?", (run_id, row["id"]))
                    stats["already_decided"] += 1
            for row in db.execute("SELECT id, fingerprint FROM proposals WHERE status='pending'").fetchall():
                if row["fingerprint"] not in seen:
                    db.execute("UPDATE proposals SET status='obsolete', last_seen_run=? WHERE id=?", (run_id, row["id"]))
                    stats["obsolete"] += 1
        return stats

    def get(self, proposal_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()

    def list(self, statuses: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        if statuses:
            marks = ",".join("?" * len(statuses))
            return self.db.execute(f"SELECT * FROM proposals WHERE status IN ({marks}) ORDER BY id", statuses).fetchall()
        return self.db.execute("SELECT * FROM proposals ORDER BY id").fetchall()

    def counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self.db.execute("SELECT status, COUNT(*) n FROM proposals GROUP BY status")}

    def decide(self, proposal_id: int, status: str, comment: str = "", result: dict | None = None) -> None:
        with self.tx() as db:
            db.execute("UPDATE proposals SET status=?, decided_at=?, decision_comment=?, result_json=? WHERE id=?",
                       (status, now(), comment or None, json.dumps(result) if result is not None else None, proposal_id))

    def save_result(self, proposal_id: int, result: dict) -> None:
        """Record partial progress (e.g. the id of an account just created) without changing the status."""
        with self.tx() as db:
            db.execute("UPDATE proposals SET result_json=? WHERE id=?", (json.dumps(result), proposal_id))

    # ------------------------------------------------------------------ audit log
    def log_write(self, proposal_id: int | None, entry: dict) -> None:
        with self.tx() as db:
            db.execute("INSERT INTO writes (proposal_id, at, method, path, request_json, status, response_json) VALUES (?,?,?,?,?,?,?)",
                       (proposal_id, now(), entry["method"], entry["path"], json.dumps(entry.get("request")),
                        entry["status"], json.dumps(entry.get("response"))))

    def writes(self, proposal_id: int | None = None, limit: int = 200) -> list[sqlite3.Row]:
        if proposal_id is not None:
            return self.db.execute("SELECT * FROM writes WHERE proposal_id=? ORDER BY id", (proposal_id,)).fetchall()
        return self.db.execute("SELECT * FROM writes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
