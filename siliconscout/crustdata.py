"""Crustdata company context (optional). SPEND-LOCKED BY DEFAULT.

Used ONLY as company context: identity resolution (domain owner, size, HQ) and a trader-credibility signal.
Never evidence of a model approval or of manufacturing capability - the registry gate stays authoritative.

Docs (docs.crustdata.com, API version 2025-11-01), all requests carry
  authorization: Bearer <CRUSTDATA_API_KEY>, x-api-version: 2025-11-01
- POST /company/identify  {"domains"|"names": [...<=25], "exact_match"?}  0.2 credits per MATCHED identifier,
  unmatched free. Returns name, primary_domain, website, employee_count_range, profile url (no HQ, no headcount).
- POST /company/enrich    same body + "fields"   2 credits per record. Adds locations (HQ, country), headcount.
- GET  /account/credits   free. {"account": {"credits": <balance>, ...}}
- Errors: 402 out of credits (never retry) · 401 bad key · 403 endpoint not allowed · 429 rate limit,
  wait X-RateLimit-Reset seconds.

Credit safety, in order:
1. allow_spend (config, default false) - when false NO network call is made; a dry-run plan with the
   worst-case credit estimate is logged and shown instead. Cached results (free) are still used.
2. max_credits_per_job caps the worst case; identifiers beyond it are skipped, never sent.
3. Before spending, the free balance check must show balance >= estimate + reserve_credits.
4. One batched request per identifier type (<=25 per request); results checkpointed + cached 30 days.
5. 402 stops all further calls for the job; 429 retries once after the reset header (max 10 s).
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

BASE = "https://api.crustdata.com"
API_VERSION = "2025-11-01"
BATCH = 25
COST = {"identify": 0.2, "enrich": 2.0}  # credits per matched record (docs)

# http(method, path, body, key, timeout) -> (status, json_or_none, headers_dict)
Http = Callable[[str, str, Optional[dict], str, int], tuple[int, object, dict]]


def _http(method: str, path: str, body: Optional[dict], key: str, timeout: int) -> tuple[int, object, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "authorization": f"Bearer {key}", "x-api-version": API_VERSION, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null"), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"null")
        except ValueError:
            payload = None
        return e.code, payload, {k.lower(): v for k, v in (e.headers or {}).items()}


def _range_upper(r: Optional[str]) -> Optional[int]:
    """'1-10' -> 10, '10001+' -> None (large). Used only when exact headcount is absent."""
    if not r or "+" in r:
        return None
    try:
        return int(str(r).split("-")[-1].replace(",", ""))
    except ValueError:
        return None


def compact(company_data: dict) -> dict:
    """Keep only the context fields we use, so checkpoints and the UI stay small."""
    b = company_data.get("basic_info") or {}
    loc = company_data.get("locations") or {}
    hc = company_data.get("headcount") or {}
    total = hc.get("total")
    return {
        "crustdata_company_id": company_data.get("crustdata_company_id") or b.get("crustdata_company_id"),
        "name": b.get("name"), "primary_domain": b.get("primary_domain"), "website": b.get("website"),
        "year_founded": b.get("year_founded"), "employee_count_range": b.get("employee_count_range"),
        "headcount": total if total is not None else _range_upper(b.get("employee_count_range")),
        "headcount_is_range": total is None and b.get("employee_count_range") is not None,
        "company_type": b.get("company_type"), "hq": loc.get("headquarters"), "country": loc.get("country"),
        "profile_url": b.get("professional_network_url"),
    }


class Crustdata:
    def __init__(self, limits: dict, store: Store, cache_dir: Path, http: Optional[Http] = None,
                 allow_spend: Optional[bool] = None):
        self.cfg = limits.get("crustdata", {}) or {}
        self.store = store
        self.cache_path = Path(cache_dir) / "crustdata_cache.json"
        self.http = http or _http
        self.allow_spend = bool(self.cfg.get("allow_spend", False)) if allow_spend is None else allow_spend
        self.mode = self.cfg.get("mode", "identify")
        self.last_plan: dict = {}

    @property
    def key(self) -> str:
        return os.environ.get("CRUSTDATA_API_KEY", "")

    def available(self) -> bool:
        en = self.cfg.get("enabled", "auto")
        return not (en is False or str(en).lower() == "false") and bool(self.key)

    def _scrub(self, s: str) -> str:
        return s.replace(self.key, "***") if self.key else s

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

    # -- free calls -------------------------------------------------------------------------------
    def balance(self) -> Optional[float]:
        """GET /account/credits - free per Crustdata docs. Returns None if unavailable."""
        if not self.key:
            return None
        status, data, _ = self.http("GET", "/account/credits", None, self.key, int(self.cfg.get("timeout_s", 20)))
        if status != 200 or not isinstance(data, dict):
            self.store.audit("crustdata_balance_error", status=status)
            return None
        bal = (data.get("account") or {}).get("credits")
        self.store.audit("crustdata_balance", credits=bal)
        return float(bal) if bal is not None else None

    # -- plan + spend -----------------------------------------------------------------------------
    def plan(self, candidates: list[Candidate]) -> tuple[dict[str, dict], dict]:
        """No network. Returns (cached contexts, plan with worst-case credit estimate)."""
        cache, out = self._load(), {}
        want: dict[str, list[tuple[str, str]]] = {"domains": [], "names": []}
        for c in candidates:
            kind, ident = ("domains", c.domain.lower()) if c.domain else ("names", c.company)
            ck = f"{self.mode}:{kind}:{ident.lower()}"
            if ck in cache and self._fresh(cache[ck]):
                out[c.candidate_id] = {**cache[ck]["ctx"], "lookup": ck, "cached": True}
            else:
                want[kind].append((c.candidate_id, ident))
        cap = float(self.cfg.get("max_credits_per_job", 5))
        per = COST.get(self.mode, 2.0)
        n_cap = int(cap / per + 1e-9)  # not cap // per: 0.6 // 0.2 == 2.0 in floating point
        sendable = {k: v for k, v in want.items() if v}
        flat = [(k, x) for k, v in sendable.items() for x in v]
        keep, skipped = flat[:n_cap], flat[n_cap:]
        batches = {k: [x for kk, x in keep if kk == k] for k in sendable}
        plan = {"mode": self.mode, "endpoint": f"POST /company/{self.mode}", "credits_per_match": per,
                "cached_free": len(out), "to_send": len(keep), "skipped_over_cap": len(skipped),
                "requests": sum(-(-len(v) // BATCH) for v in batches.values() if v),
                "worst_case_credits": round(len(keep) * per, 2), "cap_credits": cap,
                "batches": {k: [i for _, i in v] for k, v in batches.items() if v}}
        self.last_plan = plan
        return out, plan

    def enrich(self, candidates: list[Candidate]) -> dict[str, dict]:
        """Returns {candidate_id: context}. Spends credits ONLY when allow_spend is true."""
        if not self.available():
            self.store.audit("crustdata_skipped", reason="no CRUSTDATA_API_KEY or disabled")
            return {}
        out, plan = self.plan(candidates)
        if not plan["to_send"]:
            self.store.audit("crustdata_all_cached", n=len(out))
            return out
        if not self.allow_spend:
            self.store.audit("crustdata_dry_run", **{k: v for k, v in plan.items() if k != "batches"})
            return out
        reserve = float(self.cfg.get("reserve_credits", 50))
        bal = self.balance()
        if bal is None or bal < plan["worst_case_credits"] + reserve:
            self.store.audit("crustdata_spend_blocked", balance=bal, needed=plan["worst_case_credits"], reserve=reserve)
            return out
        cache = self._load()
        ids = {(k, ident): cid for k in plan["batches"] for cid, ident in
               [(c.candidate_id, (c.domain.lower() if c.domain else c.company)) for c in candidates]
               if ident in plan["batches"][k]}
        timeout = int(self.cfg.get("timeout_s", 20))
        for kind, idents in plan["batches"].items():
            for i in range(0, len(idents), BATCH):
                chunk = idents[i:i + BATCH]
                body: dict = {kind: chunk}
                if kind == "domains":
                    body["exact_match"] = True
                if self.mode == "enrich":
                    body["fields"] = self.cfg.get("fields", ["basic_info", "locations", "headcount"])
                t0 = time.time()
                try:
                    status, data, hdr = self.http("POST", f"/company/{self.mode}", body, self.key, timeout)
                    if status == 429:
                        wait = min(float(hdr.get("x-ratelimit-reset", 5) or 5), 10.0)
                        self.store.audit("crustdata_rate_limited", wait_s=wait)
                        time.sleep(wait)
                        status, data, hdr = self.http("POST", f"/company/{self.mode}", body, self.key, timeout)
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                    self.store.audit("crustdata_error", kind=kind, error=self._scrub(str(e))[:200])
                    continue
                if status != 200:
                    self.store.audit("crustdata_http_error", status=status,
                                     error=self._scrub(json.dumps(data))[:300] if data else "")
                    if status in (401, 402, 403):  # bad key / no credits / not allowed: stop, never retry
                        self._save(cache)
                        return out
                    continue
                matched = 0
                for ident, row in zip(chunk, data if isinstance(data, list) else []):
                    m = sorted(row.get("matches") or [], key=lambda x: -(x.get("confidence_score") or 0))
                    ctx = {"matched": bool(m), **(compact(m[0]["company_data"]) if m else {}),
                           "confidence": m[0].get("confidence_score") if m else None}
                    matched += bool(m)
                    ck = f"{self.mode}:{kind}:{ident.lower()}"
                    cache[ck] = {"at": datetime.now(timezone.utc).isoformat(), "ctx": ctx}
                    cid = ids.get((kind, ident))
                    if cid:
                        out[cid] = {**ctx, "lookup": ck, "cached": False}
                used = hdr.get("x-credits-used")
                credits = float(used) if used else COST.get(self.mode, 2.0) * matched
                self.store.ledger("crustdata", f"company_{self.mode}:{kind}",
                                  credits * float(self.cfg.get("inr_per_credit", 0)),
                                  int((time.time() - t0) * 1000), credits=credits, records=len(chunk), matched=matched)
        self._save(cache)
        return out
