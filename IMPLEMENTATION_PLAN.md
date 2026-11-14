# Implementation Plan — Procurement Q&A Agent (FastAPI + React)

Source of requirements: `system_design.md`. This plan describes how to build it: architecture, modules, data contracts, the FX conversion rules, the agent workflow, the UI, tests and milestones.

---

## 0. Facts established from the inputs (drive design decisions)

| Input | Observed fact | Consequence |
|---|---|---|
| `invoices_2025_clean.csv` | 140 rows, `invoice_number` unique, dates 2025-01-03 … 2025-12-26 | Small → load fully into memory at startup |
| | Currencies: EUR 89, GBP 27, USD 24 | 51 rows need FX for EUR reporting |
| | 32 invoices fall on weekends | Fallback rule is exercised often, must be visible in the UI |
| | 2 negative amounts (credit notes, policy §6) | Totals are *net*; surface credit notes in the evidence |
| | `po_number` null in 21 rows, `approved_by` null in 25 rows | Nullable columns; relevant to policy §2.2 / §3.1 questions |
| `vendor_master.csv` | 10 vendors, all invoice `vendor_id`s match, names consistent | Left-join on `vendor_id`; still validate at load |
| | Status: 8 ACTIVE, V-1006 INACTIVE, V-1010 ON_HOLD | Status breakdown in `/summary` |
| `procurement_policy.md` | FIN-POL-014 v3.2, clauses §1–§9, approval table in §3 | Split into clause-addressable chunks; §5.1 requires the ECB reference rate on the invoice date |
| ECB EXR `D.USD.EUR.SP00.A` / `D.GBP.EUR.SP00.A` | SDMX csvdata via the ECB Data Portal API; units of currency per 1 EUR; USD 4 dp, GBP 5 dp; published on TARGET business days only (2025 gaps: 1 Jan, 18 and 21 Apr, 1 May, 25 and 26 Dec) | Parser validates key, dates, decimals and publication-day coverage; one series per EUR conversion; cache snapshots |

---

## 1. Architecture

```
React (Vite + TS)                    FastAPI
┌──────────────────┐   GET /summary  ┌───────────────────────────────────────────┐
│ SummaryPanel     │ ─────────────▶ │ api/routes.py                              │
│ ChatPanel        │   POST /ask     │   └─ agent/orchestrator.py (plan & solve)  │
│ EvidencePanel    │ ─────────────▶ │        ├─ router (GPT, structured output)  │
└──────────────────┘   GET /traces/* │        ├─ policy_answerer (GPT + verify)   │
                                     │        ├─ data_answerer (GPT codegen →     │
                                     │        │     sandbox exec → verify → GPT   │
                                     │        │     explanation)                  │
                                     │        └─ composer (merge, limitations)    │
                                     │ data/   store.py  (CSV load + validation)  │
                                     │ fx/     client, parser, cache, convert     │
                                     │ policy/ clauses.py                         │
                                     │ tracing/ trajectory.py (JSONL)             │
                                     └───────────────────────────────────────────┘
```

Rule that runs through the whole design: **the LLM decides and explains; application code computes and verifies.** GPT never makes up numbers, rates or quotes. Every number shown comes from executed code over validated data, and every quote is checked against the source text.

### Repository layout

```
backend/
  app/
    main.py               # FastAPI app, lifespan: load data, load/refresh FX, build clauses
    config.py             # pydantic-settings: OPENAI_API_KEY, OPENAI_MODEL, paths, FX settings
    api/routes.py         # /ask, /summary, /health, /traces/{id}, /fx/status
    api/schemas.py        # Pydantic request/response models (single source of truth for the UI contract)
    data/store.py         # load + validate CSVs → DataStore (invoices, vendors, enriched view)
    fx/client.py          # HTTP fetch (httpx, timeout, retry) of the two authorized ECB URLs only
    fx/parser.py          # HTML → [(date, Decimal | None)]
    fx/cache.py           # snapshot persistence + integrity
    fx/convert.py         # Decimal conversion, observation selection, evidence objects
    policy/clauses.py     # policy.md → clause list with ids, headings, verbatim text
    agent/llm.py          # OpenAI client wrapper (structured outputs, retries, token/latency logging)
    agent/prompts/*.md    # router, policy, codegen, explain prompts (versioned)
    agent/router.py
    agent/policy_answerer.py
    agent/data_answerer.py
    agent/sandbox.py      # AST validation + isolated subprocess execution
    agent/orchestrator.py
    tracing/trajectory.py
  tests/ ...
  fixtures/ecb/           # captured ECB csvdata responses for offline tests
frontend/
  src/ api/ client.ts, types.ts (generated from OpenAPI)
       state/ chatReducer.ts, useSummary.ts, useAsk.ts
       components/ SummaryPanel, ChatPanel, MessageBubble, AnswerCard,
                   EvidencePanel/{PolicyEvidence, DataEvidence, FxEvidence, CodeEvidence, Limitations},
                   SourceBadge, StatusBanner, Skeleton, ErrorCard
       styles/ tokens.css
var/                      # runtime: fx_cache/, traces/  (git-ignored)
```

