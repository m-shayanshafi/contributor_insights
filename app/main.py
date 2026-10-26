"""FastAPI application factory.

Run with: uv run uvicorn app.main:create_app --factory
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.ratelimit import ClientRateLimiter
from app.api.routes import router
from app.config import Settings
from app.logging_setup import configure_logging
from app.services import Services, open_services

logger = logging.getLogger(__name__)


ServicesFactory = Callable[[Settings], AbstractAsyncContextManager[Services]]


def create_app(settings: Settings | None = None,
               services_factory: ServicesFactory = open_services) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.secret_values())
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Starting: repo=%s data_source=%s llm_mode=%s",
            settings.target_repo, settings.data_source, settings.llm_mode,
        )
        async with services_factory(settings) as services:
            app.state.services = services
            yield

    app = FastAPI(title="Contributor Insights", lifespan=lifespan)
    app.state.settings = settings
    # Enforced on every /api route by the router's dependency; /healthz is not limited.
    app.state.rate_limiter = ClientRateLimiter(settings.rate_limit)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        # Log details server-side; never return stack traces or upstream bodies.
        logger.exception("Unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {
            "status": "ok",
            "repo": settings.target_repo,
            "data_source": settings.data_source,
            "llm_mode": settings.llm_mode,
        }

    app.include_router(router)
    return app
