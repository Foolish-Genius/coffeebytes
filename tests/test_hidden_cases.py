"""The 12 hidden-case tests from the MVP brief, plus contract tests. Ground truth = fixture registries."""
from __future__ import annotations

import copy
from datetime import date

import pytest

from siliconscout.config import Config
from siliconscout.models import Decision, GV, JobState, Role
from siliconscout.output import to_output, validate
from siliconscout.pipeline import Crash, Job

NEED = "Need GPS trackers for 300 trucks, deliver to Pune"
TODAY = date(2026, 11, 22)


@pytest.fixture
def cfg(tmp_path):
    return Config.load(jobs_dir=tmp_path)


def run(cfg, need=NEED, job_id="j1", **kw):
    job = Job(job_id, cfg, today=TODAY, **kw)
    job.run(need)
    return job


def by_id(job, cid):
    return next(a for a in job.state.assessments if a.candidate_id == cid)


def g(a, name):
    return a.gate(name).value


# 1. Approved maker
def test_approved_maker(cfg):
    a = by_id(run(cfg), "c01")
    assert g(a, "legal_approval") == GV.PASS and a.role == Role.MAKER
    src = a.gate("legal_approval").source
    assert "as on 2026-07-16" in src and "TAC-ARAI-F0001" in src
    assert a.decision == Decision.QUALIFIED_FOR_RFQ


def test_either_list_passes(cfg):  # ICAT-only listing passes
    a = by_id(run(cfg), "c08")
    assert g(a, "legal_approval") == GV.PASS and "ICAT" in a.gate("legal_approval").source


# 2. Brand, not maker
def test_brand_not_maker(cfg):
    a = by_id(run(cfg), "c06")
    assert a.role == Role.BRAND_OWNER
    assert a.maker_of_record == "Kavach Electronics Pvt Ltd" and a.company != a.maker_of_record


# 3. Trader posing as maker
def test_trader_posing_as_maker(cfg):
    a = by_id(run(cfg), "c07")
    assert a.role == Role.TRADER and g(a, "supplier_role") == GV.FAIL
    assert a.decision == Decision.NOT_A_FIT


def test_trader_kept_as_reseller_if_allowed(cfg):
    cfg.priorities = {**cfg.priorities, "buyer_requires_maker": False}
    a = by_id(run(cfg), "c07")
    assert g(a, "supplier_role") == GV.PASS  # kept as reseller; legal gate still FAILs the unlisted model
    assert a.decision == Decision.NOT_A_FIT


# 4. Lookalike names
def test_lookalike_names_kept_separate(cfg):
    job = run(cfg)
    real, fake = by_id(job, "c04"), by_id(job, "c05")
    assert g(real, "legal_approval") == GV.PASS
    # R3: one letter off the registered name with the same model is ambiguous -> HOLD, never PASS, never merged
    assert g(fake, "legal_approval") != GV.PASS and fake.decision == Decision.HOLD_FOR_REVIEW
    assert fake.maker_of_record is None and any(f.startswith("Ambiguous registry match") for f in fake.flags)


# R3: ambiguous model-number format -> HOLD_FOR_REVIEW, not FAIL; a clear absence still FAILs
def test_ambiguous_model_match_holds(cfg):
    from siliconscout.gates import assess
    from siliconscout.models import Candidate, Page
    from siliconscout.registry import Registries
    job = run(cfg, job_id="amb")
    spec = job.state.spec
    page = Page(url="https://navyug-telematics.example/x", text="Navyug Telematics Pvt Ltd makes the NVT-140 in Pune, India.")
    near = Candidate(candidate_id="z1", company="Navyug Telematics Pvt Ltd", model="NVT-140", pages=[page])
    far = Candidate(candidate_id="z2", company="Navyug Telematics Pvt Ltd", model="QX-9", pages=[page])
    a1 = assess(near, spec, cfg, Registries(cfg.data_dir), job.store, TODAY)
    a2 = assess(far, spec, cfg, Registries(cfg.data_dir), job.store, TODAY)
    assert a1.decision == Decision.HOLD_FOR_REVIEW and g(a1, "legal_approval") == GV.UNKNOWN
    assert "NVT-140X" in a1.gate("legal_approval").reason and a1.gate("legal_approval").date == "2026-07-16"
    assert a2.decision == Decision.NOT_A_FIT and g(a2, "legal_approval") == GV.FAIL