Stack: Python 3.12, FastAPI, pydantic v2, pandas, httpx, `openai` SDK, pytest. Frontend: React 18 + TypeScript + Vite, with types generated from FastAPI's OpenAPI schema (`openapi-typescript`). `.env` is read only by the backend (`OPENAI_API_KEY`, optional `OPENAI_MODEL`). The model name is configurable and never hard-coded in prompts.

---

## 2. Data layer (`data/store.py`)

1. Load with explicit dtypes: strings stay strings, `amount` is parsed **as a string, then turned into `Decimal`** (keeps the 2-dp value exact). A float copy is kept only for pandas convenience operations that don't affect reported money.
2. Validate on load and fail fast with a clear startup error:
   - required columns present; `invoice_number` unique; `invoice_date` parses as ISO date; `currency ∈ {EUR, GBP, USD}`; `amount` has ≤ 2 dp; every `vendor_id` exists in vendor master; `status ∈ {ACTIVE, INACTIVE, ON_HOLD}`.
   - Soft findings (null `po_number` / `approved_by`, negative amounts, name mismatch with the master) are recorded as **data-quality notes**, not errors. They are exposed in `/summary` and to the code generator.
3. Build the **enriched invoice view**, the only table GPT-generated code sees for money questions:

| column | meaning |
|---|---|
| all invoice columns | as supplied (`amount` = original currency amount) |
| `vendor_country`, `vendor_category`, `vendor_status` | joined from vendor master |
| `amount_eur` | application-computed reporting amount (Decimal → see §3), `NaN` if unavailable |
| `fx_status` | `IDENTITY` (EUR) · `EXACT` · `FALLBACK` · `UNAVAILABLE` |
| `fx_observation_date` | ECB rate date actually used (null for EUR / unavailable) |
| `fx_ref_id` | key into the `fx_refs` table (rates, URL, snapshot id) |
| `is_credit_note` | `amount < 0` |

`fx_refs` is a separate small DataFrame (one row per `(observation_date, legs)`), so the generated code can include rate references in its supporting records without handling raw rates itself.

Columns for USD and GBP reporting are computed lazily if the router asks for a non-EUR reporting currency.

---

## 3. FX subsystem (`fx/`) — ECB euro reference rates

> **Changed 14 Nov 2026:** the authorized source moved from Federal Reserve H.10 to ECB euro foreign exchange reference rates at the user's direction. Policy §5.1 asks for the ECB reference rate on the invoice date, so conversions now follow the policy; only weekend/holiday fallbacks remain a flagged convention.

### 3.1 Fetch (`client.py`)
- Allow-list of exactly two ECB Data Portal API requests (CSV): `https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A?startPeriod=2024-01-01&format=csvdata` and the same for `D.GBP.EUR.SP00.A`. No other FX source exists in the codebase; redirects off `data-api.ecb.europa.eu` are refused.
- httpx, 20 s timeout, 2 retries with backoff, `Accept: text/csv`. Runs at startup (in the background, only when the cache is missing or older than `FX_MAX_AGE_HOURS`) and on demand via a dev-only refresh; never inside generated code.

