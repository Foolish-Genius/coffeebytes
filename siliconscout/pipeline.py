"""One state machine, two lanes. Checkpoint after every stage; external sends go through the outbox."""
from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from . import rfq as R
from .adapters import discover, distributor_offers
from .config import Config
from .crustdata import Crustdata
from .followup import clarify, explain, llm_parse, parse, requirement_field
from .gates import apply_quote, assess, rank
from .llm import LLM
from .models import Assessment, Decision, JobState, Quote
from .registry import Registries
from .spec import translate
from .store import Store

DEVICE_STAGES = ["spec", "discover", "assess", "rfq_draft", "await_approval", "rfq_send", "decide"]
COMPONENT_STAGES = ["spec", "distributor", "decide"]


class Crash(Exception):
    """Raised by tests (or --kill-after) to simulate a process kill."""


def file_sender(store: Store) -> Callable[[dict], None]:
    def send(p: dict) -> None:
        (store.dir / "outbox" / f"{p['rfq_id']}.eml").write_text(
            f"To: {p['to']}\nSubject: {p['subject']}\nX-Idempotency-Key: {p['key']}\n\n{p['body']}", encoding="utf-8")
    return send


class Job:
    def __init__(self, job_id: str, cfg: Optional[Config] = None, today: Optional[date] = None,
                 sender: Optional[Callable[[dict], None]] = None, crash_at: Optional[str] = None,
                 crust_http: Optional[Callable] = None, crust_spend: Optional[bool] = None):
        self.cfg = cfg or Config.load()
        self.store = Store(self.cfg.jobs_dir, job_id)
        self.regs = Registries(self.cfg.data_dir)
        self.llm = LLM(self.cfg.limits, self.store)
        self.crust = Crustdata(self.cfg.limits, self.store, self.cfg.jobs_dir / "_cache", http=crust_http,
                               allow_spend=crust_spend)
        self.today = today or date.today()
        self.sender = sender or file_sender(self.store)
        self.crash_at = crash_at or os.environ.get("SS_KILL_AFTER")
        self.state = self.store.latest() or JobState(job_id=job_id)
        self.state.extra["started_at"] = time.time()  # time limit applies per run/turn, so follow-ups get a fresh budget
        self.deadline = self.state.extra["started_at"] + float(self.cfg.limits.get("max_job_seconds", 900))
        self.llm.deadline = self.crust.deadline = self.deadline

    # -- helpers -------------------------------------------------------------------------------
    def _done(self, stage: str) -> bool:
        return stage in self.state.extra.get("done", [])

    def _commit(self, stage: str, status: Optional[str] = None) -> None:
        self.state.stage = stage
        self.state.extra.setdefault("done", []).append(stage)
        if status:
            self.state.status = status
        self.store.checkpoint(self.state)
        self.store.audit("stage_done", stage=stage)
        if self.crash_at == stage:
            raise Crash(stage)

    def _llm(self) -> Optional[LLM]:
        return self.llm if self.llm.available() else None

    # -- run -----------------------------------------------------------------------------------
    def run(self, need: Optional[str] = None, source: str = "fixture", candidates_file: Optional[Path] = None) -> JobState:
        st = self.state
        if not self._done("spec"):
            if not need:
                raise ValueError("A new job needs the operating need text.")
            st.spec = translate(need, self.cfg, self._llm())
            self.store.audit("spec", lane=st.spec.lane, spec=st.spec.model_dump())
            self._commit("spec", "RUNNING")
        if not st.extra.get("clarified"):  # vague need: ask before any search
            qs = clarify(st.spec)
            if qs:
                st.extra["questions"], st.status = qs, "NEEDS_CLARIFICATION"
                self.store.checkpoint(st)
                return st
            st.extra["clarified"], st.extra["questions"] = True, []
        stages = COMPONENT_STAGES if st.spec.lane == "D_COMPONENT" else DEVICE_STAGES
        for stage in stages:
            if self._done(stage):
                continue
            if not getattr(self, f"_s_{stage}")(source, candidates_file):
                return st  # paused (awaiting a human)
        return st

    def _s_discover(self, source, cfile) -> bool:
        self.state.candidates = discover(self.state.spec, self.cfg, self.store, source, cfile, deadline=self.deadline)
        self._commit("discover")
        return True

    def _s_assess(self, *_) -> bool:
        llm = self._llm()
        if "crustdata" not in self.state.extra:  # spend-locked by default; once per job, checkpointed, cached
            self._fetch_context()
        self._reassess(llm)
        self._commit("assess")
        return True

    def _fetch_context(self) -> None:
        self.state.extra["crustdata"] = self.crust.enrich(self.state.candidates)
        self.state.extra["crustdata_plan"] = {k: v for k, v in self.crust.last_plan.items() if k != "batches"}
        res = self.crust.last_plan.get("result") or {}
        self.state.extra["crustdata_plan"]["spent"] = bool(res.get("ok_requests"))
        self.store.checkpoint(self.state)

    def _reassess(self, llm=None) -> None:
        ctx = self.state.extra.get("crustdata", {})
        extra = self.state.extra.get("fields") or {}
        self.state.assessments = rank([assess(c, self.state.spec, self.cfg, self.regs, self.store, self.today, llm,
                                              ctx.get(c.candidate_id), extra) for c in self.state.candidates], self.cfg)

    # -- follow-up turns ---------------------------------------------------------------------------
    def follow_up(self, text: str) -> str:
        """Answer a clarifying question, change the spec, or explain a result. Re-runs only what changed."""
        st = self.state
        action, value = parse(text)
        if action == "other":
            action, value = llm_parse(text, self._llm()) or ("other", text)
        thread = st.extra.setdefault("thread", [])
        thread.append({"role": "user", "text": text})
        if action == "explain":
            reply = explain(text, st.assessments)
        elif action == "other":
            reply = ("I couldn't map that. Try: '500 units', 'deliver to Chennai', 'require IP67', "
                     "'why Orbitrack?', or 'proceed'.")
        elif any(r.status == "SENT" for r in st.rfqs) and action != "proceed":
            reply = "RFQs have already been sent for this spec; start a new job to change it."
        else:
            reply = self._apply(action, value)
        thread.append({"role": "assistant", "text": reply})
        self.store.audit("follow_up", action=action, value=value)
        self.store.checkpoint(st)
        return reply

    def _apply(self, action: str, value: str) -> str:
        st, sp = self.state, self.state.spec
        rediscover = False
        if action == "set_quantity":
            sp.quantity = int(value)
            sp.assumptions.append(f"Quantity set to {value} by follow-up.")
        elif action == "set_location":
            sp.delivery_location = value
            sp.assumptions = [a for a in sp.assumptions if not a.startswith("Delivery location not stated")]
        elif action == "add_requirement":
            key, fdef = requirement_field(value, self.cfg.lanes[sp.lane].get("required_fields", {}))
            st.extra.setdefault("fields", {})[key] = fdef
            sp.requirements[key] = fdef["label"]
        elif action == "set_purpose":
            old = sp
            st.spec = translate(f"{old.raw_need} ({value})", self.cfg, None)
            st.spec.quantity = st.spec.quantity or old.quantity
            st.spec.delivery_location = st.spec.delivery_location or old.delivery_location
            rediscover = True
        elif action == "proceed":
            st.extra["clarified"] = True
        qs = clarify(st.spec)
        if qs and action != "proceed":
            st.extra["questions"], st.status = qs, "NEEDS_CLARIFICATION"
            return "Updated. Still need: " + " ".join(qs)
        st.extra["clarified"], st.extra["questions"] = True, []
        keep = {"spec"} if rediscover else {"spec", "discover"}
        st.extra["done"] = [d for d in st.extra.get("done", []) if d in keep]
        st.rfqs = [r for r in st.rfqs if r.status == "SENT"]
        st.status = "RUNNING"
        self.run()
        return f"Updated ({action.replace('_', ' ')}). " + summary(st)[1]

    def refresh_context(self) -> dict:
        """Re-plan (and, only if spend is allowed, fetch) Crustdata context, then re-assess. RFQs untouched."""
        self._fetch_context()
        if self._done("assess"):
            quotes = {q.candidate_id: q for q in self.state.quotes}
            self._reassess(self._llm())
            for a in self.state.assessments:
                if a.candidate_id in quotes:
                    apply_quote(a, quotes[a.candidate_id], self.state.spec, self.cfg)
            self.state.assessments = rank(self.state.assessments, self.cfg)
            self.store.checkpoint(self.state)
        return self.crust.last_plan

    def _s_rfq_draft(self, *_) -> bool:
        emails = {c.candidate_id: c.email for c in self.state.candidates}
        self.state.rfqs = [R.draft(self.state.job_id, a, emails.get(a.candidate_id, ""), self.state.spec)
                           for a in self.state.assessments if R.can_rfq(a)]
        self._commit("rfq_draft")
        return True

    def _s_await_approval(self, *_) -> bool:
        if any(r.status == "DRAFT" for r in self.state.rfqs):
            if self.state.status != "AWAITING_APPROVAL":
                self.state.status = "AWAITING_APPROVAL"
                self.store.checkpoint(self.state)
            return False
        self._commit("await_approval")
        return True

    def _s_rfq_send(self, *_) -> bool:
        for r in self.state.rfqs:
            if r.status != "APPROVED":
                continue
            payload = {"rfq_id": r.rfq_id, "to": r.to, "subject": r.subject, "body": r.body, "key": r.idempotency_key}
            r.status = self.store.send_once(r.idempotency_key, "rfq_email", payload, self.sender)
            if self.crash_at == "mid_send":  # killed after the send, before the checkpoint
                raise Crash("mid_send")
            r.sent_at = r.sent_at or self.store.db.execute(
                "SELECT ts FROM outbox WHERE key=?", (r.idempotency_key,)).fetchone()[0]
        self._commit("rfq_send", "RFQS_OUTSTANDING" if any(r.status == "SENT" for r in self.state.rfqs) else None)
        return True

    def _s_distributor(self, *_) -> bool:
        part, label = distributor_offers(self.state.spec.mpn or "", self.cfg, self.store)
        self.state.assessments = rank(R.component_assess(self.state.spec, part, label, self.cfg, self.regs), self.cfg)
        self.state.extra["manufacturer"] = (part or {}).get("manufacturer")
        self._commit("distributor")
        return True

    def _s_decide(self, *_) -> bool:
        self.state.status = summary(self.state)[0]
        self._commit("decide")
        return True

    # -- human actions ------------------------------------------------------------------------
    def approve(self, rfq_ids: list[str] | str = "all", by: str = "operator") -> list[str]:
        done = []
        for r in self.state.rfqs:
            if r.status == "DRAFT" and (rfq_ids == "all" or r.rfq_id in rfq_ids):
                r.status, r.approved_by = "APPROVED", by
                done.append(r.rfq_id)
        self.store.audit("rfq_approved", ids=done, by=by)
        self.store.checkpoint(self.state)
        return done

    def add_quote(self, raw: dict) -> Assessment:
        q: Quote = R.normalize_quote(raw, self.state.spec)
        if self.cfg.limits.get("mode") == "live" and q.label.value == "TEST_FIXTURE":
            raise ValueError("TEST_FIXTURE quotes are never accepted in a live run.")
        a = next(x for x in self.state.assessments if x.candidate_id == q.candidate_id)
        apply_quote(a, q, self.state.spec, self.cfg)
        self.state.quotes.append(q)
        self.state.assessments = rank(self.state.assessments, self.cfg)
        self.state.status = summary(self.state)[0]
        self.store.audit("quote_intake", candidate=q.candidate_id, label=q.label.value, unit_price=q.unit_price)
        self.store.checkpoint(self.state)
        return a


