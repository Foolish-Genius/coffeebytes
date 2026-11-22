"""Registry-first gates, decision states and ranking without fake scores."""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from .config import Config
from .evidence import extract_claims, find_injections, normalize, verify_claim
from .llm import CLAIMS_SCHEMA
from .models import (ORDER_LABELS, Assessment, Candidate, Claim, Decision, GateResult, GV, PriceLabel, Quote,
                     RegistryHit, Role, Spec)
from .registry import Registries, norm_company, norm_model
from .store import Store

INDIA_RE = re.compile(r"\bindia\b|pan-india|\b(maharashtra|karnataka|tamil nadu|haryana|uttar pradesh|rajasthan|"
                      r"gujarat|telangana|delhi|kerala|west bengal)\b", re.I)


def _cite(h: RegistryHit) -> str:
    return f"{h.title} (as on {h.as_on}, sha256 {h.sha256}, {h.label}) row: {h.company} / {h.model} / {h.ref}"


def registry_check(c: Candidate, spec: Spec, cfg: Config, regs: Registries, today: date) -> tuple[list[GateResult], Role, list[RegistryHit], list[str], Optional[str]]:
    """Returns (gates, role, hits, flags, maker_of_record). Company AND model must match; never name alone."""
    lane = cfg.lanes[spec.lane]
    names = lane.get("registries", [])
    if not names:
        why = "No government registry exists/is confirmed for this category; official-site evidence only."
        return [GateResult(gate="legal_approval", value=GV.NA, reason=why)], Role.UNKNOWN, [], [], None
    snaps = [regs.get(n) for n in names]
    me = norm_company(c.company)
    page_text = normalize(" ".join(p.text for p in c.pages))
    exact, other_co = [], []
    for s in snaps:
        for row in s.by_model(c.model):
            (exact if norm_company(row["company"]) == me else other_co).append(s.hit(row))
    flags: list[str] = []
    listed = ", ".join(f"{s.title} as on {s.as_on}" for s in snaps)

    def validity(h: RegistryHit) -> GateResult:
        if h.valid_until and h.valid_until < today.isoformat():
            return GateResult(gate="legal_approval", value=GV.FAIL, source=_cite(h),
                              reason=f"Listed but approval expired on {h.valid_until}.")
        return GateResult(gate="legal_approval", value=GV.PASS, source=_cite(h),
                          reason=f"Model {h.model} listed for {h.company}, ref {h.ref}, valid until {h.valid_until or 'n/a'}.")

    if exact:
        valid = [h for h in exact if not (h.valid_until and h.valid_until < today.isoformat())]
        h = (valid or exact)[0]
        return [validity(h)], Role.MAKER, exact, flags, h.company
    # brand vs maker: model listed under a different company whose row names this candidate as brand,
    # or the candidate's own page names the registered maker. Recorded separately, never merged.
    for h in other_co:
        if (h.brand and norm_company(h.brand) == me) or norm_company(h.company) in norm_company(page_text):
            g = validity(h)
            g.reason = f"Brand owner; registered maker is {h.company}. " + g.reason
            return [g], Role.BRAND_OWNER, [h], flags, h.company
    if other_co:
        h = other_co[0]
        flags.append(f"Possible impersonation/lookalike: model {c.model} is registered to {h.company}, "
                     f"not {c.company}, and the site does not name the registered maker.")
        return [GateResult(gate="legal_approval", value=GV.FAIL, source=_cite(h),
                           reason=f"{c.company} / {c.model} is not on {listed}; the model is registered to {h.company}.")], \
            Role.UNKNOWN, [h], flags, None
    return [GateResult(gate="legal_approval", value=GV.FAIL, source=listed,
                       reason=f"Company+model {c.company} / {c.model or '(none given)'} not found on {listed}.")], \
        Role.UNKNOWN, [], flags, None


