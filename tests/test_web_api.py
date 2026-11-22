"""Web layer: auth, ownership, Stripe webhook credits (signed, idempotent), job debit, approve + quote."""
import hashlib
import hmac
import json
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("stripe")
from fastapi.testclient import TestClient  # noqa: E402

from api.billing import PACKS, StripeGateway  # noqa: E402
from api.main import create_app  # noqa: E402
from siliconscout.config import Config  # noqa: E402

WHSEC = "whsec_unit_test_only"
NEED = "Need GPS trackers for 300 trucks, deliver to Pune"


class FakeGateway(StripeGateway):
    def __init__(self):
        super().__init__(secret_key="sk_test_fake", webhook_secret=WHSEC)
        self.sessions = {}

    def create_checkout(self, *, user_id, email, pack_id, base_url):
        sid = f"cs_test_{len(self.sessions) + 1}"
        p = PACKS[pack_id]
        self.sessions[sid] = {"id": sid, "payment_status": "unpaid", "amount_total": p["amount"], "currency": "inr",
                              "client_reference_id": str(user_id),
                              "metadata": {"user_id": str(user_id), "pack": pack_id, "credits": str(p["credits"])}}
        return {"id": sid, "url": f"https://checkout.stripe.test/{sid}"}

    def retrieve(self, session_id):
        return self.sessions[session_id]


def sign(payload: bytes, secret: str = WHSEC) -> str:
    t = str(int(time.time()))
    v1 = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={v1}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    cfg = Config.load(jobs_dir=tmp_path / "jobs")
    gw = FakeGateway()
    app = create_app(cfg, db_path=tmp_path / "web.sqlite", stripe_gw=gw)
    return TestClient(app), gw


def signup(c, email="ops@example.com"):
    r = c.post("/api/auth/signup", json={"email": email, "password": "correct horse"})
    assert r.status_code == 200, r.text
    return r.json()


def pay(c, gw, pack="starter"):
    sid = c.post("/api/billing/checkout", json={"pack": pack}).json()["id"]
    gw.sessions[sid]["payment_status"] = "paid"
    body = json.dumps({"type": "checkout.session.completed", "data": {"object": gw.sessions[sid]}}).encode()
    return sid, body


def test_auth_flow(env):
    c, _ = env
    assert c.get("/api/me").status_code == 401
    u = signup(c)
    assert u["credits"] == 0 and c.get("/api/me").json()["email"] == "ops@example.com"
    assert c.post("/api/auth/signup", json={"email": "OPS@example.com", "password": "another pw"}).status_code == 409
    c.post("/api/auth/logout")
    assert c.get("/api/me").status_code == 401
    assert c.post("/api/auth/login", json={"email": "ops@example.com", "password": "wrong pass"}).status_code == 401
    assert c.post("/api/auth/login", json={"email": "ops@example.com", "password": "correct horse"}).status_code == 200


def test_job_needs_credits(env):
    c, _ = env
    signup(c)
    r = c.post("/api/jobs", json={"need": NEED})
    assert r.status_code == 402


def test_webhook_signed_and_idempotent(env):
    c, gw = env
    signup(c)
    sid, body = pay(c, gw)
    assert c.post("/api/stripe/webhook", content=body, headers={"stripe-signature": sign(body, "whsec_wrong")}).status_code == 400
    r = c.post("/api/stripe/webhook", content=body, headers={"stripe-signature": sign(body)})
    assert r.json()["granted"] is True
    r = c.post("/api/stripe/webhook", content=body, headers={"stripe-signature": sign(body)})
    assert r.json()["granted"] is False  # replay does not double-credit
    assert c.post("/api/billing/confirm", json={"session_id": sid}).json()["granted"] is False
    h = c.get("/api/billing/history").json()
    assert h["credits"] == PACKS["starter"]["credits"] and len(h["history"]) == 1


def test_tampered_amount_not_credited(env):
    c, gw = env
    signup(c)
    sid, _ = pay(c, gw)
    gw.sessions[sid]["amount_total"] = 100
    assert c.post("/api/billing/confirm", json={"session_id": sid}).json()["granted"] is False
    assert c.get("/api/me").json()["credits"] == 0


def test_live_key_rejected():
    gw = StripeGateway(secret_key="sk_live_nope", webhook_secret="")
    assert not gw.enabled and gw.live_key_rejected


