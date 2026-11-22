"""Per-feature website tests + latency benchmarks (no extra deps; thresholds are generous for CI)."""
import time

import pytest

pytest.importorskip("fastapi")
from tests.test_web_api import NEED, PACKS, env, pay, sign, signup  # noqa: E402,F401


def funded(c, gw):
    signup(c)
    sid, _ = pay(c, gw)
    c.post("/api/billing/confirm", json={"session_id": sid})


# ---- features -------------------------------------------------------------------------------------
def test_public_config(env):
    c, _ = env
    d = c.get("/api/config").json()
    assert d["credits_per_job"] == 1 and {p["id"] for p in d["packs"]} == set(PACKS)
    assert d["google_enabled"] is False and d["stripe_enabled"] is True


def test_frontend_root_responds(env):
    c, _ = env
    assert c.get("/").status_code == 200


@pytest.mark.parametrize("email", ["", "not-an-email", "a@b"])
def test_signup_rejects_bad_email(env, email):
    c, _ = env
    assert c.post("/api/auth/signup", json={"email": email, "password": "correct horse"}).status_code in (400, 422)


def test_login_unknown_user(env):
    c, _ = env
    assert c.post("/api/auth/login", json={"email": "ghost@example.com", "password": "whatever1"}).status_code == 401


@pytest.mark.parametrize("method,path", [
    ("get", "/api/billing/history"), ("post", "/api/billing/checkout"), ("get", "/api/jobs"),
    ("post", "/api/jobs"), ("get", "/api/jobs/x"), ("get", "/api/admin/overview")])
def test_routes_require_login(env, method, path):
    c, _ = env
    r = getattr(c, method)(path, json={}) if method == "post" else c.get(path)
    assert r.status_code in (401, 422)


def test_unknown_pack(env):
    c, _ = env
    signup(c)
    assert c.post("/api/billing/checkout", json={"pack": "nope"}).status_code == 400


def test_checkout_returns_url(env):
    c, _ = env
    signup(c)
    r = c.post("/api/billing/checkout", json={"pack": "starter"}).json()
    assert r["id"].startswith("cs_test_") and r["url"].startswith("https://")


def test_confirm_other_users_session_forbidden(env):
    c, gw = env
    signup(c, "a@example.com")
    sid, _ = pay(c, gw)
    c.post("/api/auth/logout")
    signup(c, "b@example.com")
    assert c.post("/api/billing/confirm", json={"session_id": sid}).status_code == 403
    assert c.get("/api/me").json()["credits"] == 0


def test_confirm_unknown_session(env):
    c, _ = env
    signup(c)
    assert c.post("/api/billing/confirm", json={"session_id": "cs_missing"}).status_code == 400


def test_webhook_unsigned_rejected(env):
    c, gw = env
    signup(c)
    _, body = pay(c, gw)
    assert c.post("/api/stripe/webhook", content=body).status_code == 400


def test_unknown_job_404(env):
    c, _ = env
    signup(c)
    assert c.get("/api/jobs/doesnotexist").status_code == 404
    assert c.post("/api/jobs/doesnotexist/approve", json={"rfq_ids": "all"}).status_code == 404


def test_quote_unknown_candidate(env):
    c, gw = env
    funded(c, gw)
    jid = c.post("/api/jobs", json={"need": NEED}).json()["job_id"]
    assert c.post(f"/api/jobs/{jid}/quote", json={"candidate_id": "nobody", "label": "SUPPLIER_QUOTE"}).status_code == 400


def test_get_job_charged_and_listed(env):
    c, gw = env
    funded(c, gw)
    jid = c.post("/api/jobs", json={"need": NEED}).json()["job_id"]
    d = c.get(f"/api/jobs/{jid}").json()
    assert d["meta"]["id"] == jid and d["output"]["status"] == "AWAITING_APPROVAL"
    assert [j["id"] for j in c.get("/api/jobs").json()] == [jid]


def test_non_admin_blocked_from_admin(env):
    c, _ = env
    u = signup(c)
    assert c.get("/api/admin/overview").status_code == 403
    assert c.post(f"/api/admin/users/{u['id']}/credits", json={"delta": 100}).status_code == 403
    assert c.get("/api/me").json()["credits"] == 0


# ---- benchmarks -----------------------------------------------------------------------------------
def _bench(fn, n):
    t = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t) / n * 1000  # ms/op


def test_bench_config_latency(env):
    c, _ = env
    ms = _bench(lambda: c.get("/api/config"), 200)
    print(f"\n/api/config: {ms:.2f} ms/op")
    assert ms < 50


def test_bench_me_latency(env):
    c, _ = env
    signup(c)
    ms = _bench(lambda: c.get("/api/me"), 200)
    print(f"\n/api/me: {ms:.2f} ms/op")
    assert ms < 50


def test_bench_signup_login(env):
    c, _ = env
    i = iter(range(10**6))
    ms = _bench(lambda: c.post("/api/auth/signup", json={"email": f"u{next(i)}@example.com", "password": "correct horse"}), 10)
    print(f"\nsignup: {ms:.2f} ms/op")
    assert ms < 1500  # password hashing is intentionally slow


def test_bench_job_run(env):
    c, gw = env
    funded(c, gw)
    ms = _bench(lambda: c.post("/api/jobs", json={"need": NEED}), 5)
    print(f"\njob run: {ms:.2f} ms/op")
    assert ms < 3000
