"""Loads the five config files. Code reads config; the hour-3 change edits config only."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Read KEY=value lines from .env (gitignored) without overriding real env vars. No dependency."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if v:
                os.environ.setdefault(k.strip(), v)


def _y(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


@dataclass
class Config:
    lanes: dict
    not_mpn: list
    gates: dict
    priorities: dict
    limits: dict
    output_schema: dict
    root: Path = ROOT
    data_dir: Path = field(default=ROOT / "data")
    jobs_dir: Path = field(default=ROOT / "jobs")

    @classmethod
    def load(cls, config_dir: Path | None = None, jobs_dir: Path | None = None) -> "Config":
        if not os.environ.get("SS_NO_DOTENV"):
            load_dotenv()
        d = Path(config_dir or os.environ.get("SS_CONFIG_DIR", ROOT / "config"))
        lane = _y(d / "lane.yaml")
        return cls(
            lanes=lane["lanes"],
            not_mpn=[s.upper() for s in lane.get("not_mpn", [])],
            gates=_y(d / "gates.yaml"),
            priorities=_y(d / "priorities.yaml"),
            limits=_y(d / "limits.yaml"),
            output_schema=json.loads((d / "output_schema.json").read_text(encoding="utf-8")),
            jobs_dir=Path(jobs_dir or os.environ.get("SS_JOBS_DIR", ROOT / "jobs")),
        )