def apply_company_context(a: Assessment, c: Candidate, ctx: Optional[dict], ig: GateResult, cfg: Config) -> GateResult:
    """Crustdata = company context only. It can corroborate or contradict identity and add a trader signal.
    It never touches legal_approval and never proves capability."""
    if not ctx:
        return ig
    a.company_context = {k: v for k, v in ctx.items() if v not in (None, "", [])}
    if not ctx.get("matched"):
        ig.reason += " (Crustdata: no company record.)"
        return ig
    cn = norm_company(ctx.get("name") or "")
    names = {norm_company(c.company)} | {norm_company(h.company) for h in a.registry_hits} \
        | {norm_company(h.brand) for h in a.registry_hits if h.brand}
    names.discard("")
    same = cn and any(cn == n or cn in n or n in cn for n in names)
    by_domain = str(ctx.get("lookup", "")).startswith("domains:")
    src = f"Crustdata company record ({ctx.get('lookup')}, context only)"
    if cn and not same and by_domain:
        a.flags.append(f"Identity conflict: Crustdata says {c.domain} belongs to '{ctx.get('name')}', not '{c.company}'.")
        return GateResult(gate="entity_identity", value=GV.UNKNOWN, source=src,
                          reason=f"Domain owner per Crustdata is '{ctx.get('name')}'; conflicts with candidate name.")
    if same and ig.value == GV.UNKNOWN and by_domain and a.registry_hits \
            and not any("impersonation" in f for f in a.flags):
        ig = GateResult(gate="entity_identity", value=GV.PASS, source=src,
                        reason=f"Registry entity matches the domain owner per Crustdata ({ctx.get('name')}, HQ {ctx.get('hq') or 'n/a'}).")
    elif same:
        ig.reason += f" Corroborated by Crustdata ({ctx.get('name')})."
    hc = ctx.get("headcount")
    tmax = int(cfg.limits.get("crustdata", {}).get("trader_headcount_max", 20))
    if hc is not None and hc <= tmax and "manufactur" in (c.listing_role or "").lower() and a.role != Role.MAKER:
        a.flags.append(f"Trader-credibility signal: ~{hc} employees per Crustdata while claiming to manufacture (signal only).")
    country = (ctx.get("country") or "").lower()
    if country and country not in ("india", "ind", "in") and any(INDIA_RE.search(h.address) for h in a.registry_hits):
        a.flags.append(f"Context note: Crustdata HQ country '{ctx.get('country')}' differs from Indian registry address.")
    return ig


