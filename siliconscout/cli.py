"""CLI. Every command prints the same JSON the workbench shows."""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from .config import Config
from .output import to_output, validate
from .pipeline import Crash, Job
from .registry import import_csv


def _print(job: Job) -> None:
    out = to_output(job)
    errs = validate(out, job.cfg.output_schema)
    if errs:
        out["contract_errors"] = errs
    print(json.dumps(out, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="siliconscout", description="ET sourcing agent: need -> compliant purchase decision")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="start or resume a job")
    r.add_argument("need", nargs="?", help="operating need, e.g. 'GPS trackers for 300 trucks, deliver to Pune'")
    r.add_argument("--job", default=None)
    r.add_argument("--source", choices=["fixture", "file", "registry"], default="fixture")
    r.add_argument("--candidates", type=Path, help="JSON list of live candidates with urls (source=file)")
    r.add_argument("--kill-after", help="simulate a crash after this stage (demo of restart safety)")
    r.add_argument("--crustdata-spend", action="store_true", help="allow Crustdata credit spend for THIS run only")
    cx = sub.add_parser("context", help="Crustdata: show the dry-run plan (no spend) or, with --spend, fetch")
    cx.add_argument("job")
    cx.add_argument("--spend", action="store_true", help="actually call Crustdata (spends credits, capped)")
    fu = sub.add_parser("ask", help="follow-up on a job: '500 units', 'require IP67', 'why Orbitrack?', 'proceed'")
    fu.add_argument("job")
    fu.add_argument("text")
    rv = sub.add_parser("review", help="supply-chain review of a purchase-history CSV (no paid calls)")
    rv.add_argument("--csv", type=Path, required=True)
    sub.add_parser("crustdata-balance", help="free GET /account/credits balance check (no credits used)")
    a = sub.add_parser("approve", help="human approval of RFQ drafts")
    a.add_argument("job")
    a.add_argument("--rfq", default="all", help="'all' or comma-separated rfq ids")
    a.add_argument("--by", default="operator")
    q = sub.add_parser("quote", help="enter a supplier quote (JSON file)")
    q.add_argument("job")
    q.add_argument("--file", type=Path, required=True)
    s = sub.add_parser("status", help="print job JSON")
    s.add_argument("job")
    h = sub.add_parser("release", help="resolve an ambiguous send after checking the mailbox")
    h.add_argument("job")
    h.add_argument("--key", required=True)
    h.add_argument("--sent", action="store_true", help="it did go out (otherwise allow one resend)")
    i = sub.add_parser("registry-import", help="freeze a parsed registry CSV with its as-on date")
    i.add_argument("--name", required=True)
    i.add_argument("--csv", type=Path, required=True)
    i.add_argument("--as-on", required=True)
    i.add_argument("--source-url", required=True)
    i.add_argument("--title", default="")
    args = p.parse_args(argv)

    cfg = Config.load()
    if args.cmd == "registry-import":
        print(import_csv(cfg.data_dir, args.name, args.csv, args.as_on, args.source_url, args.title))
        return 0
    if args.cmd == "crustdata-balance":
        from .crustdata import Crustdata
        from .store import Store
        print(json.dumps({"credits": Crustdata(cfg.limits, Store(cfg.jobs_dir, "_account"), cfg.jobs_dir / "_cache").balance()}))
        return 0
    if args.cmd == "review":
        from .review import load_rows, review
        print(json.dumps(review(load_rows(args.csv), cfg), indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "ask":
        job = Job(args.job, cfg)
        print(job.follow_up(args.text), file=sys.stderr)
        _print(job)
        return 0
    if args.cmd == "context":
        job = Job(args.job, cfg, crust_spend=args.spend or None)
        plan = job.refresh_context()
        print(json.dumps({k: v for k, v in plan.items()}, indent=2), file=sys.stderr)
        _print(job)
        return 0
    if args.cmd == "run":
        job = Job(args.job or uuid.uuid4().hex[:8], cfg, crash_at=args.kill_after,
                  crust_spend=True if args.crustdata_spend else None)
        try:
            job.run(args.need, args.source, args.candidates)
        except Crash as e:
            print(f"Simulated kill after stage '{e}'. Re-run: python -m siliconscout run --job {job.state.job_id}", file=sys.stderr)
            return 137
    else:
        job = Job(args.job, cfg)
        if args.cmd == "approve":
            ids = "all" if args.rfq == "all" else args.rfq.split(",")
            print("approved:", job.approve(ids, args.by), file=sys.stderr)
            job.run()
        elif args.cmd == "quote":
            job.add_quote(json.loads(args.file.read_text(encoding="utf-8")))
        elif args.cmd == "release":
            job.store.release_ambiguous(args.key, args.sent)
            for rq in job.state.rfqs:
                if rq.idempotency_key == args.key:
                    rq.status = "SENT" if args.sent else "APPROVED"
            job.state.extra["done"] = [d for d in job.state.extra.get("done", []) if d not in ("rfq_send", "decide")]
            job.store.checkpoint(job.state)
            job.run()
    _print(job)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