# 5. Expired approval
def test_expired_approval(cfg):
    a = by_id(run(cfg), "c03")
    assert g(a, "legal_approval") == GV.FAIL and "2025-06-30" in a.gate("legal_approval").reason
    assert a.decision == Decision.NOT_A_FIT


# 6. Wrong lane
def test_component_routed_to_component_lane(cfg):
    job = run(cfg, "Need 500 pcs of part number FSC-MCU100A for repairs", "j-d")
    assert job.state.spec.lane == "D_COMPONENT" and job.state.spec.mpn == "FSC-MCU100A"
    assert job.state.rfqs == [] and all(a.role in (Role.AUTHORIZED_DISTRIBUTOR, Role.TRADER) for a in job.state.assessments)
    broker = next(a for a in job.state.assessments if a.company == "Broker Beta")
    assert broker.decision == Decision.NOT_A_FIT  # cheapest but unauthorized: price never overrides a gate


def test_device_need_not_routed_to_components(cfg):
    assert run(cfg).state.spec.lane == "A_FLEET"


# 7. Verbatim check fails
def test_verbatim_check_fails(cfg):
    a = by_id(run(cfg), "c06")
    assert g(a, "spec:ip_rating") == GV.UNKNOWN
    assert any("Claim rejected (ip_rating)" in f for f in a.flags)


# 8. Capacity silent
def test_capacity_silent(cfg):
    a = by_id(run(cfg), "c01")
    assert a.decision == Decision.QUALIFIED_FOR_RFQ and g(a, "capacity") == GV.TO_CONFIRM


# 9. No quote yet
def test_no_quote_yet(cfg):
    job = run(cfg)
    job.approve("all")
    job.run()
    out = to_output(job)
    assert out["status"] == "RFQS_OUTSTANDING" and "RFQs outstanding" in out["decision_summary"]
    assert not any(a.decision == Decision.RECOMMEND_ORDER for a in job.state.assessments)


def test_real_quote_enables_order_and_unknowns_stay_unknown(cfg):
    job = run(cfg)
    job.approve("all")
    job.run()
    job.add_quote({"candidate_id": "c01", "label": "SUPPLIER_QUOTE", "unit_price": 7450, "quantity": 300,
                   "delivery_days": 21, "warranty_months": 24, "approval_ref": "TAC-ARAI-F0001"})
    assert by_id(job, "c01").decision == Decision.RECOMMEND_ORDER
    view = to_output(job)["candidates"][0]["quote"]
    assert view["goods_value"] == 2235000 and view["freight"] == "UNKNOWN" and view["gst_pct"] == "UNKNOWN"


def test_inflation_and_lead_time_flags_never_fail(cfg):  # R6 + inflation
    job = run(cfg, job_id="infl")
    job.add_quote({"candidate_id": "c01", "label": "SUPPLIER_QUOTE", "unit_price": 7450, "quantity": 300,
                   "delivery_days": 180, "validity_until": "2026-12-31", "received_at": "2026-11-22"})
    a = by_id(job, "c01")
    assert any(f.startswith("Long lead time: 180 days") for f in a.flags)
    assert any(f.startswith("Price-escalation risk") for f in a.flags)
    assert a.decision == Decision.RECOMMEND_ORDER  # a signal for the buyer, never a failure gate
    job.add_quote({"candidate_id": "c01", "label": "SUPPLIER_QUOTE", "unit_price": 7450, "quantity": 300,
                   "delivery_days": 180, "validity_until": "2026-12-31", "received_at": "2026-11-22",
                   "escalation_clause": "fixed to delivery"})
    assert not any(f.startswith("Price-escalation risk") for f in by_id(job, "c01").flags)


