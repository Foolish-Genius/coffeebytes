"""Evidence: verbatim validator, deterministic claim extraction, prompt-injection detection, page fetch."""
from __future__ import annotations

import hashlib
import html
import re
import urllib.request
from typing import Optional

from .models import Claim, Page
from .store import Store, now


def normalize(s: str) -> str:
    s = html.unescape(s or "").replace(" ", " ")
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip().lower()


def verify_claim(claim: Claim, pages: list[Page]) -> Claim:
    """A claim stands only if its passage appears verbatim (after whitespace/entity normalization) in a stored page."""
    p = normalize(claim.passage)
    for pg in pages:
        if (not claim.url or claim.url == pg.url) and p and p in normalize(pg.text):
            return claim.model_copy(update={"verified": True, "reason": "verbatim match", "url": pg.url})
    return claim.model_copy(update={"verified": False, "reason": "passage not found verbatim in stored page"})


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]


def extract_claims(pages: list[Page], fields: dict) -> list[Claim]:
    """Free extraction: the passage is the page sentence itself, so it is verbatim by construction."""
    out: list[Claim] = []
    for name, f in fields.items():
        for pg in pages:
            hit = None
            for s in sentences(pg.text):
                low = s.lower()
                if any(re.search(p, low) for p in f.get("patterns", [])):
                    hit = Claim(field=name, value="supported", passage=s, url=pg.url)
                    break
                if any(re.search(p, low) for p in f.get("contradict", [])):
                    hit = hit or Claim(field=name, value="contradicted", passage=s, url=pg.url)
            if hit:
                out.append(hit)
                break
    return out


def find_injections(pages: list[Page], patterns: list[str]) -> list[str]:
    found = []
    for pg in pages:
        for s in sentences(pg.text):
            if any(re.search(p, s.lower()) for p in patterns):
                found.append(s)
    return found


def fetch(url: str, store: Store, timeout: int = 15, ua: str = "SiliconScout") -> Optional[Page]:
    """Fetch once, store the text first (data, never instructions), then cite from the stored copy."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(2_000_000).decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001 - any fetch failure becomes missing evidence
        store.audit("fetch_failed", url=url, error=str(e)[:200])
        return None
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    sha = hashlib.sha256(text.encode()).hexdigest()
    (store.dir / "pages" / f"{sha[:16]}.txt").write_text(f"{url}\n{now()}\n\n{text}", encoding="utf-8")
    store.audit("fetched", url=url, sha256=sha[:16], chars=len(text))
    return Page(url=url, text=text, fetched_at=now(), sha256=sha[:16])