### 3.2 Parse and validate (`parser.py`)
- SDMX `csvdata` rows: `KEY, FREQ, CURRENCY, CURRENCY_DENOM, TIME_PERIOD, OBS_VALUE, OBS_STATUS, TITLE, …`. The key must be exactly `EXR.D.<CUR>.EUR.SP00.A` (daily, EUR denominator, reference rate).
- `TIME_PERIOD` is ISO; dates must be unique, increasing, on weekdays, between 1999-01-04 and the fetch date.
- `OBS_VALUE` → `Decimal(str)`; numeric with at most the series' published decimals (USD 4, GBP 5); plausibility band per series (USD 0.8–1.7, GBP 0.55–1.0 per EUR). `OBS_STATUS` other than `A` (normal) makes that observation unusable.
- **Coverage:** every ECB publication day in the required window (first invoice date − 7 days … last invoice date) must be present. Publication days are weekdays except TARGET holidays (1 Jan, Good Friday, Easter Monday, 1 May, 25 and 26 Dec; Easter computed with the Gregorian algorithm). In 2025 the only missing weekdays are exactly those six days.
- A response that fails validation is **never** used. The previous valid snapshot stays active, and the failure is logged and shown in `/fx/status`.

### 3.3 Cache (`cache.py`)
- `var/fx_cache/ecb/<series>/<snapshot_id>.csv` (raw response, kept for audit — policy §8.2 lineage) plus `.json` metadata: source URL, series key and title, fetch time, HTTP status, raw SHA-256, `data_sha256` (hash of the parsed observations) and the validation report. `snapshot_id = <fetched_at UTC>_<sha256[:12] of raw bytes>`.
- Deduplication uses `data_sha256`, so a refresh with unchanged rates reuses the existing snapshot id and writes nothing.
- `active.json` points at the active snapshot per series. Startup order: valid cached snapshot → background refresh only if missing/stale → if neither works, FX is **unavailable** and the app still runs (original-currency features work).
- Snapshots are immutable, re-parsed and integrity-checked on load. Every answer cites the snapshot ids it used.

### 3.4 Conversion rules (`convert.py`)

Notation: `X(c, d)` = units of currency c per 1 EUR on date d (ECB quote); `X(EUR) = 1`.

| from → to | formula | series required |
|---|---|---|
| X → X | identity | none |
| USD → EUR | `amount ÷ X(USD)` | USD |
| GBP → EUR | `amount ÷ X(GBP)` | GBP |
| EUR → USD | `amount × X(USD)` | USD |
| EUR → GBP | `amount × X(GBP)` | GBP |
| GBP → USD | `amount × X(USD) ÷ X(GBP)` | USD, GBP (same date) |
| USD → GBP | `amount × X(GBP) ÷ X(USD)` | USD, GBP (same date) |

General form: `amount_dst = amount_src × X(dst) ÷ X(src)`.

**Observation selection** for invoice date `t` and required series `L`:
1. If every series in `L` has a rate on `t` → `EXACT`, `d = t` (this is what §5.1 asks for).
2. Otherwise, unless `exact_date=True`: the latest `d` with `t−7 ≤ d < t` where **every** series in `L` has a rate on that same `d` → `FALLBACK` (days recorded). This happens on weekends and TARGET holidays.
3. Otherwise → `UNAVAILABLE`, with a reason (`NO_SNAPSHOT`, `AFTER_SNAPSHOT_COVERAGE`, `EXACT_DATE_NOT_AVAILABLE`, `NO_OBSERVATION_WITHIN_7_DAYS`).
4. `d > t` is never chosen, nor any date after the snapshot's fetch date.

The fallback is labelled in code, API responses and the UI as a **prototype convention** that §5.1 does not cover, and those conversions are flagged for review.

**Decimal precision and rounding**
- `decimal.localcontext(prec=28)` for intermediate math; rates used exactly as published. Cross rates are computed in one expression, never rounded on their own.
- Each invoice's reporting amount is rounded to 0.01 with `ROUND_HALF_UP`; the unrounded value is kept in the evidence.
- Aggregates = **sum of rounded per-invoice amounts** (a total always equals the rows shown).
- Policy thresholds (e.g. §3 "50,000.01 – 250,000") are compared against the rounded EUR amount.

