# Procurement spend questions

A finance analyst asks questions in plain English about the 2025 invoices, the vendor master and the global procurement policy. Every answer shows what it is based on: the policy clauses it quotes, the invoice rows behind each figure, and the ECB euro reference rates behind every converted amount.

- **Backend:** FastAPI (Python 3.13), pandas, OpenAI Responses API with structured outputs
- **Frontend:** React 19 + TypeScript + Vite, types generated from the backend's OpenAPI schema
- **Evidence sources (the only ones):** `data/invoices_2025_clean.csv`, `data/vendor_master.csv`, `data/procurement_policy.md`, and the ECB euro foreign exchange reference rates for [USD](https://data.ecb.europa.eu/data/datasets/EXR/EXR.D.USD.EUR.SP00.A) and [GBP](https://data.ecb.europa.eu/data/datasets/EXR/EXR.D.GBP.EUR.SP00.A) from the ECB Data Portal

Design and decisions are in [`system_design.md`](system_design.md) (requirements) and [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) (how it was built).
The descriptions of the final implementation and testing are in ['docs/ARCHITECTURE.md'](docs/ARCHITECTURE.md) (what has been built).

---

## Run it

### 1. Backend

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Put the OpenAI key in the repo-root `.env` (read only by the backend):

```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5-mini
```

```bash
.venv/bin/uvicorn app.main:app --reload
```

On first start the app fetches and validates the two ECB reference-rate series in the background and caches them under `var/fx_cache/ecb/`. `/summary` works immediately with original-currency figures; EUR consolidation appears once the rates are loaded (a few seconds). Later starts use the cache and only refetch when it is older than 24 hours.

### 2. Frontend

Needs Node 20+. On this machine Node lives in the project-local `.tools/node` (created with `conda create -p .tools/node -c conda-forge nodejs=22`); use your own Node if you have one.

```bash
cd frontend
PATH="$PWD/../.tools/node/bin:$PATH" npm install
PATH="$PWD/../.tools/node/bin:$PATH" npm run dev
```

Open http://localhost:5173. Vite forwards `/api/*` to the backend at `http://127.0.0.1:8000` (override with `BACKEND_URL`).

### 3. Tests and evals

```bash
cd backend && .venv/bin/pytest -q
```

```bash
cd frontend && PATH="$PWD/../.tools/node/bin:$PATH" npm test
```

```bash
cd backend && .venv/bin/python -m evals.run_golden
```

The backend and frontend tests run offline: the model is replaced by scripted outputs, and the ECB rates come from captured fixture responses. The golden evals call the real model (about 5 minutes, a few cents); see [Quality](#quality).

---

## What the analyst sees

- **Summary panel (loads on startup from `GET /summary`):** spend by original currency reconciled to a consolidated EUR total under a double rule, the ECB rate basis (and how many weekend/holiday invoices used an earlier rate), spend by vendor status, the largest vendors, data-quality counts (missing PO, missing approver, credit notes, vendors that aren't ACTIVE) and example questions. If rates are missing, original-currency totals stay and the EUR total is replaced by an explanation — never a partial sum.
- **Each answer** opens with a fixed status sentence and a **Based on** strip with three slots — *Procurement policy*, *Invoice and vendor records*, *ECB reference rates* — each shown as used and verified (solid, ✓), used with limits (hatched, !) or not used (dashed), in words as well as shape.
- **Evidence tabs:** *Result* (recalculated figures + original-currency breakdown), *Policy clauses* (verbatim quotes), *Invoices* (every supporting row; missing PO/approver flagged), *Exchange rates* (per invoice date: ECB rate date used and why, rate values, source and snapshot), *How it was checked* (why these sources, each recalculation, the generated query, the trace reference).
- **States:** summary loading skeleton and retry; per-question progress with elapsed time and Cancel; network, timeout, busy and server errors with *Ask again* / *Edit the question*; conversation kept across reloads.
- High contrast: every text colour is ≥ 7:1 (WCAG AAA); Atkinson Hyperlegible Next; full keyboard use; axe-clean.

---

## How a question is answered

```
question ─► 1 Router (GPT, structured)      which sources? in scope? reporting currency? EUR filter? exact-date?
            │   + app rules                  policy+records ⇒ add ECB rates; cross-currency totals ⇒ add ECB rates
            ├─► out of scope ───────────────► fixed message, no evidence
            ├─► 2 Policy (GPT)               answer from the 21 clauses only; every quote must be found
            │                                verbatim in its clause (one repair round) or the answer is "no evidence"
            └─► 3 Records / ECB rates
                 a GPT writes one pandas function from the schema (never sees rows)
                 b static checks: no imports, I/O, private attrs, query/eval; columns must exist
                 c runs in a separate Python process: restricted builtins, no network, CPU + time limits,
                   JSON-only output; plus "probe" runs for FX completeness
                 d app recalculates every declared total/count/mean in Decimal from the supporting invoices
                 e one repair round with the exact error, then QUERY_FAILED with guidance
                 f GPT explains using {{row:column}} placeholders; the app fills in the verified values
            ─► 4 Compose: status, Based-on evidence, limitations, caveats  ─► trace written
```

Each step (prompt version, model, raw output, parsed decision, generated code, checks, timings, tokens) is appended to `var/traces/YYYY-MM-DD.jsonl` under the answer's trace id. `GET /traces/{id}` returns it in dev mode; the UI shows the reference on every answer.

---

## Currency conventions (read before relying on converted figures)

**ECB euro reference rates, as the policy requires.** Policy FIN-POL-014 §5.1 says non-EUR invoices are converted at the **ECB reference rate on the invoice date** for reporting. The app uses exactly those rates: the ECB Data Portal series [EXR.D.USD.EUR.SP00.A](https://data.ecb.europa.eu/data/datasets/EXR/EXR.D.USD.EUR.SP00.A) and [EXR.D.GBP.EUR.SP00.A](https://data.ecb.europa.eu/data/datasets/EXR/EXR.D.GBP.EUR.SP00.A). Every converted answer and the summary state this basis. (Until 14 Nov 2026 the prototype used Federal Reserve H.10 rates; the source was switched at the user's direction.)

**Formula.** ECB rates quote units of the foreign currency per 1 EUR. With X(EUR)=1 and every value from the **same** observation date:

| Conversion | Formula | Series needed |
|---|---|---|
| USD → EUR | amount ÷ USD per EUR | USD |
| GBP → EUR | amount ÷ GBP per EUR | GBP |
| EUR → USD | amount × USD per EUR | USD |
| GBP → USD | amount × USD per EUR ÷ GBP per EUR | both, same date |
| (general) | amount × X(target) ÷ X(source) | |

**Precision and rounding.** `Decimal`, 28 significant digits, rates used exactly as published (USD 4 dp, GBP 5 dp), no intermediate rounding (a cross rate is never rounded on its own). Each invoice is rounded once to 0.01 with ROUND_HALF_UP; totals are sums of the rounded invoice amounts, so a total always equals the rows shown. Money crosses the API as decimal strings.

**Weekends and TARGET holidays — a prototype convention, not an ECB or policy rule.** The ECB publishes no reference rates on weekends or TARGET holidays (1 Jan, Good Friday, Easter Monday, 1 May, 25 and 26 Dec). For such invoice dates the latest *earlier* ECB rate is used, at most 7 calendar days back, with the same date for every currency involved; a future rate is never used. §5.1 asks for the rate *on the invoice date* and does not say what to do on these days, so these conversions are shown with their actual rate date and flagged for review. In the 2025 data: 35 invoices convert at the invoice date, 16 fall back (all weekends except Good Friday, at most 2 days), 89 are already in EUR. An explicit "exact invoice-date rates only" request disables the fallback.

**Missing rates are never hidden.** If any invoice behind a converted figure has no acceptable rate, that figure is shown as *unavailable* (null), the affected invoices are listed with the missing evidence, and review is recommended; original-currency amounts stay available. Generated queries cannot hide this by dropping or filtering those rows: the app re-runs the query on probe copies in which the rate-less rows look like ordinary converted rows, and any it would include make the converted figure unavailable.

**Rate evidence.** Only the two allow-listed ECB Data Portal API requests are made (by application code; generated code has no network). Each response is validated (series key, ISO dates on weekdays only, increasing and unique, published decimals, plausibility band, final status, and every ECB publication day in the invoice window present) and stored unchanged as an immutable snapshot (`var/fx_cache/ecb/<series>/<snapshot_id>.csv` + metadata) with SHA-256 integrity checks. Every converted result cites the series key and page, API URL, observation date, requested date, rate values, formula and method, and snapshot id.

## API

| Endpoint | Purpose |
|---|---|
| `POST /ask` `{question, reporting_currency?}` | Answer + evidence. HTTP 200 for every handled outcome, with `status` ∈ `ANSWERED`, `PARTIAL`, `NO_EVIDENCE`, `OUT_OF_SCOPE`, `QUERY_FAILED`, `ERROR`. 422 for invalid input, 503 + `Retry-After` when `MAX_CONCURRENT_ASKS` questions are already running. |
| `GET /summary` | Deterministic overview (no model call), cached until the ECB rate snapshot changes. |
| `GET /health` | Data, policy, ECB rates and model configuration status. |
| `GET /fx/status` | Active snapshots, last refresh error, invoice coverage. |
| `POST /fx/refresh` | Refetch ECB rates now (dev mode only). |
| `GET /traces/{id}` | Full decision trajectory (dev mode only). |

The OpenAPI schema is at `/docs`. After changing response models, regenerate the frontend types: export `create_app().openapi()` to `frontend/src/api/openapi.json`, then `npm run gen:api`.

### Configuration (environment or `.env`)

| Variable | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | — | Required for `/ask`; `/summary` works without it |
| `OPENAI_MODEL` | `gpt-4.1` | Any Responses-API model with structured outputs |
| `OPENAI_REASONING_EFFORT` | `low` | Sent only to reasoning models (`gpt-5*`, `o*`); empty disables |
| `OPENAI_TIMEOUT_S` | `60` | Per model call |
| `MAX_CONCURRENT_ASKS` | `4` | Questions processed at once |
| `FX_REFRESH_ON_STARTUP` | `true` | Background ECB rate refresh at startup when needed |
| `FX_MAX_AGE_HOURS` | `24` | Cache age that triggers a startup refresh |
| `DEV_MODE` | `true` | Exposes `/traces/{id}` and `POST /fx/refresh`; **set `false` outside local development** |
| `DATA_DIR`, `VAR_DIR` | `data/`, `var/` | Inputs and runtime state (caches, traces, eval reports) |

---

## Quality

- **Backend tests** (`backend/tests`, offline): data validation, policy clause parsing and quote verification, ECB parser (including TARGET holidays)/cache/client, every conversion and fallback rule (including a weekday holiday and a one-leg-missing cross rate), Decimal rounding, `/summary` against an independent stdlib reference to the cent, FX-down modes, the sandbox, verification and repair loops, the probe-based completeness check, hybrid questions, API status codes and security headers.
- **Frontend tests** (`frontend/src/test`): formatting, the chat reducer, the Based-on logic, summary loading/error/FX-down states, answer rendering, tabs and keyboard, cancel/retry/busy, and axe accessibility checks.
- **Golden evals** (`backend/evals`): 32 questions with expected values computed by an independent reference (`evals/reference.py`, stdlib + Decimal, own fallback logic): the three example questions, totals, counts, EUR-reporting vs EUR-denominated, USD reporting, quarter filters, exact-date requests, hybrid policy + data, policy boundaries, out-of-scope, prompt injection and unanswerable questions. The run passes at ≥ 95%. Latest result: see `var/evals/` (summary below).

| Run (UTC) | Rates | Model | Passed | Wall time (4 workers) |
|---|---|---|---|---|
| 20261114T054909Z | ECB reference rates | `gpt-5-mini` | 32/32 | 173 s |
| 20261114T052231Z | Federal Reserve H.10 (before the switch) | `gpt-5-mini` | 32/32 | 168 s |
| 20261114T052547Z | Federal Reserve H.10 (before the switch) | `gpt-5-mini` | 32/32 | 184 s |

Cases by category: data 16, hybrid 2, policy 8, out_of_scope 2, adversarial 2, unsupported 2. Per-question latency: median 20 s, max 55 s (data questions 16–55 s, policy 6–13 s, out of scope ~3 s).

---

## Known limitations

- **Latency:** 5–60 s per answer (separate model calls for routing, policy, code generation and explanation). Progress is shown as typical stages, not streamed.
- **Single questions:** follow-ups ("and for Q2?") are answered on their own; the conversation is not sent as context.
- **The model can still misread a question.** The app guarantees that shown numbers match the data and the cited rates, and that quotes are verbatim, but it cannot guarantee the query matches the intent (e.g. which invoices count as "above the threshold"). The *How it was checked* tab shows the generated query and filters for review.
- **Sandbox memory limit** is not enforceable on macOS (CPU time and wall-clock limits are).
- **Weekend and TARGET-holiday invoices** use the latest earlier ECB rate (a prototype convention §5.1 does not cover); they are flagged for review.
- `DEV_MODE` defaults to on for local work; turn it off before exposing the service.

## Layout

```
backend/app/       config, api (routes, schemas), data (CSV load/validate), policy (clauses),
                   fx (ECB client, parser, cache, conversion, enrichment), agent (router, policy answerer,
                   data answerer, sandbox, prompts, orchestrator), tracing, summary
backend/tests/     offline tests and ECB fixtures
backend/evals/     golden question set, independent reference, runner
frontend/src/      api client + generated types, state, components (summary, conversation, evidence)
var/               runtime: fx_cache/, traces/, evals/  (git-ignored)
```
# Write-up
## What you built and what you left out

Following the plan-and-solve agentic workflow, the backend consists of four agents responsible for making the following decisions
  - router agent: after receiving the user query, use GPT model (with OPENAI_API_KEY saved in .env) to decide which data source to use
    1) the global procurement and spend policy specified in procurement_policy.md;
    2) the invoice and vendor records saved in those two csv file;
    3) ECB euro reference rates when the data question needs currency conversion; this supplements the CSV evidence rather than replacing it; or 
		4) if the user query is irrelevant, such as "what is today's weather?", respond the input query cannot be supported by any evidence in the data. 
      The LLM will only answer questions supported by those three files and the authorized ECB euro reference rates
  - policy agent: if procurement_policy.md is chosen by the router agent, use the content in procurement_policy.md as context of the GPT model to answer the query and list the supporting evidence. If the query cannot be supported by any clause in procurement_policy.md, respond no evidence can be found to answer the question. Never use GPT's internal knowledge to answer without getting support from the data
  - query code generation agent: if the invoice and vendor records saved in those two csv file are chosen by the router agent, given the table schema as context, use GPT to convert the user query into pandas query function, and return the query results as well as records supporting the results. When conversion is needed, expose validated application-computed reporting amounts and ECB rate references in the schema.
  - results explaination agent: Use GPT to provide explanation on the verified results, grounded in the selected local records, policy clauses where relevant, and ECB reference-rate observations. If the user query cannot be successfully converted to pandas query function or the function cannot successfully run (e.g. due to the user's reference to columns not existing in those tables), return the explanation to the user so that the user can update the query accordingly. Missing or invalid rates must produce an explicit limitation or review response rather than guessed rates or an incomplete consolidated answer

The orchestrator will implement the control flow described above. The following principles are followed in the implementation:
  - LLM decides and explains while application code computs and verifies.
  - Evidence is a first-class output, not a by-product.
  - Never present an incomplete figure as complete.
  - Every outcome is explicit.
  - Everying is traceable.

I focus on implementing the above agentic workflow because they are most critical in meeting the user requirement of 1) the results should be evidence-based (using available input data to generate results instead of relying on LLM's internal knowledge); 2) the decision trajectory should be traceable. The following features are left out due to time constraint:
- storing procurement records and policy clauses in databases for efficient query;
- authentification of different users for access control;
- optimize LLM response speed to reduce latency;
- allow user to ask follow-up question based on the answers of previous questions.

## How you'd evaluate it
I will generate input queries to cover typical use cases and different edge cases. The typical use cases include those queries that can be answered using
 either procurement records or policy clauses (or both). 
 The edge cases should cover the following:
- out-of-scope queries that are not relevant to procurement records or policy (e.g. what is today's weather?);
- queries related to policy but no support from the given procurement_policy.md
- queries related to procurement records, but cannot successfully execute because of its using columns not present in the csv files

The expected answers should be generated by human annotation. Specifically, the human should write the query using the input natural language description and save the results as the ground truth.

## What production would need
To move the prototype into production, the following changes need to be made:
- Real data: replace the csv files with warehouse table, keep versioning of both procurement records and policy, use SQL instead to scale to large dataset
- Access control: apply authentication with different user roles (with different role-level scopes)
- Cost: use the enterprise OpenAI models or open weight models for cost control (compare different models to achieve optimal tradeoff between performance and cost)
- Audit trail: besides what have been traced in the current implementation (input question, source chosen, prompt, model, query code, verification results), add the user identity,
  data version to the trace. Also consider the rention policy to decide how long to keep the logged traces.
- Efficiency: with multiple users using the system at the same time, these multiple jobs needs to be put in a queue. Furthermore, multiple app instances need to be created to meet the load.