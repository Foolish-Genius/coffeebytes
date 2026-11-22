"""Vague needs get clarifying questions before any search; follow-ups change the spec or explain results."""
from datetime import date

import pytest

from siliconscout.config import Config
from siliconscout.models import Decision, GV
from siliconscout.output import to_output, validate
from siliconscout.pipeline import Job

TODAY = date(2026, 11, 22)


@pytest.fixture
def cfg(tmp_path):
    return Config.load(jobs_dir=tmp_path)


def job(cfg, need, jid="f1"):
    j = Job(jid, cfg, today=TODAY)
    j.run(need)
    return j


def by_id(j, cid):
    return next(a for a in j.state.assessments if a.candidate_id == cid)


def test_vague_need_asks_before_search(cfg):
    j = job(cfg, "we need some devices for our operations")
    out = to_output(j)
    assert out["status"] == "NEEDS_CLARIFICATION" and len(out["questions"]) == 2
    assert j.state.candidates == [] and "discover" not in j.state.extra.get("done", [])
    assert validate(out, cfg.output_schema) == []


def test_answers_unblock_and_run(cfg):
    j = job(cfg, "we need some devices for our operations")
    j.follow_up("vehicle tracking for our trucks")
    assert j.state.spec.lane == "A_FLEET" and to_output(j)["status"] == "NEEDS_CLARIFICATION"  # qty still missing
    reply = j.follow_up("300 trucks")
    assert j.state.spec.quantity == 300 and to_output(j)["status"] == "AWAITING_APPROVAL" and "Updated" in reply


def test_proceed_uses_assumptions(cfg):
    j = job(cfg, "Need GPS trackers, deliver to Pune")  # no quantity
    assert to_output(j)["status"] == "NEEDS_CLARIFICATION"
    j.follow_up("proceed")
    assert to_output(j)["status"] == "AWAITING_APPROVAL"


def test_require_ip67_tightens_and_rechecks(cfg):
    j = job(cfg, "Need GPS trackers for 300 trucks, deliver to Pune")
    assert by_id(j, "c04").decision == Decision.QUALIFIED_FOR_RFQ  # IP66 met the IP65+ rule
    j.follow_up("require IP67")
    assert by_id(j, "c01").gate("spec:ip_rating").value == GV.PASS  # IP67
    assert by_id(j, "c04").gate("spec:ip_rating").value == GV.UNKNOWN  # IP66 no longer evidences IP67
    assert by_id(j, "c04").decision == Decision.PROVISIONAL


def test_quantity_location_and_explain(cfg):
    j = job(cfg, "Need GPS trackers for 300 trucks, deliver to Pune")
    j.follow_up("make it 500 units")
    j.follow_up("deliver to Chennai")
    assert j.state.spec.quantity == 500 and j.state.spec.delivery_location == "Chennai"
    assert "500 x" in j.state.rfqs[0].body or "500" in j.state.rfqs[0].subject
    ans = j.follow_up("why Sarathi?")
    assert "NOT_A_FIT" in ans and "2025-06-30" in ans
    assert len(to_output(j)["thread"]) == 6


def test_no_spec_change_after_rfqs_sent(cfg):
    j = job(cfg, "Need GPS trackers for 300 trucks, deliver to Pune")
    j.approve("all")
    j.run()
    assert "already been sent" in j.follow_up("make it 500 units") and j.state.spec.quantity == 300


def test_unparsed_followup_no_llm_is_honest(cfg):
    j = job(cfg, "Need GPS trackers for 300 trucks, deliver to Pune")
    assert "couldn't map" in j.follow_up("hmm what about the weather")