def test_et_compliance_checks_in_spec_and_rfq(cfg):
    job = run(cfg, job_id="comp")
    assert any("AIS-140" in c for c in job.state.spec.compliance_checks)
    assert any("WPC" in c for c in job.state.spec.compliance_checks)
    assert "compliance documents" in job.state.rfqs[0].body and "escalation clause" in job.state.rfqs[0].body


def test_budgetary_price_never_supports_order(cfg):
    job = run(cfg)
    job.add_quote({"candidate_id": "c01", "label": "PUBLIC_BUDGETARY", "unit_price": 5000, "quantity": 300})
    assert by_id(job, "c01").decision == Decision.QUALIFIED_FOR_RFQ


def test_unquoted_candidates_tie(cfg):
    job = run(cfg)
    assert "c04" in by_id(job, "c01").tied_with


# 10. Prompt injection
def test_prompt_injection_ignored_and_logged(cfg):
    job = run(cfg)
    a = by_id(job, "c08")
    assert any("Prompt injection ignored" in f for f in a.flags)
    assert a.decision == Decision.QUALIFIED_FOR_RFQ and a.rank > by_id(job, "c01").rank
    assert any(r["event"] == "prompt_injection_ignored" for r in job.store.audit_rows())


# 11. Kill after RFQ send
def test_kill_after_rfq_send_no_duplicate(cfg):
    sent = []
    job = Job("jk", cfg, today=TODAY, sender=sent.append, crash_at="mid_send")
    job.run(NEED)
    job.approve("all")
    with pytest.raises(Crash):
        job.run()
    assert len(sent) == 1
    job2 = Job("jk", cfg, today=TODAY, sender=sent.append)  # fresh process
    job2.run()
    keys = [p["key"] for p in sent]
    assert len(keys) == len(set(keys)) == len(job2.state.rfqs)
    assert all(r.status == "SENT" for r in job2.state.rfqs)


def test_ambiguous_send_is_held_for_person(cfg):
    def dies(_):
        raise Crash("during send")
    job = Job("ja", cfg, today=TODAY, sender=dies)
    job.run(NEED)
    job.approve("all")
    with pytest.raises(Crash):
        job.run()
    sent = []
    job2 = Job("ja", cfg, today=TODAY, sender=sent.append)
    job2.run()
    assert job2.state.rfqs[0].status == "HELD_AMBIGUOUS" and job2.state.rfqs[0].rfq_id not in [p["rfq_id"] for p in sent]
    assert to_output(job2)["status"] == "HELD_FOR_PERSON"


def test_resume_from_checkpoint_skips_done_stages(cfg):
    with pytest.raises(Crash):
        Job("jr", cfg, today=TODAY, crash_at="discover").run(NEED)
    job = Job("jr", cfg, today=TODAY)
    job.run()
    stages = [r["detail"]["stage"] for r in job.store.audit_rows() if r["event"] == "stage_done"]
    assert stages.count("discover") == 1 and stages.count("spec") == 1


# 12. Hour-3 style change
def test_hour3_new_field_in_config(cfg):
    old = run(cfg, job_id="old")
    snapshot = old.state.model_dump_json()
    cfg2 = copy.deepcopy(cfg)
    cfg2.lanes["A_FLEET"]["required_fields"]["ota_firmware"] = {"label": "OTA firmware updates", "patterns": ["\\bota\\b", "over-the-air"]}
    new = run(cfg2, job_id="new")
    out = to_output(new)
    assert validate(out, cfg2.output_schema) == []
    assert g(by_id(new, "c01"), "spec:ota_firmware") == GV.UNKNOWN
    assert JobState.model_validate_json(snapshot).job_id == "old"  # old snapshot still readable


def test_old_snapshot_with_extra_and_missing_fields_reads(cfg):
    st = JobState.model_validate({"job_id": "x", "status": "NEW", "future_field": 1})
    assert st.job_id == "x"


def test_output_contract(cfg):
    for need, jid in [(NEED, "c1"), ("Need 500 pcs of part number FSC-MCU100A", "c2"), ("Need 40 smart coffee machines", "c3")]:
        job = run(cfg, need, jid)
        assert validate(to_output(job), cfg.output_schema) == [], need


