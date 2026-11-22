"""Web-app SQLite: users, sessions, credit ledger, payments, job ownership. Separate from per-job engine DBs."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
import os


def _free_credits() -> int:
    """Starter credits for every new account (SS_WEB_FREE_CREDITS, default 0) so jobs run without Stripe."""
    try:
        return max(0, int(os.environ.get("SS_WEB_FREE_CREDITS", "0")))
    except ValueError:
        return 0

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT DEFAULT '',
  pw_hash TEXT, google_sub TEXT UNIQUE, credits INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  method TEXT NOT NULL DEFAULT 'password');
CREATE TABLE IF NOT EXISTS credit_tx(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, delta INTEGER NOT NULL, balance_after INTEGER NOT NULL,
  kind TEXT NOT NULL, ref TEXT DEFAULT '', note TEXT DEFAULT '', ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payments(
  stripe_session_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, pack TEXT, credits INTEGER NOT NULL,
  amount INTEGER, currency TEXT, via TEXT, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, need TEXT NOT NULL, source TEXT NOT NULL,
  status TEXT DEFAULT 'NEW', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id, created_at);
CREATE INDEX IF NOT EXISTS tx_user ON credit_tx(user_id, id);
"""

SESSION_DAYS = 14


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def check_password(pw: str, stored: Optional[str]) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    _, salt, dk = stored.split("$")
    got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
    return hmac.compare_digest(got.hex(), dk)


