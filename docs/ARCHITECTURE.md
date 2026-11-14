# Procurement Q&A Agent — Implementation Guide

How the system is built, what each component does and exposes, how the components call each other for real questions, and why it was designed this way.

Version: 1.0 (ECB reference rates), November 2026. Applies to the code in `backend/` and `frontend/`.

## 1. What the system does

A finance analyst types a question in plain English. The system answers it from exactly four evidence sources, and shows which of them each answer rests on:

- `data/invoices_2025_clean.csv`: 140 invoices from 2025, in EUR, GBP and USD
- `data/vendor_master.csv`: 10 vendors with country, category and status
- `data/procurement_policy.md`: the procurement policy FIN-POL-014 v3.2 (kept unchanged)
- ECB euro foreign exchange reference rates (US dollar and pound sterling series), fetched from the ECB Data Portal

There are two user-facing operations:

- `GET /summary`: an overview that needs no language model (consolidated EUR spend, original-currency totals, vendor-status breakdown, largest vendors, data-quality flags).
- `POST /ask`: an answer to a question, with the evidence behind it (policy clauses, invoice rows, exchange rates) and an explicit status.

## 2. Design principles and why

These five rules explain almost every design decision in the code.

**P1. The model decides and explains; application code computes and verifies.**
GPT chooses sources, writes a pandas query and phrases the explanation. It never produces a number, a rate or a quote that reaches the analyst unchecked. Totals are recalculated in `Decimal` by application code, quotes are matched verbatim against the policy file, and numbers in the explanation are filled in by the app.
*Why:* language models are good at intent and phrasing but unreliable at arithmetic and exact quotation. A finance answer that is fluent but wrong by a cent, or that cites a clause that does not exist, is worse than no answer.

**P2. Evidence is a first-class output, not a by-product.**
Every response carries structured evidence: clause ids and exact quotes, supporting invoice rows, and for each conversion the ECB series, URL, rate date, requested date, rate value, formula and snapshot id.
*Why:* the brief asks most of all that the UI makes clear what an answer is based on. Structured evidence lets the UI render a "Based on" strip and evidence tabs without parsing prose.

**P3. Never present an incomplete figure as complete.**
If any invoice behind a converted total lacks an acceptable exchange rate, the converted figure is shown as unavailable (null), the affected invoices are listed, and review is recommended.
*Why:* a silently partial total is the most dangerous failure in financial reporting, because it looks correct.

**P4. Every outcome is explicit.**
`/ask` returns HTTP 200 with one of six statuses: `ANSWERED`, `PARTIAL`, `NO_EVIDENCE`, `OUT_OF_SCOPE`, `QUERY_FAILED` or `ERROR`. HTTP errors are reserved for invalid input (422) and overload (503).
*Why:* "no evidence" and "the query could not run" are valid, useful answers that the UI must show differently from a crash.

**P5. Everything is traceable.**
Each question gets a trace id. Every step (prompt version, raw model output, parsed decision, generated code, checks, timings, tokens) is appended to a JSON-lines log.
*Why:* when an answer looks wrong, you need to see which step went wrong without re-running it. Policy §8.2 also asks for traceable lineage from source to report.

## 3. Repository map

```
agent_design/
├── data/                       inputs (read-only)
├── backend/
│   ├── app/
│   │   ├── main.py             app factory and startup (lifespan)
│   │   ├── config.py           settings from environment / .env
│   │   ├── api/                routes.py (HTTP endpoints), schemas.py (response contract)
│   │   ├── data/store.py       load + validate CSVs, exact Decimal amounts, joined table
│   │   ├── policy/clauses.py   policy.md -> addressable verbatim clauses, quote check
│   │   ├── fx/                 ECB rates: series, client, parser, cache, service,
│   │   │                       convert, enrich
│   │   ├── summary.py          deterministic GET /summary
│   │   ├── agent/              router, policy_answerer, data_answerer, sandbox,
│   │   │                       sandbox_worker, orchestrator, llm, prompts/
│   │   └── tracing/            trajectory.py (per-request JSONL trace)
│   ├── tests/                  offline tests + ECB fixtures + stdlib reference
│   └── evals/                  golden question set, independent reference, runner
├── frontend/src/
│   ├── api/                    client.ts, generated schema.d.ts, types.ts
│   ├── state/                  chatReducer.ts, useChat.ts, useSummary.ts
│   ├── components/             Masthead, SummaryPanel, Conversation, Composer,
│   │                           TurnView, AnswerCard, evidence/*
│   ├── sources.ts, format.ts   "Based on" logic, exact money formatting
│   └── styles/app.css          high-contrast design tokens
└── var/                        runtime only (rate cache, traces, eval reports)
```

