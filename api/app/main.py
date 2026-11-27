from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.config import Settings, load_settings
from app.flagsmith_service import FlagsmithService
from app.schemas import (
    CheckStatusRequest,
    CreateTrialRequest,
    CreateTrialResponse,
    DisplayedRequest,
    DisplayedResponse,
    HealthResponse,
    ReceiptResponse,
    SessionResponse,
    SubmitTrialRequest,
    SummaryResponse,
    TrialDetailResponse,
)
from app.security import (
    get_verified_visitor_id,
    set_visitor_cookie,
    validate_origin,
    verify_visitor_cookie,
)
from app.store import StoreError, TrialStore
from app.store_pg import PostgresTrialStore, create_pool, run_schema_migrations

# psycopg async requires SelectorEventLoop on Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def default_now() -> datetime:
    return datetime.now(timezone.utc)


def create_app(
    settings: Settings | None = None,
    store: TrialStore | None = None,
    flagsmith: FlagsmithService | None = None,
    now_fn: Callable[[], datetime] = default_now,
) -> FastAPI:
    resolved_settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI):
        pool = None
        if store is None:
            pool = await create_pool(resolved_settings.database_url)
            await run_schema_migrations(pool)
            app_instance.state.store = PostgresTrialStore(pool)
        else:
            app_instance.state.store = store

        if flagsmith is None:
            app_instance.state.flagsmith = FlagsmithService(resolved_settings)
        else:
            app_instance.state.flagsmith = flagsmith

        app_instance.state.settings = resolved_settings
        app_instance.state.now_fn = now_fn
        try:
            yield
        finally:
            app_instance.state.flagsmith.close()
            if pool is not None:
                await pool.close()

    app = FastAPI(
        title="One Copy Experiment API",
        version="1.0.0",
        redirect_slashes=False,
        lifespan=lifespan,
    )

    @app.exception_handler(StoreError)
    async def store_error_handler(_request: Request, exc: StoreError) -> JSONResponse:
        body: dict[str, Any] = {"detail": exc.detail, **exc.extra}
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.middleware("http")
    async def no_store_cache_header(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def get_settings_dep(request: Request) -> Settings:
        return request.app.state.settings

    def get_store_dep(request: Request) -> TrialStore:
        return request.app.state.store

    def get_flagsmith_dep(request: Request) -> FlagsmithService:
        return request.app.state.flagsmith

    def get_now_dep(request: Request) -> datetime:
        return request.app.state.now_fn()

    def require_origin_dep(
        request: Request,
        cfg: Settings = Depends(get_settings_dep),
    ) -> None:
        validate_origin(request, cfg)

    def require_visitor_dep(
        request: Request,
        cfg: Settings = Depends(get_settings_dep),
    ) -> UUID:
        return get_verified_visitor_id(request, cfg)

    @app.get("/api/health", response_model=HealthResponse)
    async def health(
        cfg: Settings = Depends(get_settings_dep),
        trial_store: TrialStore = Depends(get_store_dep),
        fs: FlagsmithService = Depends(get_flagsmith_dep),
        now: datetime = Depends(get_now_dep),
    ) -> HealthResponse:
        db_ok = await trial_store.health_check()
        return HealthResponse(
            status="ok",
            database="ok" if db_ok else "unavailable",
            flagsmithConfigured=fs.is_configured,
            serverNow=now,
        )

    @app.post("/api/session", response_model=SessionResponse)
    async def create_or_resume_session(
        request: Request,
        response: Response,
        _: None = Depends(require_origin_dep),
        cfg: Settings = Depends(get_settings_dep),
        trial_store: TrialStore = Depends(get_store_dep),
        now: datetime = Depends(get_now_dep),
    ) -> SessionResponse:
        cookie_val = request.cookies.get(cfg.cookie_name)
        visitor_id = verify_visitor_cookie(cookie_val, cfg.cookie_secret)
        if visitor_id is None:
            visitor_id = uuid4()
            set_visitor_cookie(response, visitor_id, cfg)

        active_trial_id = await trial_store.get_active_trial_id(
            visitor_id, cfg.experiment_name, cfg.app_cohort
        )
        return SessionResponse(
            visitorSuffix=visitor_id.hex[-6:].upper(),
            activeTrialId=active_trial_id,
            serverNow=now,
        )

    @app.post("/api/trials", response_model=CreateTrialResponse, status_code=201)
    async def create_trial(
        body: CreateTrialRequest,
        _: None = Depends(require_origin_dep),
        visitor_id: UUID = Depends(require_visitor_dep),
        cfg: Settings = Depends(get_settings_dep),
        trial_store: TrialStore = Depends(get_store_dep),
        now: datetime = Depends(get_now_dep),
    ) -> CreateTrialResponse:
        trial_id, state, title = await trial_store.create_trial(
            visitor_id=visitor_id,
            experiment=cfg.experiment_name,
            enrolled_cohort=cfg.app_cohort,
            title=body.title,
            now=now,
        )
        return CreateTrialResponse(trialId=trial_id, state=state, title=title)

    @app.post("/api/trials/{trial_id}/submit")
    async def submit_trial(
        trial_id: UUID,
        body: SubmitTrialRequest,
        _: None = Depends(require_origin_dep),
        visitor_id: UUID = Depends(require_visitor_dep),
        cfg: Settings = Depends(get_settings_dep),
        trial_store: TrialStore = Depends(get_store_dep),
        fs: FlagsmithService = Depends(get_flagsmith_dep),
        now: datetime = Depends(get_now_dep),
    ) -> JSONResponse:
        status_code, payload = await trial_store.submit_trial(
            trial_id=trial_id,
            visitor_id=visitor_id,
            action_id=body.actionId,
            intent=body.intent,
            settings=cfg,
            flagsmith=fs,
            now=now,
        )
        return JSONResponse(status_code=status_code, content=payload)

    @app.post("/api/trials/{trial_id}/check-status")
    async def check_status(
        trial_id: UUID,
        body: CheckStatusRequest | None = None,
        _: None = Depends(require_origin_dep),
        visitor_id: UUID = Depends(require_visitor_dep),
        trial_store: TrialStore = Depends(get_store_dep),
        now: datetime = Depends(get_now_dep),
    ) -> JSONResponse:
        action_id = body.actionId if body is not None else None
        status_code, payload = await trial_store.check_status(
            trial_id=trial_id,
            visitor_id=visitor_id,
            action_id=action_id,
            now=now,
        )
        return JSONResponse(status_code=status_code, content=payload)

    @app.get("/api/trials/{trial_id}", response_model=TrialDetailResponse)
    async def get_trial(
        trial_id: UUID,
        visitor_id: UUID = Depends(require_visitor_dep),
        trial_store: TrialStore = Depends(get_store_dep),
        now: datetime = Depends(get_now_dep),
    ) -> TrialDetailResponse:
        detail = await trial_store.get_trial(
            trial_id=trial_id,
            visitor_id=visitor_id,
            now=now,
        )
        return TrialDetailResponse(**detail)

    @app.get("/api/receipts/{code}", response_model=ReceiptResponse)
    async def get_receipt(
        code: str,
        visitor_id: UUID = Depends(require_visitor_dep),
        trial_store: TrialStore = Depends(get_store_dep),
    ) -> ReceiptResponse:
        receipt = await trial_store.get_receipt(
            receipt_code=code,
            visitor_id=visitor_id,
        )
        return ReceiptResponse(**receipt)

    @app.post("/api/receipts/{code}/displayed", response_model=DisplayedResponse)
    async def mark_receipt_displayed(
        code: str,
        body: DisplayedRequest,
        _: None = Depends(require_origin_dep),
        visitor_id: UUID = Depends(require_visitor_dep),
        cfg: Settings = Depends(get_settings_dep),
        trial_store: TrialStore = Depends(get_store_dep),
        fs: FlagsmithService = Depends(get_flagsmith_dep),
        now: datetime = Depends(get_now_dep),
    ) -> JSONResponse:
        status_code, payload = await trial_store.mark_receipt_displayed(
            receipt_code=code,
            visitor_id=visitor_id,
            action_id=body.actionId,
            enrolled_cohort=cfg.app_cohort,
            flagsmith=fs,
            now=now,
        )
        return JSONResponse(status_code=status_code, content=payload)

    @app.get("/api/stats/summary", response_model=SummaryResponse)
    async def get_summary(
        cfg: Settings = Depends(get_settings_dep),
        trial_store: TrialStore = Depends(get_store_dep),
        now: datetime = Depends(get_now_dep),
    ) -> SummaryResponse:
        summary = await trial_store.get_summary(
            cfg.experiment_name,
            enrolled_cohort=cfg.app_cohort,
            now=now,
        )
        return SummaryResponse(**summary)

    return app


app = create_app()
