"""FastAPI app. Run: py -3.13 -m uvicorn api.main:create_app --factory --port 8000   (serves web/dist if built)

The engine is used as-is: every job view is siliconscout.output.to_output(Job) plus the job's audit log and
cost ledger. This layer only adds users, credits and Stripe.
"""
from __future__ import annotations

import os
import re
import secrets
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Union
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from siliconscout.config import ROOT, Config
from siliconscout.output import to_output, validate
from siliconscout.pipeline import Job

from .billing import CREDITS_PER_JOB, CURRENCY, PACKS, StripeGateway, StripeNotConfigured, grant_from_session
from .db import DB, InsufficientCredits, check_password

COOKIE = "ss_session"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Creds(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=8, max_length=200)
    name: str = Field(default="", max_length=100)


class NewJob(BaseModel):
    need: str = Field(min_length=3, max_length=2000)
    source: Literal["fixture", "registry"] = "fixture"


class Approve(BaseModel):
    rfq_ids: Union[Literal["all"], list[str]] = "all"


class QuoteIn(BaseModel):
    candidate_id: str
    label: Literal["SUPPLIER_QUOTE", "DISTRIBUTOR_LIVE", "PUBLIC_BUDGETARY"] = "SUPPLIER_QUOTE"
    unit_price: Optional[float] = Field(default=None, ge=0)
    quantity: Optional[int] = Field(default=None, ge=0)
    delivery_days: Optional[int] = Field(default=None, ge=0)
    warranty_months: Optional[int] = Field(default=None, ge=0)
    approval_ref: Optional[str] = Field(default=None, max_length=100)
    model: Optional[str] = Field(default=None, max_length=100)
    gst_pct: Optional[float] = Field(default=None, ge=0)
    freight: Optional[float] = Field(default=None, ge=0)
    installation: Optional[float] = Field(default=None, ge=0)
    payment_terms: Optional[str] = Field(default=None, max_length=200)
    validity_until: Optional[str] = Field(default=None, max_length=40)


class CheckoutIn(BaseModel):
    pack: str


class ConfirmIn(BaseModel):
    session_id: str = Field(max_length=200)


class GrantIn(BaseModel):
    delta: int = Field(ge=-100000, le=100000)
    note: str = Field(default="", max_length=200)