## 4. Runtime architecture

```
 Browser (React)                      FastAPI process
 ┌────────────────────┐  GET /summary  ┌─────────────────────────────────────────┐
 │ SummaryPanel       │ ─────────────► │ routes.summary ─► summary.build_summary │
 │                    │                │                    (no model call)      │
 │ Conversation       │  POST /ask     │ routes.ask ─► Agent.ask (orchestrator)  │
 │  AnswerCard        │ ─────────────► │   1 router.route          ── GPT        │
 │  EvidencePanel     │                │   2 policy_answerer.answer ── GPT       │
 └────────────────────┘                │   3 data_answerer.answer   ── GPT x2    │
                                       │       └─ sandbox.execute ─► subprocess  │
                                       │   4 compose AskResponse                 │
                                       │                                         │
   shared, loaded at startup:          │ DataStore · PolicyDocument · FxService  │
                                       │ FxViews · Tracer · OpenAILLM            │
                                       └──────────────┬──────────────────────────┘
                                                      │ only application code
                                                      ▼ fetches rates
                                     ECB Data Portal API (2 allow-listed URLs)
```

### 4.1 Startup sequence (`main.create_app` → `lifespan`)

1. Load settings (`config.Settings`) from the environment and the repo-root `.env`.
2. `load_datastore()` reads and validates both CSVs. Invalid data raises and the app refuses to start (fail fast; a wrong answer from bad data is worse than no service).
3. `load_policy()` splits the policy into clauses and records its SHA-256.
4. Build `FxService` with the coverage window "first invoice date − 7 days … last invoice date", load cached ECB snapshots, and start a background refresh only if a series is missing or older than `FX_MAX_AGE_HOURS` (24 h). The API is usable immediately; the summary simply shows EUR as unavailable until rates load.
5. Create `FxViews` (per-currency converted invoice tables), the `OpenAILLM` client, the `Tracer`, the `Agent`, and a semaphore limiting concurrent `/ask` calls (`MAX_CONCURRENT_ASKS`, default 4).

*Rationale:* everything is in memory (140 invoices, 21 clauses, about 700 rate rows per series), so a database would add moving parts without benefit. Shared objects are immutable or swapped atomically, so request threads need no locking beyond small caches.

## 5. Backend components and their APIs

Each subsection lists the component's job, its public API and the reasoning behind it.

### 5.1 Configuration — `app/config.py`

`Settings` (pydantic-settings) reads environment variables and `.env`. `get_settings()` returns a cached instance; tests construct `Settings(_env_file=None, ...)` so they never read your key.

| Setting | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | — | needed for `/ask` only |
| `OPENAI_MODEL` | `gpt-4.1` (your `.env`: `gpt-5-mini`) | model for all four GPT steps |
| `OPENAI_REASONING_EFFORT` | `low` | sent only to reasoning models |
| `MAX_CONCURRENT_ASKS` | 4 | overload guard (503 beyond) |
| `FX_REFRESH_ON_STARTUP`, `FX_MAX_AGE_HOURS` | true, 24 | when to refetch ECB rates |
| `DEV_MODE` | true | exposes `/traces/{id}` and `POST /fx/refresh` |

Derived paths: `invoices_path`, `vendors_path`, `policy_path`, `fx_cache_dir` (`var/fx_cache/ecb`), `traces_dir` (`var/traces`).

### 5.2 Data store — `app/data/store.py`

**API**

```python
load_datastore(invoices_path: Path, vendors_path: Path) -> DataStore   # raises DataValidationError

@dataclass
class DataStore:
    invoices: pd.DataFrame        # invoice columns; invoice_date datetime64, amount float
    vendors: pd.DataFrame
    enriched: pd.DataFrame        # invoices + vendor_country/category/status + is_credit_note
    amounts: dict[str, Decimal]   # invoice_number -> exact original amount
    file_sha256: dict[str, str]
    notes: list[DataQualityNote]  # MISSING_PO, MISSING_APPROVER, CREDIT_NOTE, NON_ACTIVE_VENDOR, ...
    date_range -> tuple[date, date]
```

**Logic.** CSVs are read as strings first. Hard checks (required columns, unique invoice numbers, ISO dates, at most 2 decimals, allowed currencies and statuses, every vendor id in the master) raise `DataValidationError` with CSV line numbers. Soft findings become data-quality notes. Amounts are parsed from their original strings into `Decimal`.