def test_generic_lane_fallback(cfg):
    job = run(cfg, "Need 40 smart coffee machines for the Delhi office", "g1")
    assert job.state.spec.lane == "GENERIC"
    assert any("no government registry" in s.lower() for s in job.state.spec.assumptions)


def test_authorized_distributor_in_device_lane(cfg):  # R2
    from siliconscout.gates import assess
    from siliconscout.models import Candidate, Page, Role
    from siliconscout.registry import Registries
    job = run(cfg, job_id="ad")
    page = Page(url="https://fleetline.example/", fetched_at="2026-11-22",
                text="Fleetline Systems Pvt Ltd supplies the NVT-140X across India as an authorised partner of Navyug Telematics Pvt Ltd.")
    a = assess(Candidate(candidate_id="d1", company="Fleetline Systems Pvt Ltd", model="NVT-140X", pages=[page]),
               job.state.spec, cfg, Registries(cfg.data_dir), job.store, TODAY)
    assert a.role == Role.AUTHORIZED_DISTRIBUTOR and a.maker_of_record == "Navyug Telematics Pvt Ltd"
    assert g(a, "legal_approval") == GV.PASS and g(a, "supplier_role") == GV.PASS
    # an unknown reseller of the same model is NOT an authorized distributor
    b = assess(Candidate(candidate_id="d2", company="Random Resellers", model="NVT-140X",
                         pages=[Page(url="https://rr.example/", text="We resell NVT-140X trackers in India.")]),
               job.state.spec, cfg, Registries(cfg.data_dir), job.store, TODAY)
    assert b.role != Role.AUTHORIZED_DISTRIBUTOR and b.decision in (Decision.NOT_A_FIT, Decision.HOLD_FOR_REVIEW)


def test_every_pass_has_source_passage_and_date(cfg):  # R4
    from siliconscout.evidence import normalize
    job = run(cfg, job_id="r4")
    pages = {p.url: p.text for c in job.state.candidates for p in c.pages}
    checked = 0
    for a in job.state.assessments:
        for gr in a.gates:
            if gr.value != GV.PASS or gr.source.startswith("derived"):
                continue
            assert gr.source and gr.date, (a.company, gr.gate)
            assert gr.passage, (a.company, gr.gate)
            if gr.source in pages:  # page-backed passages must be verbatim in the stored page
                assert normalize(gr.passage) in normalize(pages[gr.source]), (a.company, gr.gate)
            checked += 1
    assert checked > 30


def test_live_mode_never_uses_test_fixtures(cfg):  # R5
    cfg.limits = {**cfg.limits, "mode": "live"}
    with pytest.raises(ValueError, match="refuses fixture candidates"):
        run(cfg, job_id="live1")
    # fixture registry can't settle approval in a live run -> CANNOT_VERIFY, never PASS
    from siliconscout.gates import assess
    from siliconscout.models import Candidate
    from siliconscout.registry import Registries
    demo = run(Config.load(jobs_dir=cfg.jobs_dir), job_id="spec-only")
    a = assess(Candidate(candidate_id="x", company="Navyug Telematics Pvt Ltd", model="NVT-140X"),
               demo.state.spec, cfg, Registries(cfg.data_dir), demo.store, TODAY)
    assert g(a, "legal_approval") == GV.UNKNOWN and a.decision == Decision.CANNOT_VERIFY
    parts = Job("live3", cfg, today=TODAY)
    parts.run("Need 500 pcs of part number FSC-MCU100A")
    assert parts.state.assessments == [] and to_output(parts)["status"] == "CANNOT_VERIFY"
    parts.state.assessments = [by_id(demo, "c01")]
    with pytest.raises(ValueError, match="TEST_FIXTURE"):
        parts.add_quote({"candidate_id": "c01", "label": "TEST_FIXTURE", "unit_price": 1})


def test_no_paid_calls_on_deterministic_path(cfg):
    job = run(cfg)
    assert to_output(job)["cost"]["paid_calls"] == 0
