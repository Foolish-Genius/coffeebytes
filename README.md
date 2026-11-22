# SiliconScout 3.0: ET sourcing agent MVP

SiliconScout turns a logistics company's operating need into a compliant electronics purchase. It finds the real makers of the device, checks them against India's government approval registries, and recommends an order only when a real quote backs it.

> Directories tell you who says they make trackers. We tell you whose model holds a government approval, and who actually makes it.

## What it does

```
Operating need -> Need-to-ET spec -> Which lane?
   device lane:    Discover makers -> REGISTRY CHECK -> Site evidence (verbatim) -> Gates & states -> RFQ (human-approved) -> Quotes -> Decision
   component lane: Exact MPN -> Authorized-distributor channel (stock, price) -> Decision
Every stage: checkpoint, outbox for external actions, cost ledger, audit log
```

| Piece | Where |
|---|---|
| Need-to-spec translation and lane routing (rules first, LLM only if rules can't route) | `siliconscout/spec.py` |
| Registry snapshots: dated, sha256-hashed, matched on company **and** model | `siliconscout/registry.py` |
| Verbatim evidence validator, injection detection, page fetch | `siliconscout/evidence.py` |
| Gates, decision states, ranking without fake scores | `siliconscout/gates.py` |
| RFQ drafts, quote normalization, component lane | `siliconscout/rfq.py` |
| State machine, restart safety, human approval | `siliconscout/pipeline.py` |
| SQLite checkpoints, outbox with idempotency keys, audit, JSONL cost ledger | `siliconscout/store.py` |
| Optional cost-capped Claude fallback | `siliconscout/llm.py` |
| CLI (JSON out) / Streamlit workbench | `siliconscout/cli.py`, `app.py` |
| Config surface (edit these, not the code) | `config/lane.yaml`, `gates.yaml`, `priorities.yaml`, `limits.yaml`, `output_schema.json` |

**Decision states:** `NOT_A_FIT` · `HOLD_FOR_REVIEW` · `PROVISIONAL` · `QUALIFIED_FOR_RFQ` · `RECOMMEND_ORDER` · `CANNOT_VERIFY`.
**Price labels:** only `SUPPLIER_QUOTE` and `DISTRIBUTOR_LIVE` can support an order. `PUBLIC_BUDGETARY` never can, and `TEST_FIXTURE` never can in `mode: live`.

## Quick start

```bash
pip install -r requirements.txt
python -m pytest -q                       # 24 tests, incl. all 12 hidden cases from the brief
python -m siliconscout run "Need GPS trackers for 300 trucks, deliver to Pune" --job demo
python -m siliconscout approve demo       # human approval -> RFQs sent through the outbox
python -m siliconscout quote demo --file examples/quote_c01.json
python -m siliconscout run "Need 500 pcs of part number FSC-MCU100A" --job parts
streamlit run app.py                      # the workbench
```

Restart-safety demo: `python -m siliconscout run "..." --job k --kill-after discover`, then `python -m siliconscout run --job k`. The job resumes from the checkpoint. An RFQ already marked sent is never sent again. A send whose outcome is unknown is held for a person (`release --key ... [--sent]`).

## Data: fixtures vs. live

All rows in `data/registries/*.csv` and `data/fixtures/*` are **fictional TEST_FIXTURE data**, used for the demo and the tests. At kickoff:

1. Parse the real ARAI and ICAT AIS-140 lists (PDF to CSV with columns `company,model,ref,valid_until,address,brand`), hand-check 10 rows, then freeze them:
   `python -m siliconscout registry-import --name arai_ais140 --csv arai.csv --as-on 2026-07-16 --source-url <url>`
2. Live discovery: `--source registry` seeds candidates from the approval lists themselves at no cost. `--source file --candidates live.json` fetches official pages for a searched list (`[{company, domain, model, email, urls: [...]}]`; see `examples/live_candidates.json`).
3. Component lane: set `MOUSER_API_KEY` for live distributor stock and price. Without it, the fixture is used and labelled TEST_FIXTURE.
4. Set `mode: live` in `config/limits.yaml`.

## Cost controls

- The deterministic path (routing, registry match, regex claim extraction, gates, ranking) makes **zero paid calls**. A test asserts this.
- Claude is called only when rules can't route a need, or when a required spec field has no regex hit. Each call is capped by `max_calls_per_job`, `max_inr_per_job` and `max_page_chars`. Untrusted page text is wrapped in `<untrusted>` tags, and LLM claims pass the same verbatim check as everything else.
- Every paid call writes one JSONL line to `jobs/<id>/cost_ledger.jsonl` (provider, tokens, INR estimate, duration).

See `docs/BUILD_LOG_AND_TOKEN_USAGE.md` for how this was built and how token and credit use was kept low.

## Not in scope (deliberately)
Multi-line BOMs, ECAD/SPICE, monitoring/PCN alerts, legal due diligence, tariff engines, autonomous negotiation/PO/payment, ERP, multi-user, PDF styling.