*Rationale:* parsing money from strings keeps "21344.84" exact; floats are kept only for pandas convenience. Joining vendor attributes once means generated queries never need a merge, which removes a common source of model mistakes (wrong join keys, duplicated rows).

### 5.3 Policy clauses — `app/policy/clauses.py`

**API**

```python
load_policy(path: Path) -> PolicyDocument
class PolicyDocument:
    title, metadata, clauses: list[Clause], sha256, doc_label  # "FIN-POL-014 v3.2"
    get(clause_id) -> Clause | None
    quote_in_clause(clause_id, quote) -> bool
@dataclass(frozen=True)
class Clause: id, section_number, section_title, kind, text, start, end, table_header
```

**Logic.** The markdown is split into 21 addressable clauses: numbered paragraphs (`2.1`, `5.1`, …), the §1 intro (`1`), the §3 table as a whole (`3.T`) and each table row (`3.T1` … `3.T4`, each carrying the header row). Each clause keeps its exact text and character span, verified against the source. `quote_in_clause` accepts a quote only if it appears verbatim in that clause, ignoring whitespace and markdown markers (`**`, backticks).

*Rationale:* addressable clauses let the model cite precisely ("3.T3" rather than "section 3") and let the app check every quote mechanically. Table rows need their own ids because the approval matrix is the most-asked part of the policy.

### 5.4 Exchange rates — `app/fx/`

This package is the only code that touches exchange-rate data.

**`series.py` — what is authorized.** `SERIES` maps `USD` and `GBP` to a `SeriesSpec` (SDMX key such as `EXR.D.USD.EUR.SP00.A`, the exact API URL, the human-readable ECB series page, published decimals of 4 or 5, a plausibility band). `ALLOWED_URLS` is the allow-list. `POLICY_NOTE` and `CONVENTION_NOTE` are the fixed sentences shown with converted figures.

**`client.py` — fetching.** `fetch(url, *, timeout=20, retries=2, backoff=1.0, transport=None) -> FetchResult` refuses any URL outside the allow-list and any redirect off `data-api.ecb.europa.eu`, retries transient failures and raises `RateFetchError`.

**`parser.py` — validation.** `parse_ecb_csv(text, spec, fetched_on, required_coverage) -> ParsedSeries` checks the series key, ISO dates (unique, increasing, weekdays only, not in the future), decimals, plausibility, observation status, and that every ECB publication day in the coverage window is present. `target_holidays(year)` computes the TARGET closing days (1 Jan, Good Friday, Easter Monday, 1 May, 25 and 26 Dec) using a Gregorian Easter algorithm; `is_publication_day(d)` combines this with weekends. Any failure raises `RateValidationError`, and that response is never used.

**`cache.py` — immutable snapshots.** `build_snapshot(series, raw, fetched_at, ...) -> Snapshot`; `SnapshotCache(root)` with `save`, `load` (re-parses and checks the stored SHA-256), `find_by_data_sha`, `active_ids` and `set_active`. Files: `var/fx_cache/ecb/<SERIES>/<snapshot_id>.csv` plus `.json` metadata, and `active.json`.

**`service.py` — lifecycle.** `FxService(cache, required_coverage, fetcher=fetch)` provides `load_cached()`, `refresh()` (fetch → validate → deduplicate on `data_sha256` → save → activate; a failed series keeps its previous snapshot), `rates() -> RateTable`, `current() -> (RateTable, version)`, `status()`, `missing_series()` and `needs_refresh(max_age)`. `version` increments when active snapshots change, which invalidates dependent caches.

**`convert.py` — the arithmetic.**

```python
required_legs(src, dst) -> tuple[str, ...]            # non-EUR currencies involved
select_observation(rates, legs, requested, exact_date=False, max_fallback_days=7) -> Selection
convert(amount, src, dst, sel) -> (unrounded, rounded) | None
rate_formula(src, dst) -> str                         # e.g. "GBP→EUR = amount ÷ GBP per EUR"
evidence(sel, rates, src, dst) -> dict                # the citation object
```

ECB quotes are units of foreign currency per 1 EUR, so with X(EUR) = 1: `amount_dst = amount_src × X(dst) ÷ X(src)`. Converting to EUR needs one series; GBP↔USD needs both, from the same date. Arithmetic is `Decimal` at 28 digits in one expression, rounded once to 0.01 with ROUND_HALF_UP.