def create_app(cfg: Optional[Config] = None, db_path: Optional[Path] = None,
               stripe_gw: Optional[StripeGateway] = None,
               job_factory: Optional[Callable[..., Job]] = None,
               google_http: Optional[Any] = None) -> FastAPI:
    cfg = cfg or Config.load()  # also loads .env into the environment (never overrides real env vars)
    db = DB(Path(db_path or os.environ.get("SS_WEB_DB") or (cfg.jobs_dir / "_web" / "app.sqlite")))
    gw = stripe_gw or StripeGateway()
    base_url = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    secure_cookie = base_url.startswith("https://")
    g_id, g_secret = os.environ.get("GOOGLE_CLIENT_ID", ""), os.environ.get("GOOGLE_CLIENT_SECRET", "")
    # Admins: these emails, and only when signed in with Google (a verified email). Admins never pay for jobs.
    admins = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
    # Crustdata spends real credits from the operator's account; web jobs stay on dry-run unless opted in.
    crust_spend = os.environ.get("SS_WEB_CRUSTDATA_SPEND") == "1"
    make_job = job_factory or (lambda jid: Job(jid, cfg, crust_spend=crust_spend))
    locks: dict[str, threading.Lock] = {}
    locks_guard = threading.Lock()

    def job_lock(jid: str) -> threading.Lock:
        with locks_guard:
            return locks.setdefault(jid, threading.Lock())

    app = FastAPI(title="SiliconScout 3.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.db, app.state.stripe = db, gw

    # --- auth helpers ---------------------------------------------------------------------------
    def set_session(resp: Response, uid: int, method: str = "password") -> None:
        resp.set_cookie(COOKIE, db.new_session(uid, method), httponly=True, samesite="lax", secure=secure_cookie,
                        max_age=14 * 24 * 3600, path="/")

    def current_user(request: Request) -> dict:
        u = db.session_user(request.cookies.get(COOKIE))
        if not u:
            raise HTTPException(401, "Sign in required.")
        u["is_admin"] = bool(u["email"] in admins and u.get("signed_in_with") == "google")
        return u

    def require_admin(u: dict = Depends(current_user)) -> dict:
        if not u["is_admin"]:
            raise HTTPException(403, "Admin only.")
        return u

    def owned_job(jid: str, u: dict) -> dict:
        row = db.job_any(jid) if u.get("is_admin") else db.job(jid, u["id"])
        if not row:
            raise HTTPException(404, "Job not found.")
        return row

    # --- public config ---------------------------------------------------------------------------
    @app.get("/api/config")
    def public_config():
        return {"google_enabled": bool(g_id and g_secret), "stripe_enabled": gw.enabled,
                "stripe_live_key_rejected": gw.live_key_rejected, "webhook_enabled": gw.webhook_enabled,
                "credits_per_job": CREDITS_PER_JOB, "currency": CURRENCY,
                "packs": [{"id": k, **v} for k, v in PACKS.items()],
                "engine_mode": cfg.limits.get("mode"),
                "llm_enabled": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "crustdata": "live" if (crust_spend and os.environ.get("CRUSTDATA_API_KEY")) else
                             ("dry_run" if os.environ.get("CRUSTDATA_API_KEY") else "off")}

    # --- auth ------------------------------------------------------------------------------------
    @app.post("/api/auth/signup")
    def signup(c: Creds, response: Response):
        email = c.email.strip().lower()
        if not EMAIL_RE.match(email):
            raise HTTPException(400, "Enter a valid email address.")
        if email in admins:
            raise HTTPException(403, "This is an admin account. Use Continue with Google.")
        if db.user_by_email(email):
            raise HTTPException(409, "An account with this email already exists. Sign in instead.")
        uid = db.create_user(email, c.password, c.name.strip())
        set_session(response, uid)
        return {**db.user(uid), "is_admin": False}

    @app.post("/api/auth/login")
    def login(c: Creds, response: Response):
        if c.email.strip().lower() in admins:
            raise HTTPException(403, "This is an admin account. Use Continue with Google.")
        u = db.user_by_email(c.email)
        if not u or not check_password(c.password, u.get("pw_hash")):
            raise HTTPException(401, "Email or password is incorrect.")
        set_session(response, u["id"])
        return {**db.user(u["id"]), "is_admin": False}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        db.end_session(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(u: dict = Depends(current_user)):
        return u

    @app.get("/api/auth/google/start")
    def google_start():
        if not (g_id and g_secret):
            return RedirectResponse("/#/auth?error=google_off", 302)
        state = secrets.token_urlsafe(24)
        q = urlencode({"client_id": g_id, "redirect_uri": f"{base_url}/api/auth/google/callback",
                       "response_type": "code", "scope": "openid email profile", "state": state,
                       "prompt": "select_account"})
        r = RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{q}", 302)
        r.set_cookie("ss_oauth_state", state, httponly=True, samesite="lax", secure=secure_cookie, max_age=600)
        return r

    @app.get("/api/auth/google/callback")
    def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        if error or not code or not state or state != request.cookies.get("ss_oauth_state"):
            return RedirectResponse("/#/auth?error=google", 302)
        import httpx
        http = google_http or httpx
        tok = http.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": g_id, "client_secret": g_secret,
            "redirect_uri": f"{base_url}/api/auth/google/callback", "grant_type": "authorization_code"}, timeout=15)
        if tok.status_code != 200:
            return RedirectResponse("/#/auth?error=google", 302)
        info = http.get("https://openidconnect.googleapis.com/v1/userinfo",
                        headers={"Authorization": f"Bearer {tok.json()['access_token']}"}, timeout=15).json()
        if not info.get("email") or not info.get("email_verified") or not info.get("sub"):
            return RedirectResponse("/#/auth?error=google_email", 302)
        uid = db.google_user(info["sub"], info["email"].lower(), info.get("name", ""))
        r = RedirectResponse("/#/", 302)
        set_session(r, uid, "google")
        r.delete_cookie("ss_oauth_state")
        return r

    # --- billing ---------------------------------------------------------------------------------
    @app.get("/api/billing/history")
    def history(u: dict = Depends(current_user)):
        return {"credits": u["credits"], "history": db.history(u["id"])}

    @app.post("/api/billing/checkout")
    def checkout(body: CheckoutIn, u: dict = Depends(current_user)):
        if body.pack not in PACKS:
            raise HTTPException(400, "Unknown credit pack.")
        try:
            return gw.create_checkout(user_id=u["id"], email=u["email"], pack_id=body.pack, base_url=base_url)
        except StripeNotConfigured:
            raise HTTPException(503, "Payments are not configured: set STRIPE_SECRET_KEY (sk_test_...) in .env.")

    @app.post("/api/billing/confirm")
    def confirm(body: ConfirmIn, u: dict = Depends(current_user)):
        """Return-page fallback for when the webhook hasn't arrived (e.g. no `stripe listen` locally)."""
        try:
            s = gw.retrieve(body.session_id)
        except StripeNotConfigured:
            raise HTTPException(503, "Payments are not configured.")
        except Exception:
            raise HTTPException(400, "Could not look up that checkout session.")
        owner = (s.get("metadata") or {}).get("user_id") or s.get("client_reference_id")
        if str(owner) != str(u["id"]):
            raise HTTPException(403, "That checkout session belongs to another account.")
        granted, msg = grant_from_session(db, s, "return_page")
        return {"granted": granted, "message": msg, "credits": db.user(u["id"])["credits"]}

    @app.post("/api/stripe/webhook")
    async def stripe_webhook(request: Request):
        payload = await request.body()
        try:
            event = gw.verify_event(payload, request.headers.get("stripe-signature", ""))
        except StripeNotConfigured:
            raise HTTPException(503, "Webhook secret not configured.")
        except Exception:
            raise HTTPException(400, "Invalid signature.")
        if event.get("type") in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            granted, msg = grant_from_session(db, event["data"]["object"], "webhook")
            return {"received": True, "granted": granted, "message": msg}
        return {"received": True}

    # --- jobs ------------------------------------------------------------------------------------
    def view(jid: str) -> dict:
        job = make_job(jid)
        out = to_output(job)
        errs = validate(out, cfg.output_schema)
        db.set_job_status(jid, out["status"])
        return {"output": out, "contract_errors": errs, "audit": job.store.audit_rows(),
                "ledger": job.store.ledger_rows()}

    @app.get("/api/jobs")
    def list_jobs(u: dict = Depends(current_user)):
        return db.jobs(u["id"])

    @app.post("/api/jobs")
    def new_job(body: NewJob, u: dict = Depends(current_user)):
        jid = uuid.uuid4().hex[:12]
        cost = 0 if u["is_admin"] else CREDITS_PER_JOB
        try:
            db.adjust(u["id"], -cost, "admin_run" if u["is_admin"] else "job_run", jid, body.need[:120])
        except InsufficientCredits:
            raise HTTPException(402, f"Not enough credits: a sourcing job costs {CREDITS_PER_JOB}. Buy credits first.")
        db.add_job(jid, u["id"], body.need, body.source)
        try:
            with job_lock(jid):
                make_job(jid).run(body.need, body.source)
        except Exception as e:  # engine failure: refund, keep the row for the audit trail
            if cost:
                db.adjust(u["id"], cost, "refund", jid, f"job failed: {type(e).__name__}")
            db.set_job_status(jid, "ERROR")
            raise HTTPException(500, f"The sourcing run failed and your credit was refunded ({type(e).__name__}: {e}).")
        return {"job_id": jid, **view(jid)}

    @app.get("/api/jobs/{jid}")
    def get_job(jid: str, u: dict = Depends(current_user)):
        row = {**owned_job(jid, u), "charged": db.job_charge(jid)}
        if row["status"] == "ERROR":
            return {"meta": row, "output": None}
        with job_lock(jid):
            return {"meta": row, **view(jid)}

    @app.post("/api/jobs/{jid}/approve")
    def approve(jid: str, body: Approve, u: dict = Depends(current_user)):
        owned_job(jid, u)
        with job_lock(jid):
            job = make_job(jid)
            done = job.approve(body.rfq_ids, by=u["email"])
            job.run()  # resume: sends approved RFQs through the idempotent outbox
            return {"approved": done, **view(jid)}

    @app.post("/api/jobs/{jid}/quote")
    def quote(jid: str, body: QuoteIn, u: dict = Depends(current_user)):
        owned_job(jid, u)
        raw = {k: v for k, v in body.model_dump().items() if v not in (None, "")}
        with job_lock(jid):
            job = make_job(jid)
            if not any(a.candidate_id == body.candidate_id for a in job.state.assessments):
                raise HTTPException(400, "Unknown candidate for this job.")
            try:
                job.add_quote(raw)
            except ValueError as e:
                raise HTTPException(400, str(e))
            return view(jid)

    # --- admin -----------------------------------------------------------------------------------
    @app.get("/api/admin/overview")
    def admin_overview(_: dict = Depends(require_admin)):
        users = db.all_users()
        for x in users:
            x["is_admin"] = x["email"] in admins
        pays = db.all_payments()
        return {"users": users, "payments": pays, "jobs": db.all_jobs(),
                "totals": {"users": len(users), "paid_amount": sum(p["amount"] or 0 for p in pays),
                           "credits_outstanding": sum(x["credits"] for x in users if not x["is_admin"])}}

    @app.post("/api/admin/users/{uid}/credits")
    def admin_grant(uid: int, body: GrantIn, a: dict = Depends(require_admin)):
        if not db.user(uid):
            raise HTTPException(404, "User not found.")
        try:
            bal = db.adjust(uid, body.delta, "admin_grant" if body.delta >= 0 else "admin_debit", a["email"],
                            body.note or f"by admin {a['email']}")
        except InsufficientCredits:
            raise HTTPException(400, "That would take the balance below zero.")
        return {"credits": bal}

    @app.exception_handler(HTTPException)
    async def http_err(_req, exc: HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    # --- frontend ----------------------------------------------------------------------------------
    dist = ROOT / "web" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    else:
        @app.get("/")
        def no_build():
            return JSONResponse({"detail": "Frontend not built. Run: cd web && npm install && npm run build "
                                           "(or npm run dev for hot reload on :5173)."})
    return app