def test_trucks_demo_end_to_end(env):
    c, gw = env
    signup(c)
    sid, _ = pay(c, gw)
    assert c.post("/api/billing/confirm", json={"session_id": sid}).json()["credits"] == 10
    r = c.post("/api/jobs", json={"need": NEED})
    assert r.status_code == 200, r.text
    d = r.json()
    jid, out = d["job_id"], d["output"]
    assert d["contract_errors"] == []
    assert out["status"] == "AWAITING_APPROVAL" and out["spec"]["quantity"] == 300 and out["rfqs"]
    assert c.get("/api/me").json()["credits"] == 9
    assert [h["kind"] for h in c.get("/api/billing/history").json()["history"]] == ["job_run", "purchase"]
    # approve one, then the rest
    first = out["rfqs"][0]["rfq_id"]
    o = c.post(f"/api/jobs/{jid}/approve", json={"rfq_ids": [first]}).json()["output"]
    assert next(r for r in o["rfqs"] if r["rfq_id"] == first)["status"] == "APPROVED"
    o = c.post(f"/api/jobs/{jid}/approve", json={"rfq_ids": "all"}).json()["output"]
    assert all(r["status"] == "SENT" for r in o["rfqs"]) and o["status"] == "RFQS_OUTSTANDING"
    # quote intake -> decision
    cid = o["rfqs"][0]["candidate_id"]
    q = c.post(f"/api/jobs/{jid}/quote", json={
        "candidate_id": cid, "label": "SUPPLIER_QUOTE", "unit_price": 4200, "quantity": 300, "delivery_days": 21,
        "warranty_months": 12, "approval_ref": next(x for x in o["candidates"] if x["candidate_id"] == cid)
        ["registry_hits"][0]["ref"]})
    assert q.status_code == 200, q.text
    assert next(x for x in q.json()["output"]["candidates"] if x["candidate_id"] == cid)["quote"]
    # approving/quoting is free
    assert c.get("/api/me").json()["credits"] == 9
    assert c.get("/api/jobs").json()[0]["id"] == jid


def test_jobs_are_private(env):
    c, gw = env
    signup(c, "a@example.com")
    sid, _ = pay(c, gw)
    c.post("/api/billing/confirm", json={"session_id": sid})
    jid = c.post("/api/jobs", json={"need": NEED}).json()["job_id"]
    c.post("/api/auth/logout")
    signup(c, "b@example.com")
    assert c.get(f"/api/jobs/{jid}").status_code == 404
    assert c.post(f"/api/jobs/{jid}/approve", json={"rfq_ids": "all"}).status_code == 404
    assert c.post("/api/billing/confirm", json={"session_id": sid}).status_code == 403
    assert c.get("/api/jobs").json() == []


def test_failed_run_refunds(tmp_path):
    class Boom:
        def __init__(self, *_a, **_k):
            pass

        def run(self, *_a, **_k):
            raise RuntimeError("engine down")

    gw = FakeGateway()
    app = create_app(Config.load(jobs_dir=tmp_path / "jobs"), db_path=tmp_path / "w.sqlite", stripe_gw=gw,
                     job_factory=Boom)
    c = TestClient(app)
    signup(c)
    sid, _ = pay(c, gw)
    c.post("/api/billing/confirm", json={"session_id": sid})
    assert c.post("/api/jobs", json={"need": NEED}).status_code == 500
    assert c.get("/api/me").json()["credits"] == 10


# --- Google OAuth + admin -----------------------------------------------------------------------
ADMIN = "boss@example.com"


class FakeGoogle:
    """Stands in for httpx: token exchange + userinfo. `who` maps auth code -> userinfo."""

    def __init__(self, who):
        self.who = who

    class R:
        def __init__(self, code, data):
            self.status_code, self._d = code, data

        def json(self):
            return self._d

    def post(self, url, data=None, **_):
        code = data["code"]
        return self.R(200, {"access_token": code}) if code in self.who else self.R(400, {})

    def get(self, url, headers=None, **_):
        return self.R(200, self.who[headers["Authorization"].split()[1]])