Observation selection: use the invoice date if every required series has a rate (`EXACT`). Otherwise, unless an exact date was requested, use the latest earlier date within 7 days on which every series has a rate (`FALLBACK`). Otherwise the result is `UNAVAILABLE`, with a reason such as `EXACT_DATE_NOT_AVAILABLE` or `NO_OBSERVATION_WITHIN_7_DAYS`. A future date is never used.

**`enrich.py` — per-invoice conversion.** `build_fx_view(store, rates, reporting_currency="EUR", exact_date=False) -> FxView` converts every invoice once and adds `amount_eur`, `fx_status`, `fx_observation_date` and `fx_ref_id`, plus a `refs` table, exact `Decimal` amounts and an evidence object per invoice. `FxViews.get(currency, exact)` caches one view per combination and rebuilds it when the FX version changes.

*Rationale:*
- Conversion happens once, in application code, before any model sees the data. Generated queries simply sum `amount_eur`, so they cannot apply a wrong rate or formula.
- The ECB was chosen because policy §5.1 requires the ECB reference rate on the invoice date. Weekend and holiday dates have no ECB publication, so the 7-day fallback is a labelled prototype convention that the policy does not cover.
- Strict validation and immutable, hash-checked snapshots make every figure reproducible: an answer cites the exact snapshot it used.

### 5.5 Summary — `app/summary.py`

`build_summary(store, policy, view, fx_status) -> SummaryResponse` computes the overview with no model call: original-currency totals, the consolidated EUR total with a per-currency reconciliation, vendor-status groups, the top 5 vendors, data-quality notes and an evidence summary (sources, coverage, fallback invoices, formulas, method, notes). If any invoice lacks a rate, the EUR total and the ranking become null with a reason and the missing invoices; a status group keeps its EUR total only if all of its invoices converted. The route caches the result until the FX version changes.

*Rationale:* the overview must appear before any question is asked and must be identical every time, so it is deterministic. It also acts as a reference the analyst can compare answers against.

### 5.6 Language-model wrapper — `app/agent/llm.py` and `prompts/`

```python
class LLM(Protocol):
    model: str
    def parse(self, *, step, instructions, input, schema: type[BaseModel]) -> LLMResult
class OpenAILLM(LLM)  # OpenAI Responses API, text_format=schema (structured output)
class LLMError(Exception): kind  # LLM_NOT_CONFIGURED | LLM_UNAVAILABLE | LLM_REFUSAL | LLM_BAD_OUTPUT
```

Each call enforces a Pydantic schema, so the app receives typed objects, never free text to parse. A malformed output gets one retry, and refusals are detected. `LLMResult` carries the raw text, model, latency and token usage for the trace. Prompts are Markdown templates in `agent/prompts/` (`router`, `policy`, `codegen`, `explain`) whose version is a hash of their content, recorded in every trace.

*Rationale:* a narrow `parse()` interface makes the model swappable and lets tests substitute a scripted fake (`tests/fake_llm.py`), so the whole agent is tested offline and deterministically.

### 5.7 Step 1 — Router — `app/agent/router.py`

```python
route(llm, policy, question, trace) -> Route
class RouterDecision(BaseModel):   # what GPT must fill
    in_scope: bool
    sources: list["policy" | "records" | "fx"]
    intent: "policy_rule" | "aggregate" | "ranking" | "lookup" | "compliance_check" | ...
    reporting_currency: "EUR" | "USD" | "GBP"
    original_currency_filter: "EUR" | "USD" | "GBP" | None
    exact_date_fx: bool
    policy_topics: list[str]
    rationale: str
apply_rules(d: RouterDecision) -> Route   # adds rules_applied: list[str]
```

The prompt lists the sources (policy section titles, table schemas, the ECB rates) and worked examples, including the distinction between *reporting* in EUR and *filtering to invoices issued* in EUR, and that a date in a question is not an "exact date only" request. `apply_rules` then enforces deterministic rules: policy plus records always adds rates (thresholds are in EUR); a cross-currency money aggregate adds rates; an in-scope question with no source becomes out of scope.

*Rationale:* routing is a language-understanding task, so GPT does it. The safety-relevant consequences are enforced by code, and every applied rule is recorded and shown to the analyst.

### 5.8 Step 2 — Policy answerer — `app/agent/policy_answerer.py`

```python
answer(llm, policy, question, trace, data_part=False) -> PolicyResult
class PolicyAnswerDraft(BaseModel): supported, answer, citations: list[Citation], caveats
class PolicyResult: supported, answer, citations: list[VerifiedCitation], caveats, rejected
```