**Evidence object** (attached to each converted row, and grouped in answers):
```json
{
  "fx_ref_id": "2025-03-07|GBP",
  "requested_date": "2025-03-09",
  "observation_date": "2025-03-07",
  "status": "FALLBACK", "fallback_days": 2, "exact_date_requested": false,
  "legs": [{"series": "ECB euro reference exchange rate — pound sterling per euro (EXR.D.GBP.EUR.SP00.A)",
            "series_key": "EXR.D.GBP.EUR.SP00.A", "quote": "GBP per 1 EUR", "value": "0.84088",
            "url": "https://data-api.ecb.europa.eu/service/data/EXR/D.GBP.EUR.SP00.A?...",
            "series_page": "https://data.ecb.europa.eu/data/datasets/EXR/EXR.D.GBP.EUR.SP00.A",
            "snapshot_id": "..."}],
  "formula": "GBP→EUR = amount ÷ GBP per EUR",
  "method": "amount × (reporting-currency units per EUR) ÷ (source-currency units per EUR) ... ROUND_HALF_UP",
  "convention_note": "Prototype convention (not an ECB or policy rule): ... weekends or TARGET holidays ...",
  "policy_note": "Converted at ECB euro foreign exchange reference rates, the rate source that FIN-POL-014 §5.1 requires ..."
}
```

**Completeness rule.** An aggregate over converted amounts is `COMPLETE` only if every contributing row has `fx_status ∈ {IDENTITY, EXACT, FALLBACK}`. Otherwise the converted aggregate is returned as **unavailable**, the affected invoices and missing ECB evidence are listed with a review recommendation, and the original-currency subtotals are still returned. A partial sum is never shown as a total.

## 4. Policy subsystem (`policy/clauses.py`)

- Read `procurement_policy.md` read-only (never modified). Split it into clauses: numbered paragraphs (`2.1`, `3.1`, …), section intros, and the §3 table. Each table row becomes its own addressable clause (`3.T1` … `3.T4`) and also keeps the whole table.
- Each clause has `{id, section_title, text (verbatim), char_span}`. The doc header (ID, version, effective date) is attached to every policy citation.

---

## 5. Agent workflow (plan and solve)

