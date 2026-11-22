"""Need-to-spec translation and lane routing. Rules first (free, deterministic); LLM only if rules can't route."""
from __future__ import annotations

import re
from typing import Optional

from .config import Config
from .llm import LLM, SPEC_SCHEMA
from .models import Spec

MPN_RE = re.compile(r"\b(?=[A-Z0-9\-]*\d)(?=[A-Z0-9\-]*[A-Z])[A-Z0-9][A-Z0-9\-]{5,}\b")
LOC_RE = re.compile(r"\b(?:deliver(?:ed|y)?\s+(?:to|in|at)|to|in|at|for)\s+([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)")
NON_PLACES = {"AIS", "GPS", "India", "NavIC", "The", "Our", "We", "IP"}


def _score(text: str, keywords: list[str]) -> int:
    t = text.lower()
    return sum(1 for k in keywords if re.search(r"\b" + re.escape(k) + r"\b", t))


def find_mpn(text: str, cfg: Config) -> Optional[str]:
    for m in MPN_RE.findall(text):
        if m.upper() not in cfg.not_mpn:
            return m
    return None


def route(text: str, cfg: Config) -> tuple[str, Optional[str]]:
    mpn = find_mpn(text, cfg)
    scores = {k: _score(text, v.get("keywords", [])) for k, v in cfg.lanes.items() if k != "GENERIC"}
    device_best = max((s for k, s in scores.items() if k != "D_COMPONENT"), default=0)
    if mpn and (scores.get("D_COMPONENT", 0) > 0 or device_best == 0):
        return "D_COMPONENT", mpn
    best = max(scores, key=lambda k: scores[k])
    return (best if scores[best] > 0 else "GENERIC"), mpn


def _quantity(text: str, unit_words: list[str]) -> tuple[Optional[int], str]:
    units = "|".join(re.escape(u) for u in unit_words)
    m = re.search(r"(\d[\d,]*)\s*(" + units + r")\b", text, re.I)
    if m:
        return int(m.group(1).replace(",", "")), m.group(2).lower()
    m = re.search(r"(?<![A-Za-z\-])(\d[\d,]{0,7})(?![\d\-])", text)
    return (int(m.group(1).replace(",", "")), "units") if m else (None, "units")


def _location(text: str) -> Optional[str]:
    for m in LOC_RE.finditer(text):
        place = m.group(1)
        if place.split()[0] not in NON_PLACES:
            return place
    return None


def translate(need: str, cfg: Config, llm: Optional[LLM] = None) -> Spec:
    lane, mpn = route(need, cfg)
    source = "rules"
    lc = cfg.lanes[lane]
    qty, unit = _quantity(need, lc.get("unit_words", []))
    loc = _location(need)
    assumptions = list(lc.get("assumptions", []))

    if lane == "GENERIC" and llm is not None:  # rules could not route: one cheap structured call
        out = llm.json_call("spec_translate", "Classify this procurement need for an Indian 3PL:\n" + need, SPEC_SCHEMA)
        if out and out.get("lane") in cfg.lanes:
            lane, source = out["lane"], "llm"
            lc = cfg.lanes[lane]
            qty = qty or out.get("quantity")
            loc = loc or out.get("delivery_location")
            assumptions = list(lc.get("assumptions", [])) + [f"(LLM) {a}" for a in out.get("assumptions", [])]

    if unit in ("trucks", "vehicles"):
        assumptions.append(f"{qty} {unit} -> {qty} devices (one per vehicle).")
    if qty is None:
        assumptions.append("Quantity not stated; RFQ will ask for MOQ and price breaks.")
    if loc is None:
        assumptions.append("Delivery location not stated; assumed buyer HQ in India, confirm before order.")
    reqs = {k: v["label"] for k, v in lc.get("required_fields", {}).items()}
    return Spec(raw_need=need, lane=lane, device=lc["device"], quantity=qty, unit=unit,
                delivery_location=loc, mpn=mpn if lane == "D_COMPONENT" else None,
                requirements=reqs, assumptions=assumptions, source=source,
                compliance_checks=list(lc.get("compliance_checks", [])))