GPT sees all clauses with their ids and must return verbatim quotes. `_verify` drops any citation whose clause id does not exist or whose quote is not in that clause. If something was dropped, GPT gets one retry listing the exact failures. If no citation survives, the result is "no evidence". In hybrid questions (`data_part=True`) GPT is told to answer only the policy part, because the records are queried separately.

*Rationale:* "never use internal knowledge" cannot be enforced by instruction alone. Requiring verifiable quotes turns it into a mechanical check: an answer with no verifiable clause behind it is not shown.

### 5.9 Step 3 — Data answerer — `app/agent/data_answerer.py`

```python
answer(llm, ctx: DataContext, question, route, policy_citations, trace) -> DataResult
@dataclass class DataContext: store, view: FxView, rates: RateTable
class CodeDraft(BaseModel): can_answer, reason, code, result_description
class Explanation(BaseModel): answer_text, key_figures: list[FigureRef(label,row,column)]
@dataclass class DataResult: status, answer_text, key_figures, data, fx, limitations,
                             caveats, failure_message, error_kind
```

This is the most involved component. Its pipeline:

1. **Generate code.** GPT receives the schema of the joined invoice table (including `amount_eur`, FX status columns, and the `vendors`, `fx_refs` and `rates` tables), the router plan, any verified policy clauses, and strict rules. It never sees data rows. It must return `def run(invoices, vendors, fx_refs, rates)`, which returns a result table, the supporting invoice numbers, a declaration of each aggregated column (`sum`/`count`/`mean`/`min`/`max`, source column, group-by) and notes. GPT can decline with a reason (for example, "there is no region field").
2. **Static check** with `sandbox.validate`.
3. **Run in the sandbox** with `sandbox.execute`. If some invoices lack a rate, the code also runs on two *probe* copies (see below).
4. **Verify** with `verify(out, ctx, affected) -> Verified`. Every declared aggregation is recalculated in `Decimal` from the supporting invoices and must match (0.01 for money, exactly for counts). Unknown invoice numbers, undeclared groups and summing `amount` across currencies are rejected. Verified money cells are replaced by exact decimal strings.
5. **Repair once.** On any failure GPT gets the exact problem and its previous code. A second failure returns `QUERY_FAILED` with a plain-language explanation and the available fields.
6. **Explain.** GPT writes the explanation from the verified facts, using `{{row:column}}` placeholders for every number. The app substitutes verified, formatted values. Typed numbers, invalid placeholders or column names in the prose trigger one rewrite (numbers from the question or the cited clauses are allowed).
7. **Assemble evidence:** result table with money-column currencies, supporting rows, original-currency breakdowns (also per group), aggregation check results, the code, and an FX block with one row per (rate date, invoice date), sources, method and notes.

**Probe runs (FX completeness).** If the view has invoices without a rate, the code also runs on two copies where those rows are disguised as normal converted rows (`amount_eur` set to +10¹⁵ or −10¹⁵, `fx_status` set to `EXACT`, a plausible rate date). If either run would include such an invoice, it "affects" the answer, and every converted aggregate becomes unavailable.

*Rationale:*
- Code generation gives flexibility (any filter or grouping) without giving up correctness, because the numbers are recomputed by the app.
- The aggregation declaration is what makes recomputation generic: the app does not need to understand the code, only to recompute what the code says it computed.
- The probes exist because live runs showed GPT filtering on `fx_status == 'EXACT'` or `dropna()`, which would hide rate-less invoices and produce a complete-looking partial total. The probes catch this whatever filter the code uses.
- Placeholders exist because the model's own numbers can drift from the verified ones; substitution makes misstated figures impossible.

### 5.10 Sandbox — `app/agent/sandbox.py` and `sandbox_worker.py`

```python
validate(code: str, known_columns: set[str]) -> list[str]          # problems; [] = acceptable
execute(code, frames, variants, rates, timeout_s=15.0, cpu_seconds=10, memory_mb=2048) -> ExecResult
class ExecResult: ok, outputs, error_type, error, line, sandbox_notes
```

`validate` parses the code with `ast` and rejects:
- imports;
- names such as `open`, `eval`, `getattr` and `__import__`;
- private or dunder attributes;
- I/O methods (`to_csv`, `read_*`, `query`, `eval`, `pd.io`, …);
- `while`, `try`, `class` and `with` blocks;
- references to columns that do not exist.

`execute` starts a fresh `python -m app.agent.sandbox_worker` process. Input is a pickle sent from the trusted parent; output comes back as JSON only. The worker:
- removes network access by replacing socket creation;
- sets a CPU-time limit (a memory limit where the OS allows it; not on macOS);
- runs the code with a restricted builtins set, with `pd`, `np` and the app's `convert_amount()` helper preloaded;
- normalises the result to plain values.