def assess(c: Candidate, spec: Spec, cfg: Config, regs: Registries, store: Store, today: date,
           llm=None, context: Optional[dict] = None) -> Assessment:
    a = Assessment(candidate_id=c.candidate_id, company=c.company, model=c.model)
    lane = cfg.lanes[spec.lane]
    gcfg = cfg.gates

    # 1. registry first, before any site evidence
    legal, role, hits, flags, maker = registry_check(c, spec, cfg, regs, today)
    a.registry_hits, a.flags, a.maker_of_record = hits, list(flags), maker

    # 2. prompt injection: logged, never obeyed (no code path reads page text as instructions)
    for s in find_injections(c.pages, gcfg.get("injection_patterns", [])):
        a.flags.append(f"Prompt injection ignored: \"{s[:120]}\"")
        store.audit("prompt_injection_ignored", candidate=c.candidate_id, sentence=s[:300])

    # 3. role: registry decides maker/brand; site text can only downgrade to trader
    text = normalize(" ".join(p.text for p in c.pages))
    trader_hit = next((m for m in gcfg.get("trader_markers", []) if m in text), None)
    if role == Role.UNKNOWN and trader_hit:
        role = Role.TRADER
    a.role = role
    allowed = set(cfg.priorities.get("allowed_roles", ["MAKER", "BRAND_OWNER"]))
    needs_maker = cfg.priorities.get("buyer_requires_maker", True)
    if role.value in allowed:
        rg = GateResult(gate="supplier_role", value=GV.PASS, reason=f"{role.value}" + (f"; maker of record {maker}" if maker and role != Role.MAKER else ""),
                        source=hits[0].title if hits else "")
    elif role == Role.TRADER:
        rg = GateResult(gate="supplier_role", value=GV.FAIL if needs_maker else GV.PASS,
                        reason=f"Listing says '{c.listing_role or 'n/a'}' but site indicates resale (\"{trader_hit}\") and no registry entry. "
                               + ("Buyer requires a maker." if needs_maker else "Kept as reseller."),
                        source=c.pages[0].url if c.pages else "")
    else:
        rg = GateResult(gate="supplier_role", value=GV.UNKNOWN, reason="Role not settled by registry or site.")

    # 4. identity: registry name (+address) appears on the candidate's own site
    if hits and c.pages:
        h = hits[0]
        named = norm_company(h.company) in norm_company(text) or (h.brand and norm_company(h.brand) in norm_company(text))
        city = (h.address.split(",")[-2].strip().lower() if h.address.count(",") >= 2 else "")
        ok = named and (role != Role.MAKER or not city or city in text or norm_company(c.company) in norm_company(text))
        ig = GateResult(gate="entity_identity", value=GV.PASS if ok and not flags else GV.UNKNOWN,
                        reason="Registry name/address matches official site." if ok and not flags else "Registry entity not confirmed on this site.",
                        source=c.pages[0].url)
    elif not c.pages:
        ig = GateResult(gate="entity_identity", value=GV.UNKNOWN, reason="Official site not fetched.")
        a.missing_evidence.append("official site page")
    else:
        ig = GateResult(gate="entity_identity", value=GV.UNKNOWN, reason="No registry record to match against.")
    ig = apply_company_context(a, c, context, ig, cfg)

    # 5. spec match: verbatim passages only
    fields = lane.get("required_fields", {})
    claims = [verify_claim(cl, c.pages) for cl in c.claims]
    for cl in claims:
        if not cl.verified:
            store.audit("claim_rejected", candidate=c.candidate_id, field=cl.field, passage=cl.passage[:200])
            a.flags.append(f"Claim rejected ({cl.field}): passage not verbatim in stored page")
    verified = {cl.field: cl for cl in claims if cl.verified}
    for cl in extract_claims(c.pages, fields):
        verified.setdefault(cl.field, cl)
    missing = [f for f in fields if f not in verified]
    if missing and llm is not None and c.pages:  # optional paid fallback, capped
        out = llm.json_call("claim_extract", "Extract passages supporting these fields: "
                            + ", ".join(f"{k} ({fields[k]['label']})" for k in missing),
                            CLAIMS_SCHEMA, untrusted=" ".join(p.text for p in c.pages))
        for raw in (out or {}).get("claims", []):
            cl = verify_claim(Claim(**raw), c.pages)  # LLM output gets the same verbatim check
            if cl.verified and cl.field in missing:
                verified.setdefault(cl.field, cl)
    spec_gates = []
    for f, fc in fields.items():
        cl = verified.get(f)
        if cl is None:
            spec_gates.append(GateResult(gate=f"spec:{f}", value=GV.UNKNOWN, reason=f"{fc['label']}: no verbatim evidence."))
        elif cl.value == "contradicted":
            spec_gates.append(GateResult(gate=f"spec:{f}", value=GV.FAIL, reason=f"{fc['label']}: contradicted.", source=cl.url, passage=cl.passage))
        else:
            spec_gates.append(GateResult(gate=f"spec:{f}", value=GV.PASS, reason=fc["label"], source=cl.url, passage=cl.passage))
    vals = [g.value for g in spec_gates]
    if not vals:
        sv = GV.NA
    elif GV.FAIL in vals:
        sv = GV.FAIL
    elif all(v == GV.PASS for v in vals):
        sv = GV.PASS
    elif GV.PASS in vals:
        sv = GV.PARTIAL
    else:
        sv = GV.UNKNOWN
    sg = GateResult(gate="spec_match", value=sv, reason=f"{vals.count(GV.PASS)}/{len(vals)} required fields evidenced verbatim.")

    # 6. geography
    addr = " ".join(h.address for h in hits)
    geo_ok = bool(INDIA_RE.search(text) or INDIA_RE.search(addr))
    geo = GateResult(gate="geography", value=GV.PASS if geo_ok else GV.UNKNOWN,
                     reason="India service/registry address evidenced." if geo_ok else "Service coverage not evidenced.")

    # 7. commercial: confirmations, never failures
    cap = GateResult(gate="capacity", value=GV.TO_CONFIRM, reason="Supplier reply needed.")
    price = GateResult(gate="price_lead_stock", value=GV.TO_CONFIRM, reason="Quote needed.")

    a.gates = [ig, rg, *legal, sg, *spec_gates, geo, cap, price]
    a.decision = decide(a, spec, cfg)
    return a


