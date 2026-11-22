"""RFQ drafting, quote normalization, and the component (authorized-channel) lane."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from .config import Config
from .models import RFQ, Assessment, Decision, GateResult, GV, PriceLabel, Quote, Role, Spec
from .registry import Registries, norm_company
from .store import now

DEVICE_FIELDS = ["unit price", "quantity and MOQ", "approval certificate / TAC number for the quoted model",
                 "warranty term", "installation and service network", "SIM / data plan terms", "firmware updates",
                 "delivery date", "payment terms", "quote validity", "GST", "freight"]


def can_rfq(a: Assessment) -> bool:
    g = {x.gate: x.value for x in a.gates}
    hard_ok = g.get("entity_identity") == GV.PASS and g.get("supplier_role") == GV.PASS \
        and g.get("legal_approval") in (GV.PASS, GV.NA)
    return a.decision in (Decision.QUALIFIED_FOR_RFQ, Decision.PROVISIONAL) and hard_ok


def draft(job_id: str, a: Assessment, email: str, spec: Spec) -> RFQ:
    to_confirm = [x.reason.split(":")[0] for x in a.gates if x.gate.startswith("spec:") and x.value != GV.PASS]
    lines = [
        "Dear team,", "",
        f"We are sourcing {spec.quantity or 'TBC'} x {spec.device}, model {a.model}, "
        f"for delivery to {spec.delivery_location or 'our site in India (TBC)'}.", "",
        "Please quote the following:", *[f"- {f}" for f in DEVICE_FIELDS],
    ]
    if to_confirm:
        lines += ["", "Please also confirm, with a datasheet reference:", *[f"- {f}" for f in to_confirm]]
    lines += ["", "Please confirm you can supply this quantity and your production lead time.", "",
              "Regards,", "Procurement (via SiliconScout)"]
    body = "\n".join(lines)
    key = hashlib.sha256(f"{job_id}|{a.candidate_id}|{body}".encode()).hexdigest()[:24]
    return RFQ(rfq_id=f"rfq-{a.candidate_id}", candidate_id=a.candidate_id, to=email or "(no contact found)",
               subject=f"RFQ: {spec.quantity or ''} x {a.model} ({spec.device})", body=body, idempotency_key=key)


def normalize_quote(raw: dict, spec: Spec) -> Quote:
    """Missing freight/tax/installation stays None (shown UNKNOWN), never zero."""
    q = Quote(**{**raw, "received_at": raw.get("received_at") or now()})
    if q.quantity is None and spec.quantity:
        q.quantity = spec.quantity
    return q


def quote_view(q: Quote) -> dict:
    gv = q.goods_value()
    u = lambda v: "UNKNOWN" if v is None else v  # noqa: E731
    return {"label": q.label.value, "currency": q.currency, "unit_price": u(q.unit_price), "quantity": u(q.quantity),
            "goods_value": u(gv), "gst_pct": u(q.gst_pct), "freight": u(q.freight), "installation": u(q.installation),
            "delivery_days": u(q.delivery_days), "warranty_months": u(q.warranty_months),
            "approval_ref": u(q.approval_ref), "validity_until": u(q.validity_until), "received_at": q.received_at}


# --- component lane -------------------------------------------------------------------------------

def _price_at(breaks: list, qty: int) -> tuple[Optional[float], Optional[int]]:
    best = None
    for bq, p in sorted(breaks):
        if qty >= bq:
            best = (p, bq)
    return best if best else (None, None)


def component_assess(spec: Spec, part: Optional[dict], label: str, cfg: Config, regs: Registries) -> list[Assessment]:
    if not part:
        return []
    qty = spec.quantity or 1
    authorized = regs.authorized(part["manufacturer"])
    max_age = int(cfg.limits.get("distributor", {}).get("max_quote_age_minutes", 60))
    fetched = part.get("fetched_at") or now()
    age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(fetched)).total_seconds() / 60
    out = []
    for i, o in enumerate(part.get("offers", []), 1):
        a = Assessment(candidate_id=f"d{i:02d}", company=o["distributor"], model=spec.mpn or "")
        is_auth = norm_company(o["distributor"]) in {norm_company(d) for d in authorized} \
            or o["distributor"] == "Mouser Electronics" and label == "DISTRIBUTOR_LIVE" and not authorized
        a.role = Role.AUTHORIZED_DISTRIBUTOR if is_auth else Role.TRADER
        price, brk = _price_at(o.get("price_breaks", []), qty)
        a.quote = Quote(candidate_id=a.candidate_id, label=PriceLabel(label), currency=o.get("currency", "INR"),
                        unit_price=price, quantity=qty, mpn=spec.mpn, packaging=o.get("packaging"),
                        pack_multiple=o.get("pack_multiple"), stock=o.get("stock"), price_break_qty=brk,
                        lead_time_days=o.get("lead_time_days"), delivery_days=o.get("lead_time_days"),
                        received_at=fetched, source=o["distributor"])
        stock_ok = (o.get("stock") or 0) >= qty
        a.gates = [
            GateResult(gate="supplier_role", value=GV.PASS if is_auth else GV.FAIL,
                       reason=("On manufacturer's authorized-distributor list for " if is_auth else "NOT on authorized list for ")
                       + part["manufacturer"], source="authorized_distributors"),
            GateResult(gate="part_number", value=GV.PASS, reason=f"Exact MPN {spec.mpn}"),
            GateResult(gate="price_lead_stock", value=GV.CONFIRMED if stock_ok else GV.TO_CONFIRM,
                       reason=f"Stock {o.get('stock')} vs need {qty}; {o.get('currency')} {price} @ break {brk}; age {age_min:.0f} min",
                       source=label),
        ]
        if not is_auth:
            a.decision = Decision.NOT_A_FIT
        elif stock_ok and age_min <= max_age and price is not None and \
                (label == "DISTRIBUTOR_LIVE" or cfg.limits.get("mode") == "demo"):
            a.decision = Decision.RECOMMEND_ORDER
        else:
            a.decision = Decision.QUALIFIED_FOR_RFQ
        out.append(a)
    return out