*Rationale:* defence in depth. Static checks catch most problems with a precise message the model can act on; the subprocess limits the damage if something slips through. JSON-only output means nothing the generated code returns can execute in the main process (a returned pickle could). `convert_amount()` lets queries convert an amount named in the question (such as "USD 60,000 on 8 March") while keeping the rate logic in application code, with every call logged as evidence.

### 5.11 Orchestrator — `app/agent/orchestrator.py`

```python
class Agent:
    def __init__(self, llm, policy, tracer, data_context: Callable[[str, bool], DataContext])
    def ask(self, question, reporting_currency=None) -> AskResponse
```

`ask` opens a trace, runs the steps and composes one `AskResponse`. It also catches `LLMError` (status `ERROR` with a user-facing message) and any unexpected exception (status `ERROR`, kind `INTERNAL`; the traceback goes only to the trace). Composition rules:

| Situation | Status |
|---|---|
| Router: out of scope | `OUT_OF_SCOPE` (fixed message, no evidence) |
| Policy only, verified clauses | `ANSWERED`; no verified clause → `NO_EVIDENCE` |
| Data answered, all figures verified | `ANSWERED` |
| Data answered, some converted figure unavailable | `PARTIAL` + `FX_UNAVAILABLE` limitation |
| Data could not run, policy part answered | `PARTIAL` + `DATA_QUERY_FAILED` |
| Data could not run, no policy part | `QUERY_FAILED` + guidance |
| Data answered, policy part unsupported | `PARTIAL` + `POLICY_NO_EVIDENCE` |
| Model unreachable or misconfigured | `ERROR` (`LLM_UNAVAILABLE`, `LLM_NOT_CONFIGURED`, …) |

### 5.12 Tracing — `app/tracing/trajectory.py`

```python
class Tracer:  start(question, **context) -> Trace;  get(trace_id) -> dict | None
class Trace:   step(name, **data);  llm_step(name, result, prompt_version, input_text, **extra)
               finish(status, response)   # appends one JSON line to var/traces/YYYY-MM-DD.jsonl
```

Typical step sequence: `router`, `policy_answer`, `policy_verdict`, `data_context`, `codegen`, `exec`, `verify`, `explain`, `response`. The last 200 traces are kept in memory; older ones are read from disk. `GET /traces/{id}` returns a trace in dev mode.

### 5.13 HTTP layer — `app/api/routes.py` and `schemas.py`

| Endpoint | Returns | Notes |
|---|---|---|
| `POST /ask` `{question, reporting_currency?}` | `AskResponse` | 200 for every handled outcome; 422 invalid input; 503 + `Retry-After` when at capacity |
| `GET /summary` | `SummaryResponse` | deterministic, cached per FX version |
| `GET /health` | data / policy / FX / model status | |
| `GET /fx/status`, `POST /fx/refresh` | snapshot status, coverage | refresh is dev-only |
| `GET /traces/{id}` | full trajectory | dev-only |

`AskResponse` contains `trace_id`, `status`, `question`, `route` (the sources and rules applied), `answer` (text and up to 5 key figures; the UI shows 3), `evidence` (`policy[]`, `data`, `fx`), `limitations[]`, `caveats[]` and `error`. Money is always a decimal string. A middleware adds security headers; `/ask` responses are marked `no-store`.

*Rationale:* the Pydantic schemas are the single source of truth. The frontend's TypeScript types are generated from the OpenAPI schema, so the two sides cannot drift apart silently.

## 6. Frontend components

| Module | Role |
|---|---|
| `api/client.ts` | `getSummary()`, `ask()`; timeouts (30 s / 120 s), cancellation, and typed `ApiError` kinds (network, timeout, server, busy, invalid, cancelled) |
| `api/schema.d.ts`, `types.ts` | generated contract and convenient aliases |
| `state/chatReducer.ts` | conversation as data: turns that are pending, done or failed; saved in sessionStorage; a pending turn at reload is marked interrupted |
| `state/useChat.ts` | submit, retry, cancel and clear; one AbortController per question |
| `state/useSummary.ts` | loading / ready / error state with reload |
| `sources.ts` | derives the three "Based on" slots (policy, records, ECB rates) as verified, limited or not used, with wording, from the response |
| `format.ts` | money formatting from decimal strings (no floats), dates, column labels |
| `SummaryPanel` | ledger reconciliation with a double-rule total, status breakdown, largest vendors, data-quality flags, example questions |
| `AnswerCard` | status line, Based-on strip, answer text, key figures, limitations, notes, evidence, trace reference |
| `evidence/*` | tabs: Result, Policy clauses, Invoices, Exchange rates, How it was checked |

