from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from app.api.schemas import (ComponentStatus, DataHealth, FxCoverage, FxSeriesStatus, FxStatusResponse,
                             AskRequest, AskResponse, HealthResponse, LLMHealth, PolicyHealth,
                             SummaryResponse)
from app.fx.service import RefreshInProgress
from app.summary import build_summary

router = APIRouter()


def _fx_status(request: Request) -> FxStatusResponse:
    fx = request.app.state.fx
    st = fx.status()
    view = request.app.state.fx_views.get("EUR")
    return FxStatusResponse(
        available=st["available"],
        refreshing=st["refreshing"],
        last_refresh_attempt=st["last_refresh_attempt"],
        series={k: FxSeriesStatus(**v) for k, v in st["series"].items()},
        eur_invoice_coverage=FxCoverage(
            reporting_currency="EUR",
            counts=view.coverage,
            complete=view.complete,
            unavailable_invoices=view.unavailable,
        ),
    )


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    state = request.app.state
    store, policy, settings = state.store, state.policy, state.settings

    data = DataHealth(
        status="ok",
        invoice_count=len(store.invoices),
        vendor_count=len(store.vendors),
        date_range=store.date_range,
        file_sha256=store.file_sha256,
    )
    pol = PolicyHealth(
        status="ok",
        document=policy.doc_label,
        clause_count=len(policy.clauses),
        sha256=policy.sha256,
    )
    fxs = _fx_status(request)
    if fxs.available:
        detail = None if fxs.eur_invoice_coverage.complete else (
            f"{len(fxs.eur_invoice_coverage.unavailable_invoices)} invoice(s) have no acceptable ECB reference rate")
        fx = ComponentStatus(status="ok", detail=detail)
    else:
        missing = [k for k, v in fxs.series.items() if v.state != "ok"]
        fx = ComponentStatus(status="unavailable",
                             detail=f"no validated ECB reference-rate snapshot for {', '.join(missing)}"
                                    + (" (refresh in progress)" if fxs.refreshing else ""))
    has_key = settings.openai_api_key is not None and bool(settings.openai_api_key.get_secret_value())
    llm = LLMHealth(
        status="ok" if has_key else "not_configured",
        model=settings.openai_model,
        detail=None if has_key else "OPENAI_API_KEY is not set",
    )

    overall = "ok" if all(c.status == "ok" for c in (data, pol, fx, llm)) else "degraded"
    return HealthResponse(status=overall, data=data, policy=pol, fx=fx, llm=llm)


@router.get("/fx/status", response_model=FxStatusResponse)
def fx_status(request: Request) -> FxStatusResponse:
    return _fx_status(request)


@router.post("/fx/refresh", response_model=FxStatusResponse)
def fx_refresh(request: Request) -> FxStatusResponse:
    if not request.app.state.settings.dev_mode:
        raise HTTPException(status_code=404)
    try:
        request.app.state.fx.refresh()
    except RefreshInProgress as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _fx_status(request)


@router.get("/summary", response_model=SummaryResponse)
def summary(request: Request) -> SummaryResponse:
    """Deterministic overview; cached until the active ECB rate snapshots change."""
    state = request.app.state
    view = state.fx_views.get("EUR")
    cached = getattr(state, "summary_cache", None)
    if cached and cached[0] == view.fx_version:
        return cached[1]
    series = {k: FxSeriesStatus(**v) for k, v in state.fx.status()["series"].items()}
    result = build_summary(state.store, state.policy, view, series)
    state.summary_cache = (view.fx_version, result)
    return result


@router.post("/ask", response_model=AskResponse,
             responses={503: {"description": "Too many questions are being answered; retry shortly"}})
def ask(body: AskRequest, request: Request, response: Response) -> AskResponse:
    """Every handled outcome (answered, no evidence, out of scope, LLM down...) is HTTP 200 with a status.
    HTTP 503 means the server is at its concurrency limit and nothing was attempted."""
    slots = request.app.state.ask_slots
    if not slots.acquire(blocking=False):
        raise HTTPException(status_code=503, headers={"Retry-After": "15"},
                            detail="Too many questions are being answered right now. Try again shortly.")
    try:
        result = request.app.state.agent.ask(body.question, body.reporting_currency)
    finally:
        slots.release()
    response.headers["X-Trace-Id"] = result.trace_id
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/traces/{trace_id}")
def get_trace(trace_id: str, request: Request) -> dict:
    if not request.app.state.settings.dev_mode:
        raise HTTPException(status_code=404)
    trace = request.app.state.tracer.get(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return trace
