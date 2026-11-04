# app/main.py

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.config import settings
from app.database import init_db, close_db
from app.routes import subscribe, notify, push_id, inbox
from app import cron

# Status and our own messages only. No request bodies, no client IPs.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
)
logging.getLogger("uvicorn.access").disabled = True
logging.getLogger("uvicorn.access").propagate = False
# httpx logs every request URL at INFO, and an APNs URL contains the device's
# push token. Only warnings and errors from these libraries are kept.
for noisy in ("httpx", "httpcore", "hpack", "h2"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting AfterCare backend")
    await init_db()

    dispatcher_task = None
    cleanup_task = None
    if settings.run_background_jobs:
        dispatcher_task = asyncio.create_task(cron.run_dispatcher())
        cleanup_task = asyncio.create_task(cron.run_cleanup())
        logger.info("Background jobs started")

    yield

    if dispatcher_task:
        dispatcher_task.cancel()
    if cleanup_task:
        cleanup_task.cancel()
    await close_db()
    logger.info("AfterCare backend stopped")


def app_kwargs(environment: str) -> dict:
    # In production the API schema is not public either, not just /docs.
    dev = environment == "development"
    return dict(
        title="AfterCare API",
        description="Anonymous STI exposure notification backend",
        version="1.0.0",
        docs_url="/docs" if dev else None,
        redoc_url=None,
        openapi_url="/openapi.json" if dev else None,
        lifespan=lifespan,
    )


app = FastAPI(**app_kwargs(settings.environment))

app.include_router(subscribe.router)
app.include_router(notify.router)
app.include_router(push_id.router)
app.include_router(inbox.router)


@app.middleware("http")
async def strip_forwarded_client(request: Request, call_next):
    """
    Ignore client identity headers so application code cannot accidentally
    treat a forwarded IP as a user identifier. Real stripping still belongs
    on the load balancer (see comment in Procfile).
    """
    headers = request.scope.get("headers")
    if headers is not None:
        request.scope["headers"] = [
            (k, v)
            for k, v in headers
            if k.lower() not in (b"x-forwarded-for", b"x-real-ip", b"forwarded")
        ]
    return await call_next(request)


_LOGGABLE_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}


class CatchUnexpectedErrors:
    """
    Stop any unexpected exception inside the app. It is never re-raised, so
    Starlette's ServerErrorMiddleware and uvicorn never log its message or
    traceback (an asyncpg error message can hold a stored hash). The only log
    line carries the method, the route template and the exception type name.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            method = scope.get("method")
            if method not in _LOGGABLE_METHODS:
                method = "OTHER"
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            logger.error("Unhandled %s on %s %s", type(exc).__name__, method, route)
            # No route streams, and uvicorn's message for a cut-off response is fixed text.
            if not started:
                response = JSONResponse({"detail": "internal_error"}, status_code=500)
                await response(scope, receive, send)


app.add_middleware(CatchUnexpectedErrors)


# ctx keys that come from the schema or are counts, never from the input.
_SAFE_CTX_KEYS = {
    "min_length", "max_length", "expected_length", "gt", "ge", "lt", "le",
    "multiple_of", "pattern", "expected", "allowed_lengths", "ordered",
}
# Types whose msg is fixed text of ours or of FastAPI.
_FIXED_MSG_TYPES = {"value_error", "json_invalid"}


def _clean_error(err: dict) -> dict:
    ctx = err.get("ctx") or {}
    msg = err.get("msg", "")
    if err.get("type") not in _FIXED_MSG_TYPES and not set(ctx) <= _SAFE_CTX_KEYS:
        # e.g. uuid_parsing quotes a character of the input in its message.
        msg = "Invalid value"
    return {"loc": err.get("loc", []), "type": err.get("type", ""), "msg": msg}


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    # Never log the body, and never echo input, ctx or url back.
    return JSONResponse(
        {"detail": [_clean_error(e) for e in exc.errors()]}, status_code=422
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
