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
        self.deadline: Optional[float] = None

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

    # -- global budget (all jobs) -----------------------------------------------------------------
    @property
    def _budget_path(self) -> Path:
        return self.cache_path.parent / "crustdata_budget.json"

    def _budget_load(self) -> dict:
        try:
            return json.loads(self._budget_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _budget_save(self, b: dict) -> None:
        self._budget_path.parent.mkdir(parents=True, exist_ok=True)
        self._budget_path.write_text(json.dumps(b), encoding="utf-8")

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
            # reserved/test TLDs (RFC 2606) are rejected by Crustdata with a 400 -> look up by name instead
            skip = self.cfg.get("skip_tlds", ["example", "test", "invalid", "localhost"])
            good_domain = c.domain and c.domain.lower().rsplit(".", 1)[-1] not in skip
            kind, ident = ("domains", c.domain.lower()) if good_domain else ("names", c.company)
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
        if self.deadline and time.time() > self.deadline:  # R9 time limit
            self.store.audit("time_limit_skipped", op="crustdata")
            return out
        reserve = float(self.cfg.get("reserve_credits", 50))
        bal = self.balance()
        if bal is None or bal < plan["worst_case_credits"] + reserve:
            self.store.audit("crustdata_spend_blocked", balance=bal, needed=plan["worst_case_credits"], reserve=reserve)
            return out
        # Global budget across ALL jobs: min(max_total_credits, fraction x starting balance).
        budget = self._budget_load()
        if budget.get("baseline") is None:
            budget["baseline"] = bal
            self._budget_save(budget)
        spent = max(float(budget.get("spent", 0)), budget["baseline"] - bal)  # own count OR real balance drop
        limit = float(self.cfg.get("max_total_credits", 1000))
        frac = self.cfg.get("max_fraction_of_balance")
        if frac:
            limit = min(limit, float(frac) * budget["baseline"])
        remaining = limit - spent
        per = plan["credits_per_match"]
        allowed = int(max(0.0, remaining) / per + 1e-9)
        if allowed < plan["to_send"]:
            flat = [(k, i) for k, v in plan["batches"].items() for i in v][:allowed]
            plan["batches"] = {k: [i for kk, i in flat if kk == k] for k in plan["batches"]}
            plan["batches"] = {k: v for k, v in plan["batches"].items() if v}
            plan["skipped_over_budget"] = plan["to_send"] - allowed
            plan["to_send"] = allowed
            plan["worst_case_credits"] = round(allowed * per, 2)
        plan["budget"] = {"limit": limit, "spent": round(spent, 2), "remaining": round(remaining, 2)}
        self.store.audit("crustdata_budget", **plan["budget"], sending=plan["to_send"])
        if not plan["to_send"]:
            self.store.audit("crustdata_spend_blocked", reason="global budget exhausted", **plan["budget"])
            return out
        cache = self._load()
        ids = {(k, ident): c.candidate_id for k in plan["batches"] for c in candidates
               for ident in (c.domain.lower(), c.company) if ident and ident in plan["batches"][k]}
        result = {"ok_requests": 0, "failed_requests": 0, "matched": 0, "credits": 0.0, "errors": []}
        self.last_plan["result"] = result
        timeout = int(self.cfg.get("timeout_s", 20))
        for kind, idents in plan["batches"].items():
            for i in range(0, len(idents), BATCH):
                chunk = idents[i:i + BATCH]
                body: dict = {kind: chunk, "exact_match": True}  # domains: exact primary domain; names: exact name
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
                    if status == 400 and kind == "domains":  # drop the domains it names as invalid, retry the rest once
                        msg = str(((data or {}).get("error") or {}).get("message", "")) if isinstance(data, dict) else ""
                        bad = {d.strip().lower() for d in msg.split(":", 1)[-1].split(",")} if "Invalid company domains" in msg else set()
                        rest = [d for d in chunk if d not in bad]
                        if bad and rest:
                            self.store.audit("crustdata_invalid_domains_dropped", dropped=sorted(bad))
                            chunk, body[kind] = rest, rest
                            status, data, hdr = self.http("POST", f"/company/{self.mode}", body, self.key, timeout)
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                    self.store.audit("crustdata_error", kind=kind, error=self._scrub(str(e))[:200])
                    result["failed_requests"] += 1
                    result["errors"].append(type(e).__name__)
                    continue
                if status != 200:
                    err = self._scrub(json.dumps(data))[:300] if data else ""
                    self.store.audit("crustdata_http_error", status=status, error=err)
                    result["failed_requests"] += 1
                    result["errors"].append(f"HTTP {status}: {err[:160]}")
                    if status in (401, 402, 403):  # bad key / no credits / not allowed: stop, never retry
                        self._save(cache)
                        return out
                    continue
                result["ok_requests"] += 1
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
                b = self._budget_load()
                b["spent"] = round(float(b.get("spent", 0)) + credits, 4)
                self._budget_save(b)
                result["matched"] += matched
                result["credits"] = round(result["credits"] + credits, 4)
                self.store.ledger("crustdata", f"company_{self.mode}:{kind}",
                                  credits * float(self.cfg.get("inr_per_credit", 0)),
                                  int((time.time() - t0) * 1000), credits=credits, records=len(chunk), matched=matched)
        self._save(cache)
        return out