def summary(st: JobState) -> tuple[str, str]:
    by = {d: [a for a in st.assessments if a.decision == d] for d in Decision}
    if by[Decision.RECOMMEND_ORDER]:
        a = by[Decision.RECOMMEND_ORDER][0]
        q = a.quote
        demo = " [DEMO: fixture data, not a live decision]" if q and q.label.value == "TEST_FIXTURE" else ""
        return "RECOMMEND_ORDER", (f"Recommend order from {a.company} ({a.model}): {q.currency} {q.unit_price}/unit x "
                                   f"{q.quantity} = {q.currency} {q.goods_value()} goods value; tax/freight shown separately.{demo}")
    sent = [r for r in st.rfqs if r.status == "SENT"]
    held = [r for r in st.rfqs if r.status == "HELD_AMBIGUOUS"]
    drafts = [r for r in st.rfqs if r.status in ("DRAFT", "APPROVED")]
    qual = len(by[Decision.QUALIFIED_FOR_RFQ])
    prov = len(by[Decision.PROVISIONAL])
    if held:
        return "HELD_FOR_PERSON", f"{len(held)} RFQ send(s) ambiguous after restart; a person must confirm before any resend."
    if sent:
        return "RFQS_OUTSTANDING", f"Shortlist + RFQs outstanding: {len(sent)} sent, awaiting response. {qual} qualified, {prov} provisional. No order without a real quote."
    if drafts:
        return "AWAITING_APPROVAL", f"Shortlist ready: {qual} qualified, {prov} provisional; {len(drafts)} RFQ(s) await human approval."
    if st.status == "NEEDS_CLARIFICATION":
        return "NEEDS_CLARIFICATION", "Before searching: " + " ".join(st.extra.get("questions", []))
    if not st.assessments:
        return "CANNOT_VERIFY", "No candidates or offers found; nothing can be recommended."
    return "NO_QUALIFIED_SUPPLIER", "No candidate passed the hard gates; see each FAIL reason and source."
