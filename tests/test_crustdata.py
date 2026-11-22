"""Crustdata adapter: mocked HTTP only (no key, no credits). Context never decides legal approval."""
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

RECORDS = {
    "navyug-telematics.example": {"basic_info": {"name": "Navyug Telematics", "primary_domain": "navyug-telematics.example", "year_founded": 2012},
                                  "locations": {"headquarters": "Pune, Maharashtra, India", "country": "India"}, "headcount": {"total": 180}},
    "fleetguru.example": {"basic_info": {"name": "FleetGuru Traders"}, "locations": {"country": "India"}, "headcount": {"total": 6}},
    "meghdoot-iot.example": {"basic_info": {"name": "Shadow Ventures Ltd"}, "locations": {"country": "India"}, "headcount": {"total": 40}},
}


def fake_post(calls):
    def post(body, key, timeout):
        calls.append({"body": body, "key": key})
        rows = [{"matched_on": d, "match_type": "domain",
                 "matches": [{"confidence_score": 0.98, "company_data": RECORDS[d]}] if d in RECORDS else []}
                for d in body.get("domains", [])]
        rows += [{"matched_on": n, "match_type": "name", "matches": []} for n in body.get("names", [])]
        return rows, None
    return post


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUSTDATA_API_KEY", FAKE_KEY)
    return Config.load(jobs_dir=tmp_path)


def run(cfg, calls, job_id="cd1"):
    job = Job(job_id, cfg, today=date(2026, 11, 22), crust_post=fake_post(calls))
    job.run(NEED)
    return job


def by_id(job, cid):
    return next(a for a in job.state.assessments if a.candidate_id == cid)


def test_one_batched_call_with_documented_shape(cfg):
    calls = []
    run(cfg, calls)
    domain_calls = [c for c in calls if "domains" in c["body"]]
    assert len(domain_calls) == 1 and domain_calls[0]["body"]["exact_match"] is True
    assert len(domain_calls[0]["body"]["domains"]) == 9  # all 9 candidates in ONE request (max 25)
    assert len(calls) == 1 and calls[0]["key"] == FAKE_KEY


def test_identity_corroborated_not_approval(cfg):
    a = by_id(run(cfg, []), "c01")
    assert "Corroborated by Crustdata" in a.gate("entity_identity").reason
    assert a.company_context["headcount"] == 180
    assert "Crustdata" not in a.gate("legal_approval").source  # registry stays the only approval evidence


def test_domain_owner_conflict_holds_for_review(cfg):
    a = by_id(run(cfg, []), "c08")
    assert any(f.startswith("Identity conflict") for f in a.flags)
    assert a.decision == Decision.HOLD_FOR_REVIEW


def test_trader_credibility_signal(cfg):
    a = by_id(run(cfg, []), "c07")
    assert any("Trader-credibility signal" in f for f in a.flags) and a.decision == Decision.NOT_A_FIT


def test_cache_means_second_job_pays_nothing(cfg):
    calls = []
    run(cfg, calls, "first")
    n = len(calls)
    job2 = run(cfg, calls, "second")
    assert len(calls) == n and to_output(job2)["cost"]["crustdata_credits"] == 0


def test_restart_does_not_repay(cfg):
    calls = []
    job = run(cfg, calls, "rs")
    n = len(calls)
    job.state.extra["done"].remove("assess")
    job.run()
    assert len(calls) == n


def test_ledger_logs_credits_and_never_the_key(cfg):
    job = run(cfg, [], "led")
    rows = job.store.ledger_rows()
    assert any(r["provider"] == "crustdata" and r["credits"] > 0 for r in rows)
    blob = job.store.ledger_path.read_text() + json.dumps(job.store.audit_rows()) + job.state.model_dump_json()
    assert FAKE_KEY not in blob


def test_no_key_no_call(tmp_path, monkeypatch):
    monkeypatch.delenv("CRUSTDATA_API_KEY", raising=False)
    calls = []
    job = run(Config.load(jobs_dir=tmp_path), calls)
    assert calls == [] and by_id(job, "c01").gate("entity_identity").value == GV.PASS
