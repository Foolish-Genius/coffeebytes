"""Crustdata adapter: mocked HTTP only (fake key, zero credits). Spend is locked by default."""
from __future__ import annotations

import json
from datetime import date

import pytest

from siliconscout.config import Config
from siliconscout.models import Decision, GV
from siliconscout.output import to_output
from siliconscout.pipeline import Job

NEED = "Need GPS trackers for 300 trucks, deliver to Pune"
FAKE_KEY = "test-not-a-real-key-123"

RECORDS = {  # identify-shaped: basic_info only, size as a range (per docs)
    "navyug-telematics.example": {"crustdata_company_id": 1, "basic_info": {"name": "Navyug Telematics", "primary_domain": "navyug-telematics.example", "employee_count_range": "51-200"}},
    "fleetguru.example": {"crustdata_company_id": 2, "basic_info": {"name": "FleetGuru Traders", "employee_count_range": "1-10"}},
    "meghdoot-iot.example": {"crustdata_company_id": 3, "basic_info": {"name": "Shadow Ventures Ltd", "employee_count_range": "11-50"}},
}


class FakeAPI:
    def __init__(self, balance=1000.0, status=200, first_status=None):
        self.calls, self.balance, self.status, self.first_status = [], balance, status, first_status

    def __call__(self, method, path, body, key, timeout):
        self.calls.append({"method": method, "path": path, "body": body, "key": key})
        if path == "/account/credits":
            return 200, {"account": {"credits": self.balance}}, {}
        if self.first_status and len([c for c in self.calls if c["path"] != "/account/credits"]) == 1:
            return self.first_status, {"error": {"type": "rate_limit_error"}}, {"x-ratelimit-reset": "0"}
        if self.status != 200:
            return self.status, {"error": {"type": "insufficient_credits"}}, {}
        rows = [{"matched_on": d, "match_type": "domain",
                 "matches": [{"confidence_score": 1.0, "company_data": RECORDS[d]}] if d in RECORDS else []}
                for d in body.get("domains", [])]
        return 200, rows, {"x-credits-used": str(0.2 * sum(bool(r["matches"]) for r in rows))}

    def spend_calls(self):
        return [c for c in self.calls if c["path"] != "/account/credits"]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUSTDATA_API_KEY", FAKE_KEY)
    c = Config.load(jobs_dir=tmp_path)
    c.limits["crustdata"]["max_credits_per_job"] = 5
    c.limits["crustdata"]["skip_tlds"] = []  # fixture domains are .example; let the mock treat them as real
    return c