def _th(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class InsufficientCredits(Exception):
    pass


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(sessions)")}
        if "method" not in cols:  # databases created before sign-in methods were tracked
            self.db.execute("ALTER TABLE sessions ADD COLUMN method TEXT NOT NULL DEFAULT 'password'")
        self.lock = threading.RLock()

    def _one(self, sql: str, *args) -> Optional[dict]:
        with self.lock:
            r = self.db.execute(sql, args).fetchone()
        return dict(r) if r else None

    def _all(self, sql: str, *args) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # --- users ---------------------------------------------------------------------------------
    def user(self, uid: int) -> Optional[dict]:
        return self._one("SELECT id,email,name,credits,created_at,google_sub IS NOT NULL AS google,"
                         " pw_hash IS NOT NULL AS has_password FROM users WHERE id=?", uid)

    def user_by_email(self, email: str) -> Optional[dict]:
        return self._one("SELECT * FROM users WHERE email=?", email.strip().lower())

    def create_user(self, email: str, pw: Optional[str] = None, name: str = "", google_sub: Optional[str] = None) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO users(email,name,pw_hash,google_sub,created_at) VALUES(?,?,?,?,?)",
                (email.strip().lower(), name, hash_password(pw) if pw else None, google_sub, now()))
            uid = cur.lastrowid
            free = _free_credits()
            if free > 0:
                self._apply(uid, free, "signup_bonus", "", "Free starter credits")
            return uid

    def google_user(self, sub: str, email: str, name: str) -> int:
        """Find by Google sub, else link to an existing account with the same (Google-verified) email, else create."""
        with self.lock:
            u = self._one("SELECT id FROM users WHERE google_sub=?", sub)
            if u:
                return u["id"]
            u = self.user_by_email(email)
            if u:
                # Google has proven ownership of this email; a password set by whoever registered it first was
                # never email-verified, so drop it rather than leave a second way in.
                self.db.execute("UPDATE users SET google_sub=?, pw_hash=NULL, name=COALESCE(NULLIF(name,''),?) "
                                "WHERE id=?", (sub, name, u["id"]))
                self.db.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
                return u["id"]
            return self.create_user(email, None, name, sub)

    # --- sessions ------------------------------------------------------------------------------
    def new_session(self, uid: int, method: str = "password") -> str:
        token = secrets.token_urlsafe(32)
        exp = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
        with self.lock:
            self.db.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at,method) VALUES(?,?,?,?,?)",
                            (_th(token), uid, now(), exp, method))
        return token

    def session_user(self, token: Optional[str]) -> Optional[dict]:
        if not token:
            return None
        s = self._one("SELECT user_id, expires_at, method FROM sessions WHERE token_hash=?", _th(token))
        if not s or s["expires_at"] < now():
            return None
        u = self.user(s["user_id"])
        if u:
            u["signed_in_with"] = s["method"]
        return u

    def end_session(self, token: Optional[str]) -> None:
        if token:
            with self.lock:
                self.db.execute("DELETE FROM sessions WHERE token_hash=?", (_th(token),))

    # --- credits -------------------------------------------------------------------------------
    def _apply(self, uid: int, delta: int, kind: str, ref: str, note: str) -> int:
        """Caller holds the lock and an open transaction."""
        if delta < 0:
            cur = self.db.execute("UPDATE users SET credits=credits+? WHERE id=? AND credits>=?", (delta, uid, -delta))
            if cur.rowcount == 0:
                raise InsufficientCredits()
        else:
            self.db.execute("UPDATE users SET credits=credits+? WHERE id=?", (delta, uid))
        bal = self.db.execute("SELECT credits FROM users WHERE id=?", (uid,)).fetchone()[0]
        self.db.execute("INSERT INTO credit_tx(user_id,delta,balance_after,kind,ref,note,ts) VALUES(?,?,?,?,?,?,?)",
                        (uid, delta, bal, kind, ref, note, now()))
        return bal

    def adjust(self, uid: int, delta: int, kind: str, ref: str = "", note: str = "") -> int:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                bal = self._apply(uid, delta, kind, ref, note)
                self.db.execute("COMMIT")
                return bal
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def grant_payment(self, session_id: str, uid: int, pack: str, credits: int, amount: int, currency: str,
                      via: str) -> bool:
        """Idempotent: one Stripe Checkout Session credits the account exactly once (webhook or return page)."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self.db.execute("SELECT 1 FROM payments WHERE stripe_session_id=?", (session_id,)).fetchone():
                    self.db.execute("ROLLBACK")
                    return False
                if not self.db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
                    raise ValueError("unknown user")
                self.db.execute("INSERT INTO payments VALUES(?,?,?,?,?,?,?,?)",
                                (session_id, uid, pack, credits, amount, currency, via, now()))
                self._apply(uid, credits, "purchase", session_id,
                            f"{pack}: {credits} credits, {currency.upper()} {amount / 100:.2f} (Stripe test)")
                self.db.execute("COMMIT")
                return True
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def history(self, uid: int, limit: int = 100) -> list[dict]:
        return self._all("SELECT delta,balance_after,kind,ref,note,ts FROM credit_tx WHERE user_id=? "
                         "ORDER BY id DESC LIMIT ?", uid, limit)

    # --- jobs ----------------------------------------------------------------------------------
    def add_job(self, jid: str, uid: int, need: str, source: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?)", (jid, uid, need, source, "NEW", now(), now()))

    def job(self, jid: str, uid: int) -> Optional[dict]:
        return self._one("SELECT * FROM jobs WHERE id=? AND user_id=?", jid, uid)

    def job_charge(self, jid: str) -> int:
        """Credits this job actually cost (debit minus any refund)."""
        r = self._one("SELECT COALESCE(-SUM(delta),0) AS c FROM credit_tx WHERE ref=? AND kind IN "
                      "('job_run','admin_run','refund')", jid)
        return int(r["c"]) if r else 0

    def job_any(self, jid: str) -> Optional[dict]:
        return self._one("SELECT * FROM jobs WHERE id=?", jid)

    def jobs(self, uid: int) -> list[dict]:
        return self._all("SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC, rowid DESC", uid)

    def set_job_status(self, jid: str, status: str) -> None:
        with self.lock:
            self.db.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (status, now(), jid))

    def delete_job_row(self, jid: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM jobs WHERE id=?", (jid,))

    # --- admin -----------------------------------------------------------------------------------
    def all_users(self) -> list[dict]:
        return self._all(
            "SELECT u.id, u.email, u.name, u.credits, u.created_at, u.google_sub IS NOT NULL AS google,"
            " u.pw_hash IS NOT NULL AS has_password,"
            " (SELECT COUNT(*) FROM jobs j WHERE j.user_id=u.id) AS jobs,"
            " (SELECT COALESCE(SUM(amount),0) FROM payments p WHERE p.user_id=u.id) AS paid_amount,"
            " (SELECT COALESCE(SUM(credits),0) FROM payments p WHERE p.user_id=u.id) AS paid_credits"
            " FROM users u ORDER BY u.created_at DESC, u.id DESC")

    def all_payments(self, limit: int = 200) -> list[dict]:
        return self._all("SELECT p.*, u.email FROM payments p JOIN users u ON u.id=p.user_id "
                         "ORDER BY p.ts DESC LIMIT ?", limit)

    def all_jobs(self, limit: int = 200) -> list[dict]:
        return self._all("SELECT j.*, u.email FROM jobs j JOIN users u ON u.id=j.user_id "
                         "ORDER BY j.created_at DESC, j.rowid DESC LIMIT ?", limit)
