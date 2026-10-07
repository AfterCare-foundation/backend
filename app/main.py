# app/main.py

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request

from app.config import settings
from app.database import init_db, close_db
from app.routes import subscribe, notify, push_id, dev_inbox
from app import cron

# Status and our own messages only. No request bodies, no client IPs.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
)
logging.getLogger("uvicorn.access").disabled = True
logging.getLogger("uvicorn.access").propagate = False

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


docs_url = "/docs" if settings.environment == "development" else None

app = FastAPI(
    title="AfterCare API",
    description="Anonymous STI exposure notification backend",
    version="1.0.0",
    docs_url=docs_url,
    redoc_url=None,
    lifespan=lifespan,
)

app.include_router(subscribe.router)
app.include_router(notify.router)
app.include_router(push_id.router)
app.include_router(dev_inbox.router)


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


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    # Default handler does not log the body. Keep it that way.
    return await request_validation_exception_handler(request, exc)


@app.get("/health")
async def health():
    return {"status": "ok"}
