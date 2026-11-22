"""Crustdata company enrichment (optional, behind a flag).

Used ONLY as company context: identity resolution (domain, HQ, headcount) and a trader-credibility signal.
Never evidence of a model approval or of manufacturing capability - the registry gate stays authoritative.

API (docs.crustdata.com, version 2025-11-01): POST https://api.crustdata.com/company/enrich
  headers: Authorization: Bearer <CRUSTDATA_API_KEY>, x-api-version: 2025-11-01
  body: {"domains": [...]} or {"names": [...]}  (one identifier type, max 25), "fields": [...]
  response: [{matched_on, match_type, matches: [{confidence_score, company_data}]}]
Credit hygiene: one batched call per identifier type per job, results checkpointed with the job and cached
across jobs (data/cache) so a restart or a repeat sourcing event never pays twice for the same company.
The key is read from the environment / .env only and is never written to logs, ledger or checkpoints.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from .models import Candidate
from .store import Store

URL = "https://api.crustdata.com/company/enrich"
API_VERSION = "2025-11-01"
BATCH = 25


def _http_post(body: dict, key: str, timeout: int) -> tuple[list, Optional[str]]:
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "x-api-version": API_VERSION, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()), r.headers.get("X-Credits-Used")


def compact(company_data: dict) -> dict:
    """Keep only the context fields we use, so checkpoints and the UI stay small."""
    b = company_data.get("basic_info") or {}
    loc = company_data.get("locations") or {}
    hc = company_data.get("headcount") or {}
    return {
        "name": b.get("name"), "primary_domain": b.get("primary_domain"), "website": b.get("website"),
        "year_founded": b.get("year_founded"), "employee_count_range": b.get("employee_count_range"),
        "headcount": hc.get("total"), "company_type": b.get("company_type"),
        "hq": loc.get("headquarters"), "country": loc.get("country"),
        "profile_url": b.get("professional_network_url"),
    }


class Crustdata:
    def __init__(self, limits: dict, store: Store, cache_dir: Path,
                 post: Optional[Callable[[dict, str, int], tuple[list, Optional[str]]]] = None):
        self.cfg = limits.get("crustdata", {}) or {}
        self.store = store
        self.cache_path = Path(cache_dir) / "crustdata_cache.json"
        self.post = post or _http_post

    @property
    def key(self) -> str:
        return os.environ.get("CRUSTDATA_API_KEY", "")

    def available(self) -> bool:
        en = self.cfg.get("enabled", "auto")
        return not (en is False or str(en).lower() == "false") and bool(self.key)

    # -- cache ------------------------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, cache: dict) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")

    def _fresh(self, entry: dict) -> bool:
        ttl = int(self.cfg.get("cache_days", 30))
        return datetime.fromisoformat(entry["at"]) > datetime.now(timezone.utc) - timedelta(days=ttl)

    # -- enrich -----------------------------------------------------------------------------------
    def enrich(self, candidates: list[Candidate]) -> dict[str, dict]:
        """Returns {candidate_id: context}. Context has matched=False when Crustdata has no record."""
        if not self.available():
            self.store.audit("crustdata_skipped", reason="no CRUSTDATA_API_KEY or disabled")
            return {}
        cache, out = self._load(), {}
        want: dict[str, list[tuple[str, str]]] = {"domains": [], "names": []}
        for c in candidates:
            kind, ident = ("domains", c.domain.lower()) if c.domain else ("names", c.company)
            ck = f"{kind}:{ident.lower()}"
            if ck in cache and self._fresh(cache[ck]):
                out[c.candidate_id] = {**cache[ck]["ctx"], "lookup": ck, "cached": True}
            else:
                want[kind].append((c.candidate_id, ident))
        max_records = int(self.cfg.get("max_records_per_job", 25))
        spent = 0
        for kind, items in want.items():
            for i in range(0, len(items), BATCH):
                chunk = items[i:i + BATCH][: max(0, max_records - spent)]
                if not chunk:
                    self.store.audit("crustdata_cap", kind=kind, skipped=len(items) - i)
                    break
                body = {kind: [ident for _, ident in chunk],
                        "fields": self.cfg.get("fields", ["basic_info", "locations", "headcount"])}
                if kind == "domains":
                    body["exact_match"] = True
                t0 = time.time()
                try:
                    data, used = self.post(body, self.key, int(self.cfg.get("timeout_s", 20)))
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                    msg = str(e)[:200].replace(self.key, "***") if self.key else str(e)[:200]
                    self.store.audit("crustdata_error", kind=kind, error=msg)
                    continue
                matched = 0
                for (cid, ident), row in zip(chunk, data if isinstance(data, list) else []):
                    m = sorted(row.get("matches") or [], key=lambda x: -(x.get("confidence_score") or 0))
                    ctx = {"matched": bool(m), **(compact(m[0]["company_data"]) if m else {}),
                           "confidence": m[0].get("confidence_score") if m else None}
                    matched += bool(m)
                    ck = f"{kind}:{ident.lower()}"
                    cache[ck] = {"at": datetime.now(timezone.utc).isoformat(), "ctx": ctx}
                    out[cid] = {**ctx, "lookup": ck, "cached": False}
                spent += len(chunk)
                credits = float(used) if used else 2.0 * matched
                self.store.ledger("crustdata", f"company_enrich:{kind}", credits * float(self.cfg.get("inr_per_credit", 0)),
                                  int((time.time() - t0) * 1000), credits=credits, records=len(chunk), matched=matched)
        self._save(cache)
        return out