*Rationale:* the UI makes no judgement of its own. Everything it shows, including whether a slot is "verified", is derived from structured evidence the backend already verified. That keeps the trust boundary on the server.

## 7. Walkthroughs with real inputs

The following come from the final golden-eval run on ECB rates (14 Nov 2026, `gpt-5-mini`). Timings and step outputs are taken from the traces.

### 7.1 Page load: `GET /summary`

```
App mounts ─► useSummary ─► GET /api/summary ─► routes.summary
   FxViews.get("EUR")          converted invoice table (cached per FX version)
   FxService.status()          snapshot ids, latest rate date
   build_summary(...)          Decimal totals, reconciliation, groups, top 5
◄─ SummaryResponse: total "6886631.10"; GBP 1,182,251.02 → EUR 1,377,108.82;
   USD 1,150,445.53 → EUR 1,015,796.16; coverage IDENTITY 89 / EXACT 35 / FALLBACK 16
```

No model is called, so the panel is on screen within milliseconds of the page loading.

### 7.2 Policy question: "Who is allowed to approve an invoice of EUR 60,000?"

```
POST /ask ─► Agent.ask
 1 router.route        GPT 2.6 s  → sources [policy], intent policy_rule
 2 policy_answerer     GPT 7.4 s  → 6 citations; _verify: all found verbatim
      3.T3 "| 50,000.01 – 250,000 | Finance Director |"   + 3.1, 7.1, 7.2, 2.1, 4.1
 3 (no data step)
 4 compose             → ANSWERED, evidence.policy = 6 clauses
total 9.9 s
```

The UI shows "Answered from the evidence". The Based-on strip marks the policy as "Quoted and checked" and the other two slots as "Not used"; the Policy tab shows row 3.T3 with its column headers.

### 7.3 Data question: "What are our top 5 vendors by total spend in 2025?"

```
 1 router.route        GPT 3.9 s  → sources [records, fx], intent ranking, reporting EUR
   data_context         FxViews.get("EUR"): coverage 89 / 35 / 16 / 0
 2 codegen             GPT 12.8 s → groupby vendor, agg total_spend_eur=sum(amount_eur),
                                    invoice_count=count; head(5); declares both aggregations
   validate             ok
   sandbox.execute      ok, 7 columns, 85 supporting invoices
   verify               total_spend_eur: verified; invoice_count: verified
 3 explain #1           jargon ["amount_eur"] → one rewrite requested
   explain #2           placeholders only → "... BioReagent Direct (V-1008) with
                        EUR 1,131,552.52 across 19 invoices; PharmaPrint Studios ..."
 4 compose             → ANSWERED
total 40.6 s
```

Every figure in the final sentence was substituted by the app from the verified table. The explanation guard caught a column name in the first draft.

### 7.4 Hybrid: "Which invoices above the Finance Director approval threshold have no approver recorded?"

```
 1 router              → [policy, records, fx], compliance_check
 2 policy_answerer     (told: answer only the policy part) → 7 verified clauses incl. 3.T3, 3.1
 3 codegen             receives the verified clause texts; filters
                       50000.01 <= amount_eur <= 250000 and approved_by missing
   sandbox / verify     9 invoices (row list, no aggregates to recompute)
 4 explain + compose   → ANSWERED with policy, data and FX evidence
```

The threshold comes from the policy text the code generator was given, not from model memory. This step's design is the result of a live failure: originally, the policy step refused ("I cannot see invoices"), and the threshold never reached the code generator.

### 7.5 Amount named in the question: "Who should approve a USD 60,000 invoice dated 8 March 2025?"

```
 1 router              → [policy, fx] (a date is not an exact-date request)
 2 policy_answerer     → 3.T3, 3.T2, 5.1, ... verified
 3 codegen             eur = convert_amount(60000, "USD", "EUR", "2025-03-08")
   convert_amount      app code: Saturday → FALLBACK to Fri 2025-03-07,
                       60000 ÷ 1.0857 = 55,263.89; logged as an FX evidence row
   result               approver "Finance Director"
 4 compose             → ANSWERED; Exchange rates tab shows the conversion row
```

The answer text says the rate was "on the invoice date"; the evidence correctly shows the Friday rate and the fallback tag. This illustrates why the structured evidence, not the prose, is the authority (see §10).