def decide(a: Assessment, spec: Spec, cfg: Config) -> Decision:
    g = {x.gate: x.value for x in a.gates}
    needs_maker = cfg.priorities.get("buyer_requires_maker", True)
    legal = g.get("legal_approval")
    if any("impersonation" in f or f.startswith("Identity conflict") for f in a.flags):
        return Decision.HOLD_FOR_REVIEW
    if legal == GV.FAIL or g.get("supplier_role") == GV.FAIL or g.get("spec_match") == GV.FAIL \
            or g.get("entity_identity") == GV.FAIL:
        return Decision.NOT_A_FIT
    if not cfg.lanes[spec.lane].get("legal_required") and a.missing_evidence and g.get("spec_match") in (GV.UNKNOWN, GV.NA):
        return Decision.CANNOT_VERIFY
    blocking = g.get("entity_identity") == GV.UNKNOWN or (g.get("supplier_role") == GV.UNKNOWN and needs_maker)
    soft_unknown = g.get("spec_match") in (GV.UNKNOWN, GV.PARTIAL) or g.get("geography") == GV.UNKNOWN
    if blocking or soft_unknown:
        return Decision.PROVISIONAL
    if a.quote and quote_supports_order(a.quote, spec, cfg):
        return Decision.RECOMMEND_ORDER
    return Decision.QUALIFIED_FOR_RFQ


def quote_supports_order(q: Quote, spec: Spec, cfg: Config) -> bool:
    ok_labels = set(ORDER_LABELS) | ({PriceLabel.TEST_FIXTURE} if cfg.limits.get("mode") == "demo" else set())
    if q.label not in ok_labels or q.unit_price is None:
        return False
    if spec.quantity and (q.quantity or 0) < spec.quantity:
        return False
    if q.validity_until and q.validity_until < date.today().isoformat():
        return False
    return True


def apply_quote(a: Assessment, q: Quote, spec: Spec, cfg: Config) -> None:
    a.quote = q
    for g in a.gates:
        if g.gate == "capacity" and q.quantity and spec.quantity and q.quantity >= spec.quantity:
            g.value, g.reason, g.source = GV.CONFIRMED, f"Supplier quoted {q.quantity} units.", q.label.value
        if g.gate == "price_lead_stock" and q.label in ORDER_LABELS | {PriceLabel.TEST_FIXTURE} and q.unit_price is not None:
            g.value = GV.CONFIRMED
            g.reason = f"{q.currency} {q.unit_price}/unit, delivery {q.delivery_days if q.delivery_days is not None else 'UNKNOWN'} days."
            g.source = q.label.value
    a.decision = decide(a, spec, cfg)


def rank(assessments: list[Assessment], cfg: Config) -> list[Assessment]:
    """Group by decision state; inside a group order by buyer priorities. Missing values tie, never invent a gap."""
    order = cfg.gates.get("decision_order", [d.value for d in Decision])
    keys = cfg.priorities.get("priority_order", [])
    desc = {"warranty_months"}

    def kv(a: Assessment):
        q = a.quote
        out = []
        for k in keys:
            v = getattr(q, k, None) if q else None
            if isinstance(v, str):
                v = len(v)  # e.g. service network description length is not meaningful: treat as present
            out.append((1, 0) if v is None else (0, -v if k in desc else v))
        return out

    ranked = sorted(assessments, key=lambda a: (order.index(a.decision.value), kv(a)))
    for i, a in enumerate(ranked, 1):
        a.rank = i
        a.tied_with = [b.candidate_id for b in ranked if b is not a and b.decision == a.decision
                       and kv(b) == kv(a) and a.decision in (Decision.QUALIFIED_FOR_RFQ, Decision.RECOMMEND_ORDER)]
    return ranked