@pytest.fixture
def genv(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("ADMIN_EMAILS", f"{ADMIN}, other-admin@example.com")
    who = {"admin-code": {"sub": "g-1", "email": ADMIN, "email_verified": True, "name": "Boss"},
           "user-code": {"sub": "g-2", "email": "user@example.com", "email_verified": True, "name": "U"},
           "unverified": {"sub": "g-3", "email": "x@example.com", "email_verified": False}}
    gw = FakeGateway()
    app = create_app(Config.load(jobs_dir=tmp_path / "jobs"), db_path=tmp_path / "w.sqlite", stripe_gw=gw,
                     google_http=FakeGoogle(who))
    return TestClient(app), gw


def google_login(c, code):
    r = c.get("/api/auth/google/start", follow_redirects=False)
    assert r.status_code == 302 and "accounts.google.com" in r.headers["location"]
    state = r.headers["location"].split("state=")[1].split("&")[0]
    return c.get(f"/api/auth/google/callback?code={code}&state={state}", follow_redirects=False)


def test_google_login_and_state_check(genv):
    c, _ = genv
    assert c.get("/api/config").json()["google_enabled"] is True
    c.get("/api/auth/google/start", follow_redirects=False)
    bad = c.get("/api/auth/google/callback?code=user-code&state=forged", follow_redirects=False)
    assert "error=google" in bad.headers["location"] and c.get("/api/me").status_code == 401
    assert "error=google_email" in google_login(c, "unverified").headers["location"]
    assert google_login(c, "user-code").headers["location"] == "/#/"
    me = c.get("/api/me").json()
    assert me["email"] == "user@example.com" and me["is_admin"] is False and me["credits"] == 0
    assert c.post("/api/jobs", json={"need": NEED}).status_code == 402  # users pay
    assert c.get("/api/admin/overview").status_code == 403


def test_admin_unlimited_and_manages_users(genv):
    c, gw = genv
    # password route is closed for admin emails, so a squatter can't claim the admin account
    assert c.post("/api/auth/signup", json={"email": ADMIN, "password": "squatter-pw"}).status_code == 403
    assert c.post("/api/auth/login", json={"email": ADMIN, "password": "squatter-pw"}).status_code == 403
    # a regular user signs up and pays
    signup(c, "payer@example.com")
    sid, _ = pay(c, gw)
    c.post("/api/billing/confirm", json={"session_id": sid})
    payer_job = c.post("/api/jobs", json={"need": NEED}).json()["job_id"]
    c.post("/api/auth/logout")

    google_login(c, "admin-code")
    me = c.get("/api/me").json()
    assert me["is_admin"] is True and me["signed_in_with"] == "google"
    for _ in range(2):  # no credits, still runs: admins are never debited
        assert c.post("/api/jobs", json={"need": NEED}).status_code == 200
    assert c.get("/api/me").json()["credits"] == 0
    assert {h["kind"] for h in c.get("/api/billing/history").json()["history"]} == {"admin_run"}
    ov = c.get("/api/admin/overview").json()
    payer = next(u for u in ov["users"] if u["email"] == "payer@example.com")
    assert payer["credits"] == 9 and payer["paid_amount"] == PACKS["starter"]["amount"] and payer["jobs"] == 1
    assert len(ov["payments"]) == 1 and ov["totals"]["credits_outstanding"] == 9
    assert c.get(f"/api/jobs/{payer_job}").status_code == 200  # admin can open any job
    assert c.post(f"/api/admin/users/{payer['id']}/credits", json={"delta": 5, "note": "goodwill"}).json()["credits"] == 14
    assert c.post(f"/api/admin/users/{payer['id']}/credits", json={"delta": -100}).status_code == 400


def test_google_link_drops_unverified_password(genv):
    c, _ = genv
    signup(c, "user@example.com")  # someone registers the address with a password first
    c.post("/api/auth/logout")
    google_login(c, "user-code")  # the real owner signs in with Google
    c.post("/api/auth/logout")
    assert c.post("/api/auth/login", json={"email": "user@example.com", "password": "correct horse"}).status_code == 401


def test_follow_up_route(env):
    c, gw = env
    signup(c)
    sid, _ = pay(c, gw)
    c.post("/api/billing/confirm", json={"session_id": sid})
    jid = c.post("/api/jobs", json={"need": NEED}).json()["job_id"]
    r = c.post(f"/api/jobs/{jid}/follow_up", json={"text": "deliver to Chennai"})
    from siliconscout.pipeline import Job
    if hasattr(Job, "follow_up"):
        assert r.status_code == 200 and "reply" in r.json() and "output" in r.json()
    else:
        assert r.status_code == 501
    assert c.get("/api/me").json()["credits"] == 9  # follow-ups are free
    c.post("/api/auth/logout")
    signup(c, "other@example.com")
    assert c.post(f"/api/jobs/{jid}/follow_up", json={"text": "x"}).status_code == 404
