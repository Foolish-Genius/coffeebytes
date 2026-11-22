"""Vague-need clarification and follow-up turns. Rules first (free); one capped LLM call only for text rules miss."""
from __future__ import annotations

import re
from typing import Optional

from .models import Spec

LLM_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["action", "value"],
    "properties": {"action": {"type": "string", "enum": ["set_quantity", "set_location", "add_requirement",
                                                         "explain", "proceed", "other"]},
                   "value": {"type": "string"}},
}


def clarify(spec: Spec) -> list[str]:
    """Questions that must be answered before searching. Missing location is only an assumption."""
    q = []
    if spec.lane == "GENERIC":
        q.append("What is the device for (vehicle tracking, warehouse scanning/RFID, temperature logging, "
                 "or a specific component part number)?")
    if spec.quantity is None:
        q.append("How many units do you need?")
    return q


def parse(text: str) -> tuple[str, str]:
    """Map a follow-up to (action, value) with rules. ('other', text) when nothing matches."""
    t = text.strip()
    low = t.lower()
    if re.match(r"^(why|explain|what|how|which|who|when|is|are|does|do|can)\b", low) or low.endswith("?"):
        return "explain", t  # a question never changes the spec
    if low.rstrip(".! ") in ("skip", "proceed", "continue", "go ahead", "use assumptions", "ok", "okay"):
        return "proceed", ""
    m = re.search(r"\b(?i:deliver(?:y)?\s+(?:to|in|at)|ship\s+to|location\s*(?:is|:)?)\s+"
                  r"([A-Z][a-z]+(?:\s[A-Z][a-z]+)?|[a-z]+)", t)  # stop at the first lowercase word after the place
    if m:
        return "set_location", m.group(1).strip().title()
    m = re.search(r"(\d[\d,]*)\s*(units?|pcs|pieces|nos|trucks|vehicles|devices|trackers|scanners|loggers)\b", low)
    if m or re.match(r"^\s*(make it|quantity|qty)?\s*[:=]?\s*\d[\d,]*\s*$", low):
        num = m.group(1) if m else re.search(r"\d[\d,]*", low).group(0)
        return "set_quantity", num.replace(",", "")
    m = re.search(r"(?:require|must have|needs?|add|should have)\s+(.+)$", t, re.I)
    if m:
        return "add_requirement", m.group(1).strip().rstrip(".")
    lane_words = {"tracker": "trackers for vehicles", "vehicle": "vehicle trackers", "truck": "truck trackers",
                  "rfid": "RFID readers", "scanner": "barcode scanners", "temperature": "temperature loggers",
                  "logger": "temperature loggers"}
    for k, v in lane_words.items():
        if k in low:
            return "set_purpose", v
    return "other", t


def requirement_field(phrase: str, lane_fields: dict) -> tuple[str, dict]:
    """A follow-up requirement becomes a spec field checked verbatim like any other.
    'IP67' tightens the IP rule; anything else is matched as the phrase itself."""
    ip = re.search(r"ip\s?6([5-9])", phrase, re.I)
    if ip and "ip_rating" in lane_fields:
        return "ip_rating", {"label": f"IP6{ip.group(1)} or better", "patterns": [f"ip6[{ip.group(1)}-9]"],
                             "contradict": [r"ip[0-5][0-9]\b"]}
    key = "custom:" + re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")[:40]
    return key, {"label": phrase, "patterns": [re.escape(phrase.lower())]}


def explain(text: str, assessments) -> str:
    """Answer 'why X' from the gates already computed - no new facts, no LLM."""
    low = text.lower()
    hit = next((a for a in assessments if a.company.lower().split()[0] in low or a.candidate_id in low), None)
    if not hit:
        return "Name a candidate (company or id), e.g. 'why Orbitrack?'."
    bad = [g for g in hit.gates if g.value.value in ("FAIL", "UNKNOWN", "PARTIAL")]
    lines = [f"{hit.company} is {hit.decision.value} ({hit.role.value})."]
    lines += [f"- {g.gate}: {g.value.value} - {g.reason}" + (f" [source: {g.source}]" if g.source else "") for g in bad]
    lines += [f"- flag: {f}" for f in hit.flags]
    if len(lines) == 1:
        lines.append("All hard gates passed; price, stock and capacity are still TO CONFIRM until a quote arrives.")
    return "\n".join(lines)


def llm_parse(text: str, llm) -> Optional[tuple[str, str]]:
    if llm is None:
        return None
    out = llm.json_call("followup_parse", "Classify this buyer follow-up on an electronics sourcing job. "
                        "Actions: set_quantity (value=number), set_location (city), add_requirement (short spec "
                        "phrase), explain, proceed, other.\nFollow-up: " + text, LLM_SCHEMA)
    return (out["action"], out["value"]) if out else None
