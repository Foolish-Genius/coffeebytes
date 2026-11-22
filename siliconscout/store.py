"""SQLite persistence: checkpoints, outbox with idempotency keys, audit log. JSONL cost ledger."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .models import JobState


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, jobs_dir: Path, job_id: str):
        self.dir = Path(jobs_dir) / job_id
        (self.dir / "pages").mkdir(parents=True, exist_ok=True)
        (self.dir / "outbox").mkdir(exist_ok=True)
        self.job_id = job_id
        self.db = sqlite3.connect(self.dir / "job.sqlite", isolation_level=None)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS checkpoint(id INTEGER PRIMARY KEY, stage TEXT, state TEXT, ts TEXT);
            CREATE TABLE IF NOT EXISTS outbox(key TEXT PRIMARY KEY, kind TEXT, payload TEXT, status TEXT, ts TEXT);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT, event TEXT, detail TEXT);
            """
        )
        self.ledger_path = self.dir / "cost_ledger.jsonl"

    # --- checkpoints -------------------------------------------------------
    def checkpoint(self, state: JobState) -> None:
        self.db.execute(
            "INSERT INTO checkpoint(stage,state,ts) VALUES(?,?,?)",
            (state.stage, state.model_dump_json(), now()),
        )

    def latest(self) -> JobState | None:
        row = self.db.execute("SELECT state FROM checkpoint ORDER BY id DESC LIMIT 1").fetchone()
        return JobState.model_validate_json(row[0]) if row else None

    # --- audit ---------------------------------------------------------------
    def audit(self, event: str, **detail: Any) -> None:
        self.db.execute(
            "INSERT INTO audit(ts,event,detail) VALUES(?,?,?)", (now(), event, json.dumps(detail, default=str))
        )

    def audit_rows(self) -> list[dict]:
        return [
            {"ts": t, "event": e, "detail": json.loads(d)}
            for t, e, d in self.db.execute("SELECT ts,event,detail FROM audit ORDER BY id")
        ]

    # --- outbox --------------------------------------------------------------
    def outbox_status(self, key: str) -> str | None:
        row = self.db.execute("SELECT status FROM outbox WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def send_once(self, key: str, kind: str, payload: dict, sender: Callable[[dict], None]) -> str:
        """Exactly-once intent: PENDING is written before the send, SENT after.
        A PENDING row found on restart means the send outcome is unknown -> HELD_AMBIGUOUS for a person."""
        status = self.outbox_status(key)
        if status == "SENT":
            self.audit("outbox_skip_duplicate", key=key)
            return "SENT"
        if status in ("PENDING", "HELD_AMBIGUOUS"):
            self.db.execute("UPDATE outbox SET status='HELD_AMBIGUOUS', ts=? WHERE key=?", (now(), key))
            self.audit("outbox_ambiguous_hold", key=key)
            return "HELD_AMBIGUOUS"
        self.db.execute(
            "INSERT INTO outbox(key,kind,payload,status,ts) VALUES(?,?,?,?,?)",
            (key, kind, json.dumps(payload), "PENDING", now()),
        )
        sender(payload)
        self.db.execute("UPDATE outbox SET status='SENT', ts=? WHERE key=?", (now(), key))
        self.audit("outbox_sent", key=key, kind=kind)
        return "SENT"

    def release_ambiguous(self, key: str, mark_sent: bool) -> None:
        """Human decision on an ambiguous send: confirm it went out, or allow one resend."""
        if mark_sent:
            self.db.execute("UPDATE outbox SET status='SENT' WHERE key=?", (key,))
        else:
            self.db.execute("DELETE FROM outbox WHERE key=?", (key,))
        self.audit("outbox_ambiguous_released", key=key, mark_sent=mark_sent)

    # --- cost ledger -----------------------------------------------------------
    def ledger(self, provider: str, op: str, inr: float, duration_ms: int, **kw: Any) -> None:
        line = {"ts": now(), "job_id": self.job_id, "provider": provider, "op": op,
                "inr_estimate": round(inr, 4), "duration_ms": duration_ms, **kw}
        with self.ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")

    def ledger_rows(self) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        return [json.loads(l) for l in self.ledger_path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def spent_inr(self) -> float:
        return sum(r.get("inr_estimate", 0) for r in self.ledger_rows())
