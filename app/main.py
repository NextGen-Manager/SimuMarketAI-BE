from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.correlation import CORRELATION_HEADER, CorrelationIdMiddleware
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.core.security_headers import SecurityHeadersMiddleware
from app.persistence.database import dispose_engine
from app.persistence.redis import dispose_redis


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()
    await dispose_redis()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.debug)

    app = FastAPI(
        title="SimuMarket AI API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
    )

    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[CORRELATION_HEADER],
    )
    # Added last so it wraps CORS short-circuit responses as well as routes.
    app.add_middleware(
        SecurityHeadersMiddleware,
        enable_hsts=settings.environment in {"staging", "production"},
    )

    register_error_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
