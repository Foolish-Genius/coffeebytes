"""Registry snapshots (ARAI/ICAT AIS-140, BIS CRS, distributor lists): dated, hashed, matched by company AND model."""
from __future__ import annotations

import csv
import hashlib
import io
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import RegistryHit

SUFFIXES = r"\b(private|pvt|limited|ltd|llp|inc|co|company|india|the)\b"


def norm_company(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    s = re.sub(SUFFIXES, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_model(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


@dataclass
class Snapshot:
    name: str
    title: str
    as_on: str
    source_url: str
    label: str
    sha256: str
    rows: list[dict]

    def hit(self, row: dict) -> RegistryHit:
        return RegistryHit(registry=self.name, title=self.title, as_on=self.as_on, sha256=self.sha256[:16],
                           label=self.label, company=row.get("company", ""), model=row.get("model", ""),
                           ref=row.get("ref", ""), valid_until=row.get("valid_until", ""),
                           address=row.get("address", ""), brand=row.get("brand", ""))

    def by_model(self, model: str) -> list[dict]:
        m = norm_model(model)
        return [r for r in self.rows if m and norm_model(r.get("model", "")) == m]


class Registries:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "registries"
        self.manifest = yaml.safe_load((self.dir / "manifest.yaml").read_text(encoding="utf-8")) or {}
        self._cache: dict[str, Snapshot] = {}

    def get(self, name: str) -> Snapshot:
        if name not in self._cache:
            meta = self.manifest[name]
            raw = (self.dir / meta["file"]).read_bytes()
            rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
            self._cache[name] = Snapshot(name, meta.get("title", name), str(meta.get("as_on", "")),
                                         meta.get("source_url", ""), meta.get("label", "OFFICIAL"),
                                         hashlib.sha256(raw).hexdigest(), rows)
        return self._cache[name]

    def authorized(self, manufacturer: str) -> set[str]:
        if "authorized_distributors" not in self.manifest:
            return set()
        snap = self.get("authorized_distributors")
        return {r["distributor"] for r in snap.rows if norm_company(r["manufacturer"]) == norm_company(manufacturer)}


def import_csv(data_dir: Path, name: str, csv_path: Path, as_on: str, source_url: str, title: str = "") -> str:
    """Freeze a parsed registry file into the snapshot store with its as-on date. Returns sha256."""
    d = Path(data_dir) / "registries"
    dest = d / f"{name}.csv"
    shutil.copyfile(csv_path, dest)
    man_path = d / "manifest.yaml"
    man = yaml.safe_load(man_path.read_text(encoding="utf-8")) or {}
    man[name] = {"title": title or name, "file": dest.name, "as_on": as_on, "source_url": source_url, "label": "OFFICIAL"}
    man_path.write_text(yaml.safe_dump(man, sort_keys=False), encoding="utf-8")
    return hashlib.sha256(dest.read_bytes()).hexdigest()
