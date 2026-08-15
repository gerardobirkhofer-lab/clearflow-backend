"""
FastAPI application entry point.
Includes all routers, middleware, exception handlers, and app factory.
"""
from __future__ import annotations
import os

import uuid

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
import structlog

from .core.config import Settings, get_settings
from .core.database import engine, init_db
from .core.redis import get_redis
from .api.v1 import (
    auth,
    tenants,
    institutions,
    collections,
    bank_statements,
    tpv_reports,
    fee_structures,
    reconciliation,
    cash_flow,
    morning_reports,
    uploads,
    webhooks,
    dashboard,
)

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """Application lifespan events: startup and shutdown."""
    # Startup
    settings = get_settings()
    logger.info("app_starting", environment="development")

    # Initialize database (create tables if they don't exist)
    await init_db()

    # Test Redis connection
    redis = await get_redis()
    await redis.ping()
    logger.info("redis_connected")

    yield

    # Shutdown
    logger.info("app_shutting_down")
    await engine.dispose()


def create_app() -> FastAPI:
    """Application factory pattern."""
    settings = get_settings()

    app = FastAPI(
        title="ClearFlow API",
        description="Reconciliation and cash flow management for SMEs",
        version="1.0.0",
        docs_url="/docs" if True else None,
        redoc_url="/redoc" if True else None,
        lifespan=lifespan,
    )
    # CORS
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173")
    allow_origins = [
        "http://localhost:3000",
        "http://localhost:5173",
        "https://clearflow-demo.vercel.app",
    ]
    if frontend_url and frontend_url not in allow_origins:
        allow_origins.append(frontend_url)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request logging middleware
    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            path=request.url.path,
            method=request.method,
            request_id=str(uuid.uuid4())[:8],
        )
        response = await call_next(request)
        logger.info(
            "request_completed",
            status_code=response.status_code,
        )
        return response

    # Exception handlers
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        logger.warning("validation_error", errors=exc.errors())
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": "Validation error", "errors": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        logger.error("unhandled_exception", error=str(exc))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error"},
        )

    # Health check
    @app.get("/health", tags=["system"])
    async def health_check():
        return {"status": "healthy", "version": "1.0.0"}

    # Include all API routers
    app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
    app.include_router(tenants.router, prefix="/api/v1", tags=["tenants"])
    app.include_router(institutions.router, prefix="/api/v1", tags=["institutions"])
    app.include_router(collections.router, prefix="/api/v1", tags=["collections"])
    app.include_router(bank_statements.router, prefix="/api/v1", tags=["bank-statements"])
    app.include_router(tpv_reports.router, prefix="/api/v1", tags=["tpv-reports"])
    app.include_router(fee_structures.router, prefix="/api/v1", tags=["fee-structures"])
    app.include_router(reconciliation.router, prefix="/api/v1", tags=["reconciliation"])
    app.include_router(cash_flow.router, prefix="/api/v1", tags=["cash-flow"])
    app.include_router(morning_reports.router, prefix="/api/v1", tags=["morning-reports"])
    app.include_router(uploads.router, prefix="/api/v1", tags=["uploads"])
    app.include_router(webhooks.router, prefix="/api/v1", tags=["webhooks"])
    app.include_router(dashboard.router, prefix="/api/v1", tags=["dashboard"])

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
