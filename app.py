"""SiliconScout workbench: one screen. Run: streamlit run app.py"""
from __future__ import annotations

import uuid

import streamlit as st

from siliconscout.config import Config
from siliconscout.output import to_output
from siliconscout.pipeline import Job

def safe(text: str) -> str:
    """Fixture URLs/emails (.example/.test) are not real: show them as plain code, never as dead links."""
    import re
    if re.search(r"\.(example|test)\b", text or ""):
        return f"`{text}` (fixture, not a real address)"
    return text


ICON = {"PASS": "🟢", "CONFIRMED": "🟢", "PARTIAL": "🟡", "UNKNOWN": "⚪", "TO_CONFIRM": "⚪", "FAIL": "🔴", "N/A": "▫️"}

st.set_page_config(page_title="SiliconScout 3.0", layout="wide")
st.title("SiliconScout 3.0 — ET sourcing workbench")
st.caption("Directories tell you who says they make trackers. We tell you whose model holds a government approval, and who actually makes it.")

cfg = Config.load()
with st.sidebar:
    if "default_job" not in st.session_state:  # stable default: a fresh uuid each rerun reset typed ids
        st.session_state["default_job"] = st.session_state.get("job_id", uuid.uuid4().hex[:8])
    job_id = st.text_input("Job id", value=st.session_state["default_job"], key="job_input")
    source = st.selectbox("Discovery source", ["fixture", "registry"], help="fixture = demo data; registry = seed from approval lists")
    st.write(f"Mode: **{cfg.limits.get('mode')}**")
    import os
    llm_on = bool(os.environ.get("ANTHROPIC_API_KEY"))
    st.write(("🟢 Claude API connected" if llm_on else "⚪ Claude API not set (free rules-only mode)")
             + f" · cap ₹{cfg.limits.get('max_inr_per_job')}/job")
    cd = cfg.limits.get("crustdata", {})
    if not os.environ.get("CRUSTDATA_API_KEY"):
        st.write("⚪ Crustdata not set (no company context)")
    elif cd.get("allow_spend"):
        st.write(f"🟢 Crustdata live ({cd.get('mode')}, cap {cd.get('max_credits_per_job')} credits/job)")
    else:
        st.write(f"🔒 Crustdata key set, spend LOCKED (dry-run only, {cd.get('mode')} mode)")
    st.write("🟢 Mouser live prices" if os.environ.get("MOUSER_API_KEY") else "⚪ Mouser not set (fixture prices)")
    st.caption("Keys go in the .env file in the project folder (see .env.example). Restart the app after editing.")

need = st.text_area("Operating need", "Need GPS trackers for 300 trucks, deliver to Pune")
if st.button("Run / resume", type="primary"):
    st.session_state["job_id"] = job_id
    Job(job_id, cfg).run(need, source)

if "job_id" not in st.session_state:
    st.stop()
job = Job(st.session_state["job_id"], cfg)
out = to_output(job)

st.subheader(f"Status: {out['status']}")
st.info(out["decision_summary"] or "-")
spec = out["spec"]
c1, c2 = st.columns(2)
c1.markdown(f"**Lane:** {out['lane']}  \n**Device:** {spec.get('device')}  \n**Qty:** {spec.get('quantity')}  \n"
            f"**Deliver to:** {spec.get('delivery_location') or 'UNKNOWN'}  \n**MPN:** {spec.get('mpn') or '-'}")
c2.markdown("**Requirements:** " + ", ".join(spec.get("requirements", {}).values()) +
            "\n\n**Assumptions:**\n" + "\n".join(f"- {a}" for a in spec.get("assumptions", [])))
if spec.get("compliance_checks"):
    with st.expander(f"ET compliance checks for this lane ({len(spec['compliance_checks'])})"):
        for c in spec["compliance_checks"]:
            st.markdown(f"- {c}")

