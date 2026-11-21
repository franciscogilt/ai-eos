"""FastAPI application factory (modular monolith entrypoint)."""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TextIO

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from opentelemetry import trace

from aieos import __version__
from aieos.api.auth import TokenVerifier, jwks_file_resolver, jwks_url_resolver
from aieos.api.routes import approvals, health, me, orgs, projects, repositories, workflows
from aieos.core.errors import (
    AuthenticationError,
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
)
from aieos.core.ids import uuid7
from aieos.core.logging import configure_logging, get_logger
from aieos.core.settings import Settings, TemporalSettings
from aieos.observability.telemetry import current_trace_id, setup_tracing
from aieos.orchestration.client import Orchestrator, TemporalOrchestrator, connect
from aieos.persistence.db import Database

log = get_logger("aieos.api")
_tracer = trace.get_tracer("aieos.api")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")

_STATUS: dict[type[DomainError], int] = {
    AuthenticationError: 401,
    ForbiddenError: 403,
    NotFoundError: 404,
    ConflictError: 409,
    ServiceUnavailableError: 503,
}


def build_verifier(settings: Settings) -> TokenVerifier:
    if settings.oidc_jwks_path is not None:
        resolver = jwks_file_resolver(settings.oidc_jwks_path)
    else:
        resolver = jwks_url_resolver(str(settings.oidc_jwks_url))
    return TokenVerifier(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        algorithms=settings.oidc_algorithms,
        resolve_key=resolver,
    )


def _error(status: int, code: str, message: str, request: Request) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    body = {"error": {"code": code, "message": message, "request_id": getattr(request.state, "request_id", None)}}
    return JSONResponse(status_code=status, content=body, headers=headers)


class UnavailableOrchestrator:
    """Used when Temporal is not reachable at startup: workflow actions return 503, the rest of the API works."""

    def __getattr__(self, name: str) -> object:
        async def unavailable(*_args: object, **_kwargs: object) -> None:
            raise ServiceUnavailableError("orchestrator unavailable; retry later")

        return unavailable


def create_app(
    settings: Settings | None = None,
    *,
    log_stream: TextIO | None = None,
    orchestrator: Orchestrator | None = None,
) -> FastAPI:
    settings = settings or Settings()  # values come from the environment
    configure_logging(level=settings.log_level, json=settings.log_json, stream=log_stream)
    setup_tracing(
        service_name=settings.otel_service_name,
        environment=settings.environment,
        otlp_endpoint=settings.otel_exporter_otlp_endpoint,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(
            settings.database_url.get_secret_value(),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            statement_timeout_ms=settings.db_statement_timeout_ms,
        )
        try:
            await db.assert_runtime_role_is_safe()
        except Exception:
            await db.dispose()
            raise
        app.state.db = db
        if orchestrator is not None:
            app.state.orchestrator = orchestrator
        elif settings.orchestrator_mode == "temporal":
            try:
                app.state.orchestrator = TemporalOrchestrator(await connect(TemporalSettings()))
            except Exception:
                log.exception("orchestrator.connect_failed")
                app.state.orchestrator = UnavailableOrchestrator()
        else:
            app.state.orchestrator = UnavailableOrchestrator()
        log.info("api.started", environment=settings.environment, version=__version__)
        try:
            yield
        finally:
            await db.dispose()
            log.info("api.stopped")

    docs_enabled = settings.environment in ("local", "test")
    app = FastAPI(
        title="AI-EOS Platform API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.state.settings = settings
    app.state.verifier = build_verifier(settings)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID.match(incoming) else str(uuid7())
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        with _tracer.start_as_current_span(f"{request.method} {request.url.path}") as span:
            trace_id = current_trace_id()
            if trace_id:
                structlog.contextvars.bind_contextvars(trace_id=trace_id)
            try:
                response = await call_next(request)
            except Exception:
                log.exception("http.unhandled_error", method=request.method, path=request.url.path)
                response = _error(500, "internal_error", "internal server error", request)
            route = request.scope.get("route")
            if route is not None and hasattr(route, "path"):
                span.update_name(f"{request.method} {route.path}")
            span.set_attribute("http.response.status_code", response.status_code)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        # Path only: query strings may carry tokens in misbehaving clients.
        log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        structlog.contextvars.clear_contextvars()
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        status = _STATUS.get(type(exc), 400)
        if status == 401:
            log.info("auth.rejected", reason=exc.message)
        return _error(status, exc.code, exc.message, request)

    for router in (
        health.router,
        me.router,
        orgs.router,
        projects.router,
        repositories.router,
        workflows.router,
        approvals.router,
    ):
        app.include_router(router)
    return app
