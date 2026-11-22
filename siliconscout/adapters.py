"""Thin adapters: candidate discovery and authorized-distributor offers. Fixture, file and live sources."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Optional

from .config import Config
from .evidence import fetch
from .models import Candidate, Page, Spec
from .registry import Registries, norm_company
from .store import Store, now


def discover(spec: Spec, cfg: Config, store: Store, source: str = "fixture",
             candidates_file: Optional[Path] = None) -> list[Candidate]:
    """source = fixture | file | registry.
    file: JSON list of {company, domain, model, email, urls[]} from a live search; pages are fetched and stored.
    registry: seeds candidates from the lane's approval lists themselves (free, authoritative), no pages yet."""
    lim = cfg.limits.get("discovery", {})
    out: list[Candidate] = []
    if source == "fixture":
        f = cfg.data_dir / "fixtures" / f"candidates_{spec.lane}.json"
        if f.exists():
            out = [Candidate(**c) for c in json.loads(f.read_text(encoding="utf-8"))["candidates"]]
    elif source == "file" and candidates_file:
        fcfg = cfg.limits.get("fetch", {})
        for i, c in enumerate(json.loads(Path(candidates_file).read_text(encoding="utf-8"))):
            pages = []
            for u in c.get("urls", [])[: int(fcfg.get("max_pages_per_candidate", 2))]:
                p = fetch(u, store, int(fcfg.get("timeout_s", 15)), fcfg.get("user_agent", "SiliconScout"))
                if p:
                    pages.append(p)
            out.append(Candidate(candidate_id=f"c{i + 1:02d}", company=c["company"], domain=c.get("domain", ""),
                                 model=c.get("model", ""), listing_role=c.get("listing_role", ""),
                                 email=c.get("email", ""), pages=pages, source="live"))
    elif source == "registry":
        regs, seen = Registries(cfg.data_dir), set()
        for name in cfg.lanes[spec.lane].get("registries", []):
            for r in regs.get(name).rows:
                k = norm_company(r["company"])
                if k not in seen:
                    seen.add(k)
                    out.append(Candidate(candidate_id=f"c{len(out) + 1:02d}", company=r["company"],
                                         model=r["model"], source=f"registry:{name}"))
    out = out[: int(lim.get("max_candidates", 12))]
    store.audit("discovered", source=source, n=len(out), min_expected=lim.get("min_candidates", 8))
    return out


# --- component lane -----------------------------------------------------------------------------

def _mouser(mpn: str, store: Store) -> Optional[dict]:
    """Live Mouser part search (needs MOUSER_API_KEY). Not exercised by tests."""
    key = os.environ.get("MOUSER_API_KEY")
    if not key:
        return None
    t0 = time.time()
    body = json.dumps({"SearchByPartRequest": {"mouserPartNumber": mpn}}).encode()
    req = urllib.request.Request(f"https://api.mouser.com/api/v1/search/partnumber?apiKey={key}", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        store.audit("distributor_error", provider="mouser", error=str(e)[:200])
        return None
    store.ledger("mouser", "part_search", 0.0, int((time.time() - t0) * 1000), credits=1)
    parts = (data.get("SearchResults") or {}).get("Parts") or []
    if not parts:
        return None
    p = parts[0]
    breaks = [[int(b["Quantity"]), float(re.sub(r"[^\d.]", "", b["Price"]))] for b in p.get("PriceBreaks", [])]
    stock = int(re.sub(r"\D", "", p.get("Availability", "") or "0") or 0)
    return {"manufacturer": p.get("Manufacturer", ""), "label": "DISTRIBUTOR_LIVE",
            "offers": [{"distributor": "Mouser Electronics", "stock": stock, "packaging": "",
                        "pack_multiple": int(p.get("Mult", 1) or 1),
                        "currency": (p.get("PriceBreaks") or [{}])[0].get("Currency", ""),
                        "price_breaks": breaks, "lead_time_days": None}]}


def distributor_offers(mpn: str, cfg: Config, store: Store) -> tuple[Optional[dict], str]:
    live = _mouser(mpn, store)
    if live:
        live["fetched_at"] = now()
        return live, "DISTRIBUTOR_LIVE"
    f = cfg.data_dir / "fixtures" / "distributor_offers.json"
    parts = json.loads(f.read_text(encoding="utf-8")).get("parts", {}) if f.exists() else {}
    part = parts.get(mpn.upper())
    if part:
        part = {**part, "fetched_at": now()}
    return part, "TEST_FIXTURE"