def test_reserved_domains_go_by_name_and_400_drops_bad_domains(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUSTDATA_API_KEY", FAKE_KEY)
    c = Config.load(jobs_dir=tmp_path)
    api = FakeAPI()
    run(c, api, "names", spend=True)  # default skip_tlds: .example -> names lookup, never rejected
    body = api.spend_calls()[0]["body"]
    assert "names" in body and "domains" not in body and body["exact_match"] is True

    class Reject(FakeAPI):
        def __call__(self, method, path, body, key, timeout):
            if path != "/account/credits" and "bad.example" in (body.get("domains") or []):
                self.calls.append({"method": method, "path": path, "body": body, "key": key})
                return 400, {"error": {"type": "invalid_request", "message": "Invalid company domains: bad.example"}}, {}
            return super().__call__(method, path, body, key, timeout)
    from siliconscout.crustdata import Crustdata
    from siliconscout.models import Candidate
    from siliconscout.store import Store
    api2 = Reject()
    cd = Crustdata({"crustdata": {"allow_spend": True, "skip_tlds": [], "max_credits_per_job": 5}},
                   Store(tmp_path, "r"), tmp_path / "_c", http=api2)
    out = cd.enrich([Candidate(candidate_id="a", company="Navyug", domain="navyug-telematics.example"),
                     Candidate(candidate_id="b", company="Bad", domain="bad.example")])
    assert len(api2.spend_calls()) == 2 and api2.spend_calls()[1]["body"]["domains"] == ["navyug-telematics.example"]
    assert out["a"]["matched"] and cd.last_plan["result"]["ok_requests"] == 1


def run(cfg, api, job_id="cd1", spend=None):
    job = Job(job_id, cfg, today=date(2026, 11, 22), crust_http=api, crust_spend=spend)
    job.run(NEED)
    return job


def by_id(job, cid):
    return next(a for a in job.state.assessments if a.candidate_id == cid)


# --- the lock -------------------------------------------------------------------------------------
def test_spend_lock_is_dry_run_zero_network_calls(cfg):
    api = FakeAPI()
    job = run(cfg, api, spend=False)  # allow_spend: false
    assert api.calls == []
    plan = to_output(job)["crustdata_plan"]
    assert plan["to_send"] == 9 and plan["worst_case_credits"] == pytest.approx(1.8) and plan["spent"] is False
    assert plan["endpoint"] == "POST /company/identify"
    assert any(r["event"] == "crustdata_dry_run" for r in job.store.audit_rows())
    assert to_output(job)["cost"]["crustdata_credits"] == 0


def test_shipped_config_budget_is_1000_and_10_percent():
    c = Config.load().limits["crustdata"]
    assert c["allow_spend"] is True and c["max_total_credits"] == 1000 and c["max_fraction_of_balance"] == 0.10


def test_global_budget_trims_across_jobs(cfg):
    cfg.limits["crustdata"].update(max_total_credits=0.8, max_fraction_of_balance=None)  # room for 4 matches
    api = FakeAPI()
    job1 = run(cfg, api, "b1", spend=True)
    # worst case 9 x 0.2 = 1.8 > 0.8 -> only 4 identifiers sent; of those only navyug matches -> 0.2 spent
    assert len(api.spend_calls()[0]["body"]["domains"]) == 4
    assert any(r["event"] == "crustdata_budget" and r["detail"]["remaining"] == pytest.approx(0.8)
               for r in job1.store.audit_rows())
    (cfg.jobs_dir / "_cache" / "crustdata_cache.json").unlink()  # force a fresh lookup in the next job
    job2 = run(cfg, api, "b2", spend=True)
    assert len(api.spend_calls()[1]["body"]["domains"]) == 3  # 0.6 left -> 3 identifiers
    assert any(r["event"] == "crustdata_budget" and r["detail"]["remaining"] == pytest.approx(0.6)
               for r in job2.store.audit_rows())


def test_real_balance_drop_counts_against_budget(cfg):
    cfg.limits["crustdata"].update(max_total_credits=1000, max_fraction_of_balance=0.10)
    (cfg.jobs_dir / "_cache").mkdir(parents=True, exist_ok=True)
    (cfg.jobs_dir / "_cache" / "crustdata_budget.json").write_text(json.dumps({"baseline": 15000, "spent": 0}))
    api = FakeAPI(balance=13500)  # 1,500 already gone (e.g. used elsewhere) > 10% of 15,000
    job = run(cfg, api, "bal", spend=True)
    assert api.spend_calls() == []
    assert any(r["event"] == "crustdata_spend_blocked" for r in job.store.audit_rows())


def test_budget_limit_is_min_of_total_and_fraction(cfg):
    cfg.limits["crustdata"].update(max_total_credits=1000, max_fraction_of_balance=0.10)
    job = run(cfg, FakeAPI(balance=15000), "lim", spend=True)
    ev = next(r for r in job.store.audit_rows() if r["event"] == "crustdata_budget")
    assert ev["detail"]["limit"] == 1000  # min(1000, 0.10 x 15000 = 1500)


def test_cap_limits_what_is_sent(cfg):
    cfg.limits["crustdata"]["max_credits_per_job"] = 0.6  # 3 identify matches
    api = FakeAPI()
    job = run(cfg, api, spend=True)
    assert len(api.spend_calls()[0]["body"]["domains"]) == 3
    assert job.state.extra["crustdata_plan"]["skipped_over_cap"] == 6


def test_low_balance_blocks_spend(cfg):
    api = FakeAPI(balance=10)  # < estimate + 50 reserve
    job = run(cfg, api, spend=True)
    assert api.spend_calls() == [] and api.calls[0]["path"] == "/account/credits"
    assert any(r["event"] == "crustdata_spend_blocked" for r in job.store.audit_rows())


# --- when spend is approved (mocked) --------------------------------------------------------------
def test_identify_one_batched_call_documented_shape(cfg):
    api = FakeAPI()
    run(cfg, api, spend=True)
    calls = api.spend_calls()
    assert len(calls) == 1 and calls[0]["path"] == "/company/identify" and calls[0]["method"] == "POST"
    assert calls[0]["body"]["exact_match"] is True and len(calls[0]["body"]["domains"]) == 9
    assert "fields" not in calls[0]["body"]


def test_ledger_uses_credits_header_and_never_the_key(cfg):
    job = run(cfg, FakeAPI(), "led", spend=True)
    rows = [r for r in job.store.ledger_rows() if r["provider"] == "crustdata"]
    assert rows and rows[0]["credits"] == pytest.approx(0.6) and rows[0]["matched"] == 3
    blob = job.store.ledger_path.read_text() + json.dumps(job.store.audit_rows()) + job.state.model_dump_json()
    assert FAKE_KEY not in blob


def test_402_stops_without_retry(cfg):
    api = FakeAPI(status=402)
    job = run(cfg, api, spend=True)
    assert len(api.spend_calls()) == 1
    assert any(r["event"] == "crustdata_http_error" and r["detail"]["status"] == 402 for r in job.store.audit_rows())


def test_429_retries_once(cfg):
    api = FakeAPI(first_status=429)
    job = run(cfg, api, spend=True)
    assert len(api.spend_calls()) == 2 and by_id(job, "c01").company_context.get("matched")


def test_cache_means_second_job_pays_nothing(cfg):
    api = FakeAPI()
    run(cfg, api, "first", spend=True)
    n = len(api.spend_calls())
    job2 = run(cfg, api, "second", spend=True)
    assert len(api.spend_calls()) == n and to_output(job2)["cost"]["crustdata_credits"] == 0


def test_context_command_upgrades_dry_run(cfg):
    api = FakeAPI()
    job = run(cfg, api, "up", spend=False)
    assert api.calls == []
    job2 = Job("up", cfg, today=date(2026, 11, 22), crust_http=api, crust_spend=True)
    job2.refresh_context()
    assert len(api.spend_calls()) == 1 and by_id(job2, "c01").company_context["name"] == "Navyug Telematics"


# --- how context is used (never approval evidence) ------------------------------------------------
def test_identity_corroborated_not_approval(cfg):
    a = by_id(run(cfg, FakeAPI(), spend=True), "c01")
    assert "Corroborated by Crustdata" in a.gate("entity_identity").reason
    assert "Crustdata" not in a.gate("legal_approval").source


def test_domain_owner_conflict_holds_for_review(cfg):
    a = by_id(run(cfg, FakeAPI(), spend=True), "c08")
    assert any(f.startswith("Identity conflict") for f in a.flags) and a.decision == Decision.HOLD_FOR_REVIEW


def test_trader_signal_from_size_range(cfg):
    a = by_id(run(cfg, FakeAPI(), spend=True), "c07")
    assert any("Trader-credibility signal: <= 10 employees (1-10)" in f for f in a.flags)


def test_time_limit_skips_paid_calls_but_job_finishes(cfg):  # R9
    cfg.limits = {**cfg.limits, "max_job_seconds": 0}
    api = FakeAPI()
    job = run(cfg, api, "slow", spend=True)
    assert api.calls == []
    assert any(r["event"] == "time_limit_skipped" and r["detail"]["op"] == "crustdata" for r in job.store.audit_rows())
    assert to_output(job)["status"] == "AWAITING_APPROVAL"  # deterministic pipeline still completes


def test_no_key_no_call(tmp_path, monkeypatch):
    monkeypatch.delenv("CRUSTDATA_API_KEY", raising=False)
    api = FakeAPI()
    job = run(Config.load(jobs_dir=tmp_path), api, spend=True)
    assert api.calls == [] and by_id(job, "c01").gate("entity_identity").value == GV.PASS