plan = out.get("crustdata_plan")
if plan:
    res = plan.get("result") or {}
    if res:
        msg = (f"Crustdata {plan['mode']}: sent {plan['to_send']} companies in {res.get('ok_requests', 0) + res.get('failed_requests', 0)} "
               f"request(s) · matched {res.get('matched', 0)} · {res.get('credits', 0)} credits used")
        if plan.get("budget"):
            msg += f" · budget left {plan['budget']['remaining']} of {plan['budget']['limit']}"
        (st.success if res.get("ok_requests") else st.error)(msg)
        for e in res.get("errors", []):
            st.caption(f"Crustdata error: {e}")
    elif plan.get("to_send") and not cfg.limits.get("crustdata", {}).get("allow_spend"):
        st.warning(f"Crustdata dry run: {plan['to_send']} companies would be sent to {plan['endpoint']}, "
                   f"worst case {plan['worst_case_credits']} credits (cap {plan['cap_credits']}; {plan['cached_free']} cached, free). "
                   "Nothing was sent.")
        if st.checkbox(f"I approve spending up to {plan['worst_case_credits']} Crustdata credits for this job"):
            if st.button("Fetch Crustdata context now"):
                Job(st.session_state["job_id"], cfg, crust_spend=True).refresh_context()
                st.rerun()

st.subheader("Candidates (grouped by decision; no fake scores)")
for c in out["candidates"]:
    head = f"#{c['rank']} {c['company']} · {c['model']} · **{c['decision']}** · {c['role']}"
    if c["tied_with"]:
        head += f" · tied on commercial terms with {', '.join(c['tied_with'])}"
    with st.expander(head):
        for f in c["flags"]:
            st.warning(f)
        cc = c.get("company_context") or {}
        if cc.get("matched"):
            st.caption("Crustdata context (not approval evidence): " + " · ".join(
                f"{k}: {cc[k]}" for k in ("name", "primary_domain", "hq", "country", "headcount", "employee_count_range", "year_founded") if cc.get(k)))
        elif cc:
            st.caption("Crustdata: no company record found.")
        if c["maker_of_record"] and c["role"] != "MAKER":
            st.write(f"Maker of record: **{c['maker_of_record']}** (recorded separately)")
        for g in c["gates"]:
            st.markdown(f"{ICON.get(g['value'], '')} `{g['gate']}` **{g['value']}** — {g['reason']}")
            if g.get("source"):
                st.caption(f"source: {safe(g['source'])}" + (f" · dated {g['date']}" if g.get("date") else ""))
            if g.get("passage"):
                st.caption(f"verbatim: “{g['passage']}”")
        if c["quote"]:
            st.json(c["quote"])

if out["rfqs"]:
    st.subheader("RFQs")
    for r in out["rfqs"]:
        with st.expander(f"{r['rfq_id']} → {safe(r['to'])} · {r['status']}"):
            st.code(r["body"])
    if any(r["status"] == "DRAFT" for r in out["rfqs"]) and st.button("Approve all drafts and send"):
        job.approve("all", by="workbench")
        job.run()
        st.rerun()

with st.form("quote"):
    st.subheader("Quote intake")
    ids = [c["candidate_id"] for c in out["candidates"]]
    cid = st.selectbox("Candidate", ids) if ids else None
    label = st.selectbox("Label", ["SUPPLIER_QUOTE", "DISTRIBUTOR_LIVE", "PUBLIC_BUDGETARY"])
    price = st.number_input("Unit price (INR)", min_value=0.0)
    qty = st.number_input("Quantity", min_value=0, value=int(spec.get("quantity") or 0))
    days = st.number_input("Delivery days", min_value=0, value=0)
    warranty = st.number_input("Warranty months", min_value=0, value=0)
    if st.form_submit_button("Add quote") and cid:
        job.add_quote({"candidate_id": cid, "label": label, "unit_price": price or None, "quantity": qty or None,
                       "delivery_days": days or None, "warranty_months": warranty or None})
        st.rerun()

st.subheader("Ask / follow up")
for q in out.get("questions", []):
    st.warning(q)
for m in out.get("thread", []):
    with st.chat_message(m["role"]):
        st.text(m["text"])
msg = st.chat_input("e.g. '500 units', 'deliver to Chennai', 'require IP67', 'why Orbitrack?', 'proceed'")
if msg:
    job.follow_up(msg)
    st.rerun()

st.subheader("Cost ledger")
st.write(out["cost"])
with st.expander("Audit log"):
    st.dataframe(job.store.audit_rows())