### 5.1 Step 1 — Router (`router.py`)
One GPT call with **structured output** (a JSON schema enforced through the API's response-format feature):

```json
{
  "in_scope": true,
  "sources": ["policy", "records", "fx"],          // any subset
  "intent": "aggregate | lookup | policy_rule | compliance_check | ...",
  "reporting_currency": "EUR",                      // default EUR
  "original_currency_filter": null,                 // e.g. "EUR" only if user means invoices *denominated* in EUR
  "exact_date_fx": false,                           // true only if user explicitly asks for the exact invoice date's rate
  "policy_topics": ["approval_thresholds"],
  "rationale": "short text (logged, shown in trace view)"
}
```
- The prompt holds the data source descriptions, both table schemas (no rows), the policy section titles, and few-shot examples covering: the three example questions, "spend in EUR" vs "invoices in EUR", hybrid questions ("Which 2025 invoices exceeded the Finance Director threshold but lack an approver?" → records + fx + policy), and out-of-scope ones ("today's weather", "current EUR/USD rate", "who is the CFO").
- App-side rules after the router: `records` answers with money aggregates in a non-original currency always add `fx`. A question that is only about FX with no invoice link (e.g. "what was EUR/USD on 3 Mar 2025?") can be answered **only** from the ECB rate snapshot, deterministically, without codegen.
- `in_scope=false` → fixed response: *"This question cannot be supported by any evidence in the invoice data, vendor master, procurement policy, or the authorized ECB rates."* No further GPT calls.
- Router failure (API error, invalid JSON after 1 repair retry) → a `503`-style answer object with `error.kind = "LLM_UNAVAILABLE"`. The app never guesses the route.

### 5.2 Step 2 — Policy answerer (`policy_answerer.py`)
- Context = the full clause list (small doc) with ids. Instructions: answer only from the clauses; cite clause ids; give **verbatim quotes**; if no clause supports the answer, return `supported=false`.
- Structured output: `{supported, answer, citations:[{clause_id, quote}], caveats}`.
- **Verification in app code:** every `clause_id` must exist and every `quote` must be an exact substring of that clause (after whitespace normalisation). Failing citations are dropped. If nothing survives → `supported=false` → *"No evidence in the procurement policy supports an answer to this question."*
- Example: "Who is allowed to approve EUR 60,000?" → Finance Director, citing `3.T3` ("50,000.01 – 250,000 | Finance Director"), plus `7.2` self-approval and `3.1` approval evidence as optional context.
- Amounts in other currencies ("approve a USD 60,000 invoice") → route also includes `fx`. Conversion is done in app code at a user-supplied date, or the answer asks for the date. The ECB basis note is added.

### 5.3 Step 3 — Data answerer (`data_answerer.py`)

**3a. Code generation.** GPT receives: the enriched view schema (column, dtype, meaning, nullability, allowed values, sample of 3 rows), the `vendors` and `fx_refs` schemas, data-quality notes, the router plan, and a strict contract:

```python
def run(invoices: pd.DataFrame, vendors: pd.DataFrame, fx_refs: pd.DataFrame) -> dict:
    # returns {
    #   "result": <DataFrame or scalar or dict>,       # the answer table/value
    #   "supporting_invoice_numbers": list[str],       # rows that contributed
    #   "money_columns": list[str],                    # e.g. ["amount_eur"]
    #   "notes": list[str]
    # }
```
Rules in the prompt: use `amount_eur` for EUR reporting and never compute FX; use `currency == 'EUR'` only for "originally denominated" filters; always return supporting rows; net credit notes unless asked otherwise; group by `vendor_id` (not name) for rankings, with name as a label; no I/O, no imports.

**3b. Static validation (`sandbox.py`).** Parse with `ast`; reject imports, `exec/eval/open/compile/__import__/getattr/globals`, dunder attribute access, `while True`, file and network names, and calls to `.to_csv/.to_pickle/...`. Check that the referenced column names exist in the schema (compare string literals used as column keys with the known columns). This gives a precise "column `region` does not exist; available: …" message.

**3c. Execution.** In a fresh `python -m app.agent.sandbox_worker` subprocess: inputs are sent as a pickle from the trusted parent, and output comes back as **JSON only**, so nothing the generated code returns can execute in the parent. The worker has restricted builtins (no `__import__`/`open`/`print`), `pd`/`np` and an app-provided `convert_amount()` helper preloaded, sockets disabled, a CPU-time limit (the memory limit can't be enforced on macOS), copies of the DataFrames, and a 15 s timeout. GPT code never does FX math: it uses `amount_eur` or `convert_amount()`, and every helper call is logged as ECB rate evidence.

**3d. Result verification (app code, not GPT).**
- Output matches the contract; `supporting_invoice_numbers` ⊆ real invoice numbers.
- Every declared aggregation (`sum|count|mean|min|max` over a source column, optional `group_by`) is **recomputed in Decimal** from the supporting invoices and must match (to 0.01 for money, exactly for counts). Summing `amount` across currencies is rejected. Verified money cells are replaced by exact decimal strings.
- **FX completeness via probe runs:** when some invoices lack a rate, the code also runs on two probe copies. In those copies the rows without a rate are disguised as ordinary converted rows: an extreme high or low `amount_eur`, `fx_status=EXACT`, and a plausible observation date. Any such invoice included in either run "affects" the answer, and every converted aggregate becomes unavailable (null) with an FX_UNAVAILABLE limitation listing them. A query can't hide missing rates through `dropna` or an `fx_status` filter (both found in live runs).
- Empty results are allowed but flagged ("no invoices match the filter …").

**3e. Repair loop.** On a validation or runtime error, send the error and the schema back to GPT **once** for a corrected function. If it fails again, return `status="QUERY_FAILED"` with a user-facing explanation (what failed, which column or concept is unknown, available columns, suggested rephrasing). No answer is given.

**3f. Explanation.** A GPT call with the verified result only (the table, supporting-row count, original-currency breakdown, FX coverage, limitations, and cited policy clauses for hybrid questions). The model must refer to numbers with `{{row:column}}` placeholders, which the app replaces with verified, formatted values. Numbers it types itself trigger one retry and then a neutral fallback text. Numbers from the question and from the cited clauses (e.g. thresholds) are allowed. The app adds the ECB basis note and fallback notes itself (not left to GPT).

**Hybrid questions:** the policy step is told to answer only the policy part (rules and thresholds), and its verified clauses are passed to code generation so thresholds come from the policy text.

### 5.4 Composer
Merges the policy and data branches. The overall `status` is one of `ANSWERED · PARTIAL (limitations) · NO_EVIDENCE · OUT_OF_SCOPE · QUERY_FAILED · ERROR`. Limitations are first-class objects, not hidden in prose.

### 5.5 Trajectory logging (`tracing/trajectory.py`)
- One `trace_id` (UUID) per `/ask`, returned in the response and the `X-Trace-Id` header.
- Append-only JSONL `var/traces/YYYY-MM-DD.jsonl`. Steps: `router`, `policy_answer`, `codegen` (attempt n), `static_check`, `exec`, `verify`, `explain`, `compose`. Each step records prompt version, model, inputs (question, plan, schema hash), raw LLM output, parsed output, generated code, exec stdout and error, duration and tokens. Data snapshot hashes (CSV sha256, FX snapshot ids) are recorded per trace.
- `GET /traces/{trace_id}` returns the trajectory (dev mode only; secrets are never logged).

---

## 6. API contract (`api/schemas.py`)

### `POST /ask`
Request: `{ "question": str (1–1000 chars), "reporting_currency"?: "EUR"|"USD"|"GBP", "conversation_id"?: str }`

Response (HTTP 200 for every handled outcome, including no-evidence and query-failed. 4xx is only for invalid input, 5xx only for real server faults):
```json
{
  "trace_id": "…",
  "status": "ANSWERED",
  "question": "…",
  "route": {"sources": ["records","fx"], "reporting_currency": "EUR", "original_currency_filter": null, "rationale": "…"},
  "answer": {"text": "…", "key_figures": [{"label": "Total spend (EUR)", "value": "1234567.89", "currency": "EUR"}]},
  "evidence": {
    "policy": [{"clause_id": "3.T3", "section": "3. Approval Thresholds", "quote": "…", "doc": "FIN-POL-014 v3.2"}],
    "data": {
      "result_table": {"columns": [...], "rows": [...]},
      "supporting_rows": {"columns": [...], "rows": [...], "total_count": 37, "truncated": false},
      "original_currency_breakdown": [{"currency": "GBP", "amount": "…", "count": 5}],
      "code": "def run(...): …",
      "row_sources": ["invoices_2025_clean.csv", "vendor_master.csv"]
    },
    "fx": {"refs": [ /* evidence objects §3.4 */ ], "coverage": {"exact": 40, "fallback": 11, "identity": 89, "unavailable": 0},
           "policy_note": "…", "convention_note": "…"}
  },
  "limitations": [{"kind": "FX_UNAVAILABLE", "message": "…", "affected_invoices": ["INV-…"], "recommendation": "Review…"}],
  "error": null
}
```

### `GET /summary`
Computed deterministically (no LLM), cached in memory and invalidated when the FX snapshot changes.
```json
{
  "dataset": {"invoice_count": 140, "date_range": ["2025-01-03","2025-12-26"], "vendor_count": 10, "csv_sha256": {...}},
  "original_currency_totals": [{"currency":"EUR","amount":"…","count":89}, …],
  "consolidated": {"available": true, "currency": "EUR", "total": "…", "coverage": {...}, "fx_evidence_summary": {...},
                    "policy_note": "…"}   // or {"available": false, "reason": "...", "missing": [...]}
  "by_vendor_status": [{"status":"ACTIVE","count":…,"original_currency_totals":[…],"eur_total":"…"|null}, …],
  "top_vendors_eur": [...] | null,
  "data_quality": {"missing_po": 21, "missing_approver": 25, "credit_notes": 2, "invoices_from_non_active_vendors": …},
  "fx_status": {"EUR": {"snapshot_id": "…", "fetched_at": "…", "release": "…"}, "GBP": {...}}
}
```
If FX is unavailable, `consolidated.available=false`, and every EUR field is `null` (never zero, never partial).

### Others
`GET /health` (data loaded, FX snapshot state, LLM key present), `GET /fx/status`, `POST /fx/refresh` (dev only), `GET /traces/{id}` (dev only).

Money is always serialised as **strings** to avoid float drift in JSON. The frontend formats them for display.

---

## 7. Frontend (React + TS)

### 7.1 Layout (desktop first, min 1280 px; stacks below 1024 px)
```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Header: app name · data snapshot · FX snapshot status chip (OK / STALE / N/A)│
├───────────────────────┬──────────────────────────────────────────────────────┤
│ SUMMARY PANEL         │ CHAT PANEL                                           │
│ • Consolidated EUR    │  [User] What are our top 5 vendors by total spend…   │
│   total (+FX badge)   │  [Answer card]                                       │
│ • Original-currency   │    Status pill: ANSWERED · Sources: DATA + FX        │
│   totals table        │    Answer text with key figures                      │
│ • Vendor status       │    ▸ Evidence (expanded by default for latest)       │
│   breakdown           │       [Data rows] [FX rates] [Policy] [Query code]   │
│ • Data quality flags  │    ⚠ Limitations (if any)                            │
│ • Example questions   │  ───────────────────────────────────────────────────  │
│   (click to ask)      │  [ textarea ...................... ] [Ask]          │
└───────────────────────┴──────────────────────────────────────────────────────┘
```

### 7.2 Making the basis of the answer clear (core requirement)
- **Source badges** on each answer: `POLICY`, `INVOICE DATA`, `ECB RATES`. Each has a distinct shape and text label, never colour alone.
- **Status pill** with fixed wording: "Answered from evidence", "Answered with limitations", "No supporting evidence", "Out of scope", "Query could not run".
- **Policy evidence**: clause id + section title + verbatim quote in a blockquote, plus the doc id and version.
- **Data evidence**: a sortable table of supporting rows (invoice #, date, vendor, original amount + currency, EUR amount, FX status). It notes "showing 50 of 137" with an expand option. A "copy as CSV" button.
- **FX evidence**: per observation date, the requested date → used date (with a "FALLBACK −2 days" tag), series values, source URL links, method and snapshot id. The ECB basis note is always present when a conversion happened, plus a caution banner when a weekend/holiday fallback was used.
- **Query code**: the generated pandas function, read-only and collapsed by default (for auditors).
- **Limitations** box: high-visibility style above the evidence, listing affected invoices and the recommended review.
- Key figures link (hover/focus) to the rows that make them up.
- Trace id shown in small text with a copy button (for troubleshooting).

### 7.3 State
- `useReducer` chat store: `messages[]`, each `{id, question, state: 'pending'|'done'|'error', response?, error?}`. It lives in memory plus `sessionStorage`, so a page reload keeps the conversation.
- `useSummary()` fetches on mount, states `loading | ready | error`, with a manual retry.
- One `/ask` request at a time per conversation. The Ask button is disabled while pending; earlier answers stay readable. `AbortController` for cancel.

### 7.4 Loading and errors
- Summary: skeleton blocks with labels ("Loading invoice overview…") so the screen is never blank. If FX is down the panel still renders the original-currency totals plus a clear "EUR consolidation unavailable — reason" card.
- Ask: a pending bubble with step text ("Planning → Querying data → Verifying → Writing answer"). This is client-side staged text. A later option is to stream step events from the server via SSE.
- Network or 5xx: an error card with the message, a retry button, and the question kept in the input.
- `QUERY_FAILED`: shown as guidance (what to change, available columns), not as a crash.
- Timeouts: client 60 s, with a visible message.

### 7.5 High-contrast visual design
- Base: near-black text `#0B0B0B` on white `#FFFFFF` (≈ 20:1). Panel borders `#000` at 2 px, no low-contrast greys for content (minimum `#333` on white ≈ 12.6:1).
- Status colours are only accents next to text labels and icons, each ≥ 7:1 against its background (WCAG AAA), e.g. dark green `#0B5D1E`, dark amber text on `#FFF4CC`, dark red `#9B0000`.
- 16 px base font, 1.5 line height, tabular numerals for money, right-aligned numeric columns, visible 3 px focus rings, full keyboard operation, `aria-live="polite"` for new answers. Respects `prefers-reduced-motion`. An optional dark high-contrast theme through CSS tokens.

---

## 8. Error-handling matrix

| Condition | Behaviour |
|---|---|
| CSV invalid at startup | App refuses to start, clear log message |
| ECB fetch fails, valid cache exists | Use cache; FX chip shows the snapshot age |
| ECB fetch fails, no cache | FX unavailable; summary shows original currencies only; converted answers return a limitation |
| ECB response malformed or implausible | Snapshot rejected, previous kept, `/fx/status` explains |
| Rate missing for an invoice (holiday, >7 days, exact-date) | Row `UNAVAILABLE`; any aggregate over it → limitation listing the invoices; never a partial total |
| OpenAI error / timeout | One retry with backoff; then `status=ERROR, kind=LLM_UNAVAILABLE`; summary unaffected |
| Router says out of scope | Fixed out-of-scope message, no evidence |
| Policy has no supporting clause / quotes fail verification | `NO_EVIDENCE` |
| Codegen invalid / unknown column / runtime error ×2 | `QUERY_FAILED` + explanation and suggestions |
| Sandbox timeout / forbidden construct | `QUERY_FAILED` (logged with code in trace) |
| Verification mismatch | `ERROR` (internal), no answer shown, trace kept |

---

## 9. Testing strategy

1. **FX unit tests** (offline, using fixtures captured from the live pages):
   - parser: series key, ISO dates, no weekend rows, decimals per series, implausible values, non-final status, TARGET-holiday-aware coverage (Easter dates).
   - selection: exact date; Saturday/Sunday → Friday; Good Friday → Thursday; 26 Dec → 24 Dec; a US holiday (17 Feb) is a normal ECB day; a GBP→USD cross where one series is missing must fall back to a date valid for **both**; > 7-day gap → UNAVAILABLE; `exact_date=True` disables fallback; never a future date.
   - Decimal: known hand-computed conversions; no pre-rounding of the cross rate; ROUND_HALF_UP on .005 boundaries; total = sum of rounded rows.
2. **Data store tests**: validation errors on corrupted copies; enrichment correctness; credit notes.
3. **Sandbox tests**: forbidden constructs rejected (import os, `__class__`, `open`, `eval`), timeout enforced, unknown-column message.
4. **Policy verification tests**: fabricated quote dropped; unknown clause id dropped; empty → NO_EVIDENCE.
5. **API tests** with a mocked LLM (recorded structured outputs): every `status` branch, `/summary` with FX present and absent (asserting `null` not `0`).
6. **Golden question evals** (real LLM, run on demand, not in CI by default): ~25 questions with expected answers computed independently by a reference pandas script. They cover the 3 example questions, EUR-reporting vs EUR-denominated, hybrid policy+data, out-of-scope, an unknown column ("spend by region"), and exact-date FX. Asserts route, status, key figures (to the cent) and evidence presence.
7. **Frontend**: Vitest + React Testing Library for reducer, loading, error and limitation rendering. Playwright smoke test against a mocked backend. axe-core accessibility check (contrast, roles).

---

## 10. Milestones

| # | Deliverable | Exit criteria |
|---|---|---|
| M1 | Backend skeleton, config, DataStore + validation, policy clauses | `/health` green; data tests pass |
| M2 | FX: client, parser, cache, conversion, enriched view | FX unit tests pass; all 140 invoices get a status; weekend/holiday cases correct |
| M3 | `/summary` (deterministic) | Matches the reference script; FX-down mode verified |
| M4 | Router + policy answerer + tracing | Example Q3 answered with verified clause quote; out-of-scope handled |
| M5 | Codegen + sandbox + verification + explanation + composer | Example Q1/Q2 correct to the cent with evidence; failure paths return QUERY_FAILED |
| M6 | Frontend: summary panel, chat, evidence panel, states | Summary visible on load; all statuses render; axe passes |
| M7 | Golden evals, hardening, README (run instructions, conventions, known limits) | ≥ 95% golden pass; docs list the ECB rate basis and fallback convention |

M1–M3 (deterministic core) can proceed alongside M6's UI work once `schemas.py` is fixed, because the frontend builds against the OpenAPI contract with mocked responses.

---

## 11. Open questions / assumptions to confirm

1. **Rounding**: per-invoice ROUND_HALF_UP to 0.01, totals = sum of rounded rows (assumed). Alternative: sum unrounded, round once.
2. **Credit notes**: totals are net of negative invoices by default (assumed); answers mention how many credit notes are included.
3. **Fallback legs**: "all required legs" is applied per conversion pair (USD→EUR needs only the EUR series; GBP→EUR needs both). Assumed, not requiring every series on every date.
4. **Policy thresholds for non-EUR invoices** use the ECB-converted EUR amount, as §5.1 prescribes. Invoices dated on a weekend or TARGET holiday use the latest earlier ECB rate and are flagged, since §5.1 does not cover those dates.
5. **Multi-turn**: follow-up questions ("and for Q2 only?") are out of scope for v1. Each question is answered on its own, while the chat history is kept in the UI.
6. **FX refresh cadence**: fetch at startup plus manual refresh. 2025 rates are historical and stable, so no scheduled refresh is planned.
