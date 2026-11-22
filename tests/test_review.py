"""Review-pass tests: follow-up parsing, near-match, restart behaviour, live-mode refusal."""
import time
from datetime import date

import pytest

from siliconscout.config import Config
from siliconscout.followup import parse
from siliconscout.gates import _near_match
from siliconscout.models import Candidate
from siliconscout.output import to_output
from siliconscout.pipeline import Crash, Job

TODAY = date(2026, 11, 22)
NEED = "Need GPS trackers for 300 trucks, deliver to Pune"


@pytest.fixture
def cfg(tmp_path):
    return Config.load(jobs_dir=tmp_path)


class _Snap:
    def __init__(self, rows):
        self.rows = rows

    def hit(self, r):
        return r


def _near(model_a, model_b):
    c = Candidate(candidate_id="x", company="Orbitrack", domain="o.com", model=model_a)
    return bool(_near_match(c, [_Snap([{"company": "Orbitrack", "model": model_b}])]))


def test_parse_why_is_explain_not_quantity():
    assert parse("why 300 trucks?")[0] == "explain"


def test_parse_plain_quantity_forms():
    assert parse("make it 500") == ("set_quantity", "500")
    assert parse("qty 1,200") == ("set_quantity", "1200")
    assert parse("500") == ("set_quantity", "500")


def test_parse_location_simple():
    assert parse("deliver to Chennai") == ("set_location", "Chennai")


def test_parse_skip_is_proceed():
    assert parse("Skip") == ("proceed", "")
def test_parse_require_quantity_is_quantity():
    assert parse("require 500 units")[0] == "set_quantity"
def test_parse_location_stops_at_by_clause():
    assert parse("deliver to Pune by March") == ("set_location", "Pune")
def test_parse_location_stops_at_in_clause():
    assert parse("ship to Navi Mumbai in 3 days") == ("set_location", "Navi Mumbai")
def test_parse_need_with_quantity_is_quantity():
    assert parse("we need 300 trucks near Delhi")[0] == "set_quantity"
def test_parse_proceed_with_period():
    assert parse("proceed.")[0] == "proceed"
def test_parse_question_not_quantity():
    assert parse("what is the price of 300 units?")[0] != "set_quantity"


def test_near_match_genuine_typo_holds():
    assert _near("NVT-140X", "NVT-140")
def test_near_match_distinct_models_not_near():
    assert not _near("NVT-140X", "NVT-240X")


def test_quantity_change_redrafts_rfqs(cfg):
    j = Job("q1", cfg, today=TODAY)
    j.run(NEED)
    assert j.state.rfqs and all("300" in r.body for r in j.state.rfqs)
    j.follow_up("make it 500")
    assert j.state.spec.quantity == 500
    assert j.state.rfqs and all("500" in r.body and "300" not in r.body for r in j.state.rfqs)


def test_followup_after_restart(cfg):
    j = Job("r1", cfg, today=TODAY)
    j.run(NEED)
    j2 = Job("r1", cfg, today=TODAY)  # fresh process, state from checkpoint
    j2.follow_up("make it 450")
    assert j2.state.spec.quantity == 450 and to_output(j2)["status"] != "NEEDS_CLARIFICATION"


def test_live_mode_refuses_fixture(cfg):
    cfg.limits["mode"] = "live"
    j = Job("l1", cfg, today=TODAY)
    with pytest.raises(ValueError):
        j.run(NEED, source="fixture")
def test_followup_after_long_gap_has_live_deadline(cfg):
    j = Job("d1", cfg, today=TODAY)
    j.run(NEED)
    j.state.extra["started_at"] = time.time() - 5000
    j.store.checkpoint(j.state)
    j2 = Job("d1", cfg, today=TODAY)
    assert j2.deadline > time.time()
