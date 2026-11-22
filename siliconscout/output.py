"""Output contract: one JSON shape for CLI and workbench, checked against config/output_schema.json."""
from __future__ import annotations

from .pipeline import Job, summary
from .rfq import quote_view


def to_output(job: Job) -> dict:
    st = job.state
    status, text = summary(st) if st.assessments or st.rfqs else (st.status, "")
    ledger = job.store.ledger_rows()
    return {
        "job_id": st.job_id,
        "status": status,
        "lane": st.spec.lane if st.spec else "",
        "spec": st.spec.model_dump() if st.spec else {},
        "candidates": [{
            "candidate_id": a.candidate_id, "rank": a.rank, "company": a.company, "model": a.model,
            "decision": a.decision.value, "role": a.role.value, "maker_of_record": a.maker_of_record,
            "tied_with": a.tied_with, "flags": a.flags, "missing_evidence": a.missing_evidence,
            "company_context": a.company_context,
            "gates": [g.model_dump(mode="json") for g in a.gates],
            "registry_hits": [h.model_dump() for h in a.registry_hits],
            "quote": quote_view(a.quote) if a.quote else None,
        } for a in st.assessments],
        "rfqs": [r.model_dump() for r in st.rfqs],
        "decision_summary": text,
        "crustdata_plan": st.extra.get("crustdata_plan"),
        "questions": st.extra.get("questions", []),
        "thread": st.extra.get("thread", []),
        "cost": {"paid_calls": len(ledger),
                 "crustdata_credits": sum(r.get("credits", 0) for r in ledger if r.get("provider") == "crustdata"), "inr_estimate": round(sum(r.get("inr_estimate", 0) for r in ledger), 4),
                 "tokens_in": sum(r.get("tokens_in", 0) for r in ledger),
                 "tokens_out": sum(r.get("tokens_out", 0) for r in ledger)},
    }


def validate(obj, schema: dict, path: str = "$") -> list[str]:
    """Minimal JSON-schema check (type/required/properties/items) - enough for the contract tests."""
    errs: list[str] = []
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str}
    if t in types and not isinstance(obj, types[t]):
        return [f"{path}: expected {t}"]
    if isinstance(obj, dict):
        errs += [f"{path}.{k}: missing" for k in schema.get("required", []) if k not in obj]
        for k, sub in schema.get("properties", {}).items():
            if k in obj:
                errs += validate(obj[k], sub, f"{path}.{k}")
    if isinstance(obj, list) and "items" in schema:
        for i, v in enumerate(obj):
            errs += validate(v, schema["items"], f"{path}[{i}]")
    return errs