### 7.6 Missing rates: "What is our total 2025 spend in EUR using only exact invoice-date exchange rates, with no fallback?"

```
 1 router              → exact_date_fx: true
   data_context         FxViews.get("EUR", exact=True): 16 invoices UNAVAILABLE
 2 codegen             filters fx_status in [EXACT, IDENTITY], sums amount_eur
   sandbox.execute      primary run: 124 invoices, total 6,202,080.03   ← a partial sum
                        probe runs: the 16 disguised invoices are included
   verify               affected = 16 → total_spend_eur: unavailable (null)
 3 explain             "... is unavailable" (after one jargon rewrite)
 4 compose             → PARTIAL + FX_UNAVAILABLE listing the 16 invoices
```

This is principle P3 in action. The model's code produced a plausible but incomplete EUR 6,202,080.03, and the app refused to present it as the total.

### 7.7 Questions the sources cannot answer

- *"What is our total spend by region?"* The router sends it to records. In codegen, GPT declines ("no region field; the closest is vendor country"). Result: `QUERY_FAILED`, with plain-language guidance and the list of available fields, in 7.4 s.
- *"What is today's weather?"* The router returns out of scope, so nothing else runs. Result: `OUT_OF_SCOPE`, in 3.0 s.

## 8. Error handling and states

| Condition | Behaviour |
|---|---|
| Invalid CSV or policy at startup | App refuses to start, naming the problem |
| ECB fetch fails, cache valid | Cache used; status shows snapshot age |
| ECB fetch fails, no cache | EUR consolidation unavailable; original currencies still shown |
| ECB response malformed | Rejected; previous snapshot kept; reason in `/fx/status` |
| Rate missing for an invoice | Converted figures over it become null; invoices listed |
| Model error or timeout | `ERROR` with a retryable message; summary unaffected |
| Generated code invalid or failing twice | `QUERY_FAILED` with explanation |
| Too many concurrent questions | HTTP 503; UI says the assistant is busy |

In the UI: a summary skeleton and retry; per-question progress with elapsed time and Cancel; error cards with "Ask again" and "Edit the question"; the conversation is kept across reloads.

## 9. How correctness is checked

- **Backend tests (220, offline):**
  - data validation, clause parsing and quote checks;
  - the ECB parser (including Easter and TARGET holidays), cache integrity and client allow-list;
  - every conversion direction, fallback case and rounding rule;
  - the summary against an independent standard-library reference (to the cent), including FX-down modes;
  - the sandbox's rejections and limits;
  - verification, repair, probes and orchestration with a scripted model;
  - API status codes and headers.
- **Frontend tests (24):** formatting, reducer, Based-on logic, loading, error and busy states, evidence tabs and keyboard use, and axe accessibility checks.
- **Golden evals (32 live questions):** expected values come from `evals/reference.py`, which shares no code with the app. 32/32 passed with ECB rates (and twice with H.10 before the switch).

*Rationale:* the reference implementations are deliberately written separately (standard library only, own fallback loop), so a bug in the app's conversion cannot also hide in the expected values.

## 10. Trade-offs and known limitations

- **Latency (6–55 s per question).** Four separate model calls (route, policy, code, explain) are slower than one, but each has a narrow, verifiable job. That is what makes the checks possible.
- **Intent is not guaranteed.** The app guarantees that shown numbers match the data and rates and that quotes are verbatim. It cannot guarantee that the query matches what the analyst meant. The "How it was checked" tab shows the generated query and filters so this can be reviewed.
- **Prose can be imprecise.** Explanations can be loosely worded (for example, "invoice-date rate" when an earlier rate was used); the structured evidence is authoritative and always shown.
- **Weekend and holiday conversions** use the latest earlier ECB rate, which policy §5.1 does not cover; they are flagged for review.
- **Each question stands alone;** no conversation context is sent to the model.
- **The sandbox memory limit** is not enforceable on macOS (CPU and time limits are).

## 11. Extending the system

- **Add a currency (e.g. CHF):** add a `SeriesSpec` in `fx/series.py`, extend `CURRENCIES` and the allowed currencies in the data store, and refresh the test fixtures with `scripts/fetch_ecb_fixture.py`. The conversion formula already generalises.
- **Change the model:** set `OPENAI_MODEL`; run `python -m evals.run_golden` to confirm behaviour.
- **Change a prompt:** edit `agent/prompts/*.md`; its version hash changes automatically in traces; re-run the evals.
- **Change the API:** edit `api/schemas.py`, export the OpenAPI schema and run `npm run gen:api`; TypeScript will flag every UI place that needs updating.
