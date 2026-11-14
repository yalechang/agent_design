"""FastAPI entry point.

Run from ``backend/``:  .venv/bin/uvicorn app.main:app --reload
"""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.llm import LLM, OpenAILLM
from app.agent.data_answerer import DataContext
from app.agent.orchestrator import Agent
from app.api.routes import router
from app.config import Settings, get_settings
from app.data.store import load_datastore
from app.fx.cache import SnapshotCache
from app.fx.convert import MAX_FALLBACK_DAYS
from app.fx.enrich import FxViews
from app.fx.series import SERIES
from app.fx.service import FxService
from app.policy.clauses import load_policy
from app.tracing.trajectory import Tracer

log = logging.getLogger("app")


def create_app(settings: Settings | None = None, fx_service: FxService | None = None,
               llm: LLM | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Invalid data or policy raises here, so the app refuses to start.
        app.state.settings = settings
        store = load_datastore(settings.invoices_path, settings.vendors_path)
        app.state.store = store
        app.state.policy = load_policy(settings.policy_path)
        settings.var_dir.mkdir(parents=True, exist_ok=True)

        # ECB snapshots must cover every publication day the invoices (and their 7-day fallback) can need.
        lo, hi = store.date_range
        fx = fx_service or FxService(SnapshotCache(settings.fx_cache_dir),
                                     required_coverage=(lo - timedelta(days=MAX_FALLBACK_DAYS), hi))
        fx.load_cached()
        app.state.fx = fx
        app.state.fx_views = FxViews(store, fx)

        key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
        app.state.llm = llm or OpenAILLM(key, settings.openai_model, settings.openai_reasoning_effort,
                                         settings.openai_timeout_s)
        app.state.tracer = Tracer(settings.traces_dir)
        def data_context(reporting_currency: str, exact_date: bool) -> DataContext:
            view = app.state.fx_views.get(reporting_currency, exact_date)
            return DataContext(store=store, view=view, rates=fx.rates())

        app.state.agent = Agent(app.state.llm, app.state.policy, app.state.tracer, data_context)
        app.state.ask_slots = threading.BoundedSemaphore(settings.max_concurrent_asks)
        if not key:
            log.warning("OPENAI_API_KEY is not set: /summary works, /ask will report LLM_NOT_CONFIGURED")
        if settings.dev_mode:
            log.warning("DEV_MODE is on: /traces/{id} and POST /fx/refresh are exposed; set DEV_MODE=false "
                        "outside local development")
        if settings.fx_refresh_on_startup and fx.needs_refresh(timedelta(hours=settings.fx_max_age_hours)):
            # Background, so the API (and original-currency features) are up immediately.
            threading.Thread(target=_safe_refresh, args=(fx,), name="ecb-rate-refresh", daemon=True).start()

        log.info("Loaded %d invoices, %d vendors, %d policy clauses; FX cached: %s",
                 len(store.invoices), len(store.vendors), len(app.state.policy.clauses),
                 [c for c in SERIES if c not in fx.missing_series()])
        yield

    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = FastAPI(title="Procurement Q&A Agent", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
        expose_headers=["X-Trace-Id"],
    )
    app.include_router(router)
    return app


def _safe_refresh(fx: FxService) -> None:
    try:
        fx.refresh()
    except Exception as e:  # never crash the app because the Fed site is unreachable
        log.warning("ECB rate startup refresh failed: %s", e)


app = create_app()
