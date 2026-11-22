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
streamlit run app.py                      # internal operator workbench (web app: see below)
```

Restart-safety demo: `python -m siliconscout run "..." --job k --kill-after discover`, then `python -m siliconscout run --job k`. The job resumes from the checkpoint. An RFQ already marked sent is never sent again. A send whose outcome is unknown is held for a person (`release --key ... [--sent]`).

## Web app (sign-in, credits, Stripe test mode)

The customer-facing app lives in `api/` (FastAPI, wraps `siliconscout.pipeline.Job`) and `web/` (React + Vite). It shows the same `to_output()` JSON as the CLI: need, derived spec and assumptions, candidates grouped by decision state with every gate (value, reason, source, verbatim passage, date), RFQ drafts with approve and send, quote intake, the decision summary, the cost ledger and the audit log. `app.py` (Streamlit) stays as the internal operator workbench.

```bash
pip install -r requirements.txt
cd web && npm install && npm run build && cd ..      # builds web/dist, which FastAPI serves
py -3.13 -m uvicorn api.main:create_app --factory --port 8000
# open http://localhost:8000, then create an account, buy credits and run a job
```

For frontend hot reload, run `npm run dev` in `web/` (port 5173, proxies `/api` to port 8000) next to the uvicorn server.

- **Accounts.** Email and password (scrypt-hashed, http-only session cookie), or Google. Every job belongs to one user, and other users get a 404.
- **Google sign-in.** In Google Cloud Console, go to APIs & Services, then Credentials, and create an OAuth client ID of type "Web application". Add the authorized redirect URI `http://localhost:8000/api/auth/google/callback` (or `<PUBLIC_BASE_URL>/api/auth/google/callback`). Put `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` in `.env` and restart, and the "Continue with Google" button appears. Only Google-verified emails are accepted. If an email already has a password account, signing in with Google links to it and removes the unverified password.
- **Admin vs users.** The emails in `ADMIN_EMAILS` are admins, but only when signed in with Google. Password sign-up and sign-in are refused for those emails, so nobody can claim them first. Admins have unlimited credits (jobs are never debited), can open any job, and get an Admin page with all users, payments and jobs, plus manual credit adjustments. Everyone else starts at 0 credits and only gets credits from Stripe payments or an admin grant.
- **Credits.** 1 credit per sourcing job. Packs: 10 for ₹499, 50 for ₹1,999, 200 for ₹6,999. The credit is debited when the run starts and refunded automatically if the engine fails. Approving RFQs and entering quotes cost nothing. The Credits page shows the balance and full history.
- **Stripe (test mode only).** Put `STRIPE_SECRET_KEY=sk_test_...` and `STRIPE_WEBHOOK_SECRET=whsec_...` in `.env` (see `.env.example`). Live keys are refused. Checkout sends the user to Stripe (test card `4242 4242 4242 4242`). The signed webhook `POST /api/stripe/webhook` (`checkout.session.completed`) adds the credits. Run it locally with `stripe listen --forward-to localhost:8000/api/stripe/webhook`. If the webhook can't reach you, the return page confirms the session with Stripe instead. Either path credits a session exactly once, and only if the amount matches the pack.
- **Data.** Users, sessions, credits and job ownership are in `jobs/_web/app.sqlite` (gitignored; override with `SS_WEB_DB`). Each job's engine state stays in `jobs/<id>/`.
- **Crustdata.** Web jobs only do a dry-run plan (no credits spent) unless `SS_WEB_CRUSTDATA_SPEND=1`.
- **RFQ "send".** It uses the engine's idempotent outbox. In this build that writes `.eml` files to `jobs/<id>/outbox/`, and no email leaves the machine.

## Deploy (Render, auto-deploys on every push to main)

`render.yaml` (Blueprint) and `Dockerfile` (React build + Python runtime) define one web service with a 1 GB persistent disk for the SQLite data. In Render, go to **New → Blueprint** and pick this repo. After that, every push or merge to `main` rebuilds and redeploys automatically (`autoDeploy: true`, `branch: main`). Secrets are entered in Render's dashboard, never in git: `PUBLIC_BASE_URL`, `ADMIN_EMAILS`, `GOOGLE_CLIENT_ID/SECRET`, `STRIPE_SECRET_KEY` (sk_test_ only), `STRIPE_WEBHOOK_SECRET`, `CRUSTDATA_API_KEY`, `ANTHROPIC_API_KEY`. After the first deploy, add `<PUBLIC_BASE_URL>/api/auth/google/callback` to the Google OAuth client's redirect URIs, and point a Stripe test-mode webhook (`checkout.session.completed`) at `<PUBLIC_BASE_URL>/api/stripe/webhook`. The disk needs a paid instance (Starter); on the free plan, users and credits would be wiped on every deploy.

## Data: fixtures vs. live

All rows in `data/registries/*.csv` and `data/fixtures/*` are **fictional TEST_FIXTURE data**, used for the demo and the tests. At kickoff:

1. Parse the real ARAI and ICAT AIS-140 lists (PDF to CSV with columns `company,model,ref,valid_until,address,brand`), hand-check 10 rows, then freeze them:
   `python -m siliconscout registry-import --name arai_ais140 --csv arai.csv --as-on 2026-07-16 --source-url <url>`
2. Live discovery: `--source registry` seeds candidates from the approval lists themselves at no cost. `--source file --candidates live.json` fetches official pages for a searched list (`[{company, domain, model, email, urls: [...]}]`; see `examples/live_candidates.json`).
3. Component lane: set `MOUSER_API_KEY` for live distributor stock and price. Without it, the fixture is used and labelled TEST_FIXTURE.
4. Set `mode: live` in `config/limits.yaml`.

## API keys (never committed)

Copy `.env.example` to `.env` and paste your keys there. `.env` is gitignored, and keys are never written to logs, the ledger or checkpoints.

| Key | Enables | Without it |
|---|---|---|
| `CRUSTDATA_API_KEY` | Company context: who owns the domain, HQ, headcount, year founded. It can corroborate identity, raise an identity conflict (which sends the candidate to HOLD_FOR_REVIEW), or add a trader-credibility signal. **Never evidence of approval or capability.** | Identity comes from the registry and the site only |
| `ANTHROPIC_API_KEY` | Claude fallback for routing and spec extraction | Rules only (free) |
| `MOUSER_API_KEY` | Live distributor stock and price | Fixture prices |

Crustdata uses `POST /company/enrich` (API version `2025-11-01`). All candidates go in **one batched request** per job (up to 25). Results are checkpointed and cached for 30 days, so a restart or a repeat lookup costs no credits. A job can use at most `max_records_per_job` × 2 credits. Credits are logged in the cost ledger.

## Cost controls

- The deterministic path (routing, registry match, regex claim extraction, gates, ranking) makes **zero paid calls**. A test asserts this.
- Claude is called only when rules can't route a need, or when a required spec field has no regex hit. Each call is capped by `max_calls_per_job`, `max_inr_per_job` and `max_page_chars`. Untrusted page text is wrapped in `<untrusted>` tags, and LLM claims pass the same verbatim check as everything else.
- Every paid call writes one JSONL line to `jobs/<id>/cost_ledger.jsonl` (provider, tokens, INR estimate, duration).

See `docs/BUILD_LOG_AND_TOKEN_USAGE.md` for how this was built and how token and credit use was kept low.

## Not in scope (deliberately)
Multi-line BOMs, ECAD/SPICE, monitoring/PCN alerts, legal due diligence, tariff engines, autonomous negotiation/PO/payment, ERP, multi-user, PDF styling.
