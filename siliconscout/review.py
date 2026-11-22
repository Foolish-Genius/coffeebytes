"""Supply-chain review: a client's purchase history in, verified inefficiencies + recommendations out.

Rules only (zero paid calls). Every finding cites its evidence (registry row, or the client's own rows).
No savings are promised: price/lead-time findings describe the client's own data, not the market.

CSV columns (header row; extra columns ignored):
  item, supplier, qty, unit_price, order_date[, category, manufacturer, lead_time_days, currency]
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from .config import Config
from .gates import registry_check
from .models import Candidate, GV, Role, Spec
from .registry import Registries, norm_company, norm_model
from .spec import route

SEV = {"high": 0, "medium": 1, "low": 2}


def _f(v) -> Optional[float]:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def load_rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [{k.strip().lower(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(fh)]


def review(rows: list[dict], cfg: Config, today: Optional[date] = None) -> dict:
    today = today or date.today()
    regs = Registries(cfg.data_dir)
    rcfg = cfg.priorities.get("review", {})
    rise = float(rcfg.get("price_rise_pct", 15))
    expiry_days = int(rcfg.get("approval_expiry_warn_days", 180))
    long_days = int(cfg.priorities.get("long_lead_time_days", 84))
    findings: list[dict] = []

    def add(sev, item, supplier, issue, evidence, rec):
        findings.append({"severity": sev, "item": item, "supplier": supplier, "issue": issue,
                         "evidence": evidence, "recommendation": rec})

    by_item: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("item"):
            by_item[norm_model(r["item"])].append(r)

    for key, rs in by_item.items():
        item = rs[0]["item"]
        cat = rs[0].get("category", "")
        lane, _ = route(cat, cfg) if cat else route(item, cfg)  # a model number alone looks like a part number
        suppliers = sorted({r.get("supplier", "") for r in rs if r.get("supplier")})

        # 1. legal approval + who they actually buy from (device lanes with a registry)
        if cfg.lanes.get(lane, {}).get("registries"):
            spec = Spec(raw_need=item, lane=lane, device=cfg.lanes[lane]["device"])
            for sup in suppliers:
                gates, role, hits, flags, maker = registry_check(Candidate(candidate_id="r", company=sup, model=item),
                                                                 spec, cfg, regs, today)
                g = gates[0]
                ev = f"{g.source} - {g.reason}"
                if g.value == GV.FAIL and "expired" in g.reason:
                    add("high", item, sup, "Approval expired", ev,
                        "Stop new orders of this model; re-certify installed units or replace with an approved model.")
                elif g.value == GV.FAIL and hits:
                    add("medium", item, sup, f"Buying an approved model from a non-registered seller (registered maker: {hits[0].company})",
                        ev, f"Buy direct from {hits[0].company} or its authorized distributor: warranty and service stay intact.")
                elif g.value == GV.FAIL:
                    add("high", item, sup, "Model not on any applicable approval list", ev,
                        "Legally required approval is missing: replace with an approved model before the next fleet audit.")
                elif g.value == GV.UNKNOWN:
                    add("medium", item, sup, "Ambiguous registry match", ev, "Have a person confirm the exact model and maker.")
                elif g.value == GV.PASS and hits and hits[0].valid_until:
                    left = (date.fromisoformat(hits[0].valid_until) - today).days
                    if 0 <= left <= expiry_days:
                        add("high", item, sup, f"Approval expires in {left} days ({hits[0].valid_until})", ev,
                            "Plan re-certification or a replacement model before expiry.")

        # 2. component lane: authorized channel
        if lane == "D_COMPONENT":
            mfr = rs[0].get("manufacturer", "")
            auth = {norm_company(d) for d in regs.authorized(mfr)} if mfr else set()
            for sup in suppliers:
                if mfr and auth and norm_company(sup) not in auth:
                    add("medium", item, sup, f"Not an authorized distributor of {mfr}",
                        f"authorized_distributors list for {mfr}", "Move this part to an authorized distributor (counterfeit risk).")
                elif not mfr:
                    add("low", item, sup, "Manufacturer unknown", "client data has no manufacturer column value",
                        "Record the manufacturer so the channel can be verified.")

        # 3. single source
        if len(suppliers) == 1 and len(rs) >= 2:
            add("medium", item, suppliers[0], "Single source", f"{len(rs)} orders, all from {suppliers[0]}",
                "Qualify a second approved source to cut supply risk.")

        # 4. price trend (client's own data)
        dated = sorted([r for r in rs if _f(r.get("unit_price")) and r.get("order_date")], key=lambda r: r["order_date"])
        if len(dated) >= 2:
            p0, p1 = _f(dated[0]["unit_price"]), _f(dated[-1]["unit_price"])
            pct = (p1 - p0) / p0 * 100 if p0 else 0
            if pct >= rise:
                add("medium", item, dated[-1].get("supplier", ""), f"Price up {pct:.0f}%",
                    f"{dated[0]['order_date']}: {p0:g} -> {dated[-1]['order_date']}: {p1:g} (client data)",
                    "Re-quote from approved makers; for long-lead items lock price to delivery or a capped escalation clause.")

        # 5. lead time
        lts = [(_f(r.get("lead_time_days")), r) for r in rs if _f(r.get("lead_time_days")) is not None]
        if lts:
            lt, r = max(lts, key=lambda x: x[0])
            if lt >= long_days:
                add("low", item, r.get("supplier", ""), f"Long lead time: {lt:g} days",
                    f"order {r.get('order_date', '?')} (client data)",
                    "Order earlier (reorder point = lead time x usage) or hold buffer stock; fix the price at PO.")

    findings.sort(key=lambda f: (SEV[f["severity"]], f["item"]))
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEV}
    return {"lines": len(rows), "items": len(by_item), "findings": findings, "counts": counts,
            "as_of": today.isoformat(),
            "note": "Rule-based review of the client's own records against loaded registries; no market prices used."}
