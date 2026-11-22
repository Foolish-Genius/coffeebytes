# Build log and token/credit usage

This covers two kinds of spend:

1. **Build-time spend**: the Claude Code session that wrote this repo from the 13-page MVP brief.
2. **Run-time spend**: what SiliconScout itself costs per sourcing job.

---

## 1. What was built

All in one session, from `SiliconScout 3.0 — ET Sourcing Agent MVP.pdf`:

| Brief requirement | Status | File |
|---|---|---|
| Need-to-spec translation with explicit assumptions | Done | `siliconscout/spec.py`, `config/lane.yaml` |
| Lane routing, device vs component (+ generic fallback) | Done | `spec.py` |
| Discovery of 8-12 candidates | Fixture, registry-seeded and URL-list adapters done. No paid search API wired. | `siliconscout/adapters.py` |
| Registry checks (AIS-140 ARAI/ICAT, either passes), dated and hashed | Done, on fictional fixture rows | `siliconscout/registry.py`, `data/registries/` |
| Maker vs brand vs trader classification | Done | `siliconscout/gates.py` |
| Verbatim evidence validator, untrusted content | Done | `siliconscout/evidence.py` |
| Gates, decision states, ranking without fake scores | Done | `gates.py`, `config/gates.yaml`, `config/priorities.yaml` |
| RFQ draft, human approval, send state, quote intake | Done (file outbox; SMTP not wired) | `siliconscout/rfq.py`, `pipeline.py` |
| Component lane with authorized-distributor stock/price | Done; Mouser live adapter written but **not exercised** (no key) | `adapters.py`, `rfq.py` |
| Audit log, cost ledger, restart safety (checkpoint + idempotent outbox) | Done | `siliconscout/store.py` |
| One workbench + CLI emitting the same JSON | Done | `app.py`, `siliconscout/cli.py` |
| The 12 hidden-case tests | All pass (24 tests total) | `tests/test_hidden_cases.py` |

Still open for kickoff: parse the real ARAI/ICAT PDFs into CSV and hand-check them, wire live search and SMTP, get a Mouser key, and set `mode: live`.

---

## 2. How build-time tokens were kept low

Credits in a coding session go mostly on **input context**: every file read, tool output and long log is re-sent on each later turn. The tactics below targeted that.

| Tactic | What it avoided |
|---|---|
| Read the PDF once, in 2 chunks (pp 1-7, 8-13), and never again | Re-reading 13 page images each time a detail was needed |
| Wrote every file in one pass with `Write`, never read it back | Read-after-write round trips: the tool keeps file state, so a re-read is pure waste |
| Wrote independent files in parallel batches (config, fixtures, modules) | One model turn per file; each turn re-sends the whole context |
| Small fixes with `Edit` (a few lines), not whole-file rewrites | Re-emitting 200-line files as output tokens |
| Rules and regex first, so the code needs no LLM to run the tests | Burning API credits on every test run; tests are free and deterministic |
| Output trimmed at the source (`pytest -q`, `Select-Object -Last N`, `Select-String`, a scratch `peek.py` that prints one line per candidate) | Dumping 30-50 KB JSON or full pip logs into context |
| Heavy install (Streamlit) run in the background while other files were written | Idle waiting turns |
| No subagents spawned | Each subagent starts cold and re-reads everything (the brief, the code) |
| Loaded one reference skill (Claude API) once, for the exact SDK call shape, then wrote code | Trial-and-error API calls; drift-related bugs |
| Fixture data designed so one candidate exercises each hidden test | A second fixture set for every test |
| The 24 tests passed on the first run, so there was no debug loop | Debug loops are the largest hidden cost: each iteration re-sends all context |

**Rough shape of the session** (estimated, not metered; the app's own usage view, `/explain-usage`, gives exact numbers):

- Biggest single input: the 13 PDF pages as images, read once.
- Biggest output: about 1,600 lines of code, config, tests and docs.
- Tool calls: about 35. Only one real bug-fix iteration happened (a shared-list aliasing bug that let an ignored prompt injection lower an identity gate), and it was fixed with a one-line `Edit`.

### Further savings for the next session

- Start a **fresh session** for follow-up work, pointing at this file and `README.md`, instead of continuing a long thread. A long thread re-sends its whole history on every turn.
- Ask for **targeted changes by file** ("in `gates.py`, add X"), not "review everything".
- Run `python -m pytest -q` yourself and paste only the failures.
- Keep real registry CSVs out of the chat. The code reads them from disk, and only their hashes and as-on dates need to appear.

---

## 3. Run-time cost of SiliconScout (per sourcing job)

The product is built so that **the default path costs ₹0 in model spend**:

| Step | Method | Paid? |
|---|---|---|
| Lane routing, quantity, location | Keyword scoring + regex | No |
| Registry match | Exact normalized company + model lookup in a hashed CSV | No |
| Maker / brand / trader | Registry fields + site markers in `gates.yaml` | No |
| Spec claims | Regex over page sentences; the passage is verbatim by construction | No |
| Gates, decision, ranking | Pure Python | No |
| Spec translation when rules can't route | 1 Claude call, structured JSON, `effort: low` | **Yes, optional** |
| Missing spec field after regex | 1 Claude call per candidate, page text capped at 6,000 chars | **Yes, optional** |
| Live distributor price | Mouser API | API credit, logged |

**Guards** (`config/limits.yaml`): `max_calls_per_job: 6`, `max_inr_per_job: 50`, `max_page_chars: 6000`, `max_tokens: 1024`, `effort: low`. When the cap is hit, optional calls are skipped and logged (`llm_skipped`), and the deterministic result stands.

**Measurement:** each paid call writes one line to `jobs/<id>/cost_ledger.jsonl` with `provider, op, model, tokens_in, tokens_out, inr_estimate, duration_ms`. The JSON output's `cost` block sums it. The test `test_no_paid_calls_on_deterministic_path` asserts that the fixture run makes zero paid calls.

**Cost per job** (formula from the brief):
`model spend + search/data credits + infrastructure + human review minutes × hourly cost`.
Report the measured ledger figure from the demo run. Don't show an illustrative number as if it were measured.

**Upper bound with all 6 LLM calls** at the default model (Claude Opus 5.5: $4 / $20 per MTok, about ₹340 / ₹1,700 per MTok in `limits.yaml`): 6 × (~2,000 in + ~500 out) ≈ 12k in + 3k out ≈ **₹4 + ₹5 ≈ ₹9 per job**, well under the ₹50 cap and under 0.2% of a ₹4,999 fee. To push it lower, set `llm.model: claude-haiku-5-5` in `limits.yaml`. That is a quality trade-off, so measure it on real pages before switching.
