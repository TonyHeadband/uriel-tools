import hmac
import json
import os
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg_pool import AsyncConnectionPool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from uriel_tools.db import apply_migrations
from uriel_tools.rag import jobs, store

MAX_BODY = 1024 * 1024


@dataclass(frozen=True)
class JobRequest:
    kind: str
    folder_id: int
    path: str
    settle: bool = False  # wait `settle_s` so a burst of events folds into this one pending job


class PayloadError(ValueError):
    pass


Handler = Callable[[dict, list[store.Folder]], list[JobRequest]]


def _handlers() -> dict[str, Handler]:
    from uriel_tools.events import nextcloud  # imports JobRequest from here

    return {"nextcloud": nextcloud.jobs_for}


def source_keys(environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    prefix = "URIEL_EVENTS_KEY_"
    return {k[len(prefix) :].lower(): v for k, v in environ.items() if k.startswith(prefix) and v}


async def _read_capped(request: Request) -> bytes | None:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY:
        return None
    body = bytearray()
    async for part in request.stream():
        body += part
        if len(body) > MAX_BODY:
            return None
    return bytes(body)


def build_events_app(
    pool: AsyncConnectionPool,
    keys: dict[str, str],
    handlers: dict[str, Handler] | None = None,
    settle_s: float = 60.0,
) -> Starlette:
    handlers = handlers if handlers is not None else _handlers()

    async def receive(request: Request):
        source = request.path_params["source"]
        handler, key = handlers.get(source), keys.get(source)
        if handler is None or key is None:
            return JSONResponse({"error": "unknown source"}, status_code=404)
        got = request.headers.get("x-uriel-source-key", "")
        if not hmac.compare_digest(got.encode(), key.encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await _read_capped(request)
        if body is None:
            return JSONResponse({"error": "body too large"}, status_code=413)
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise PayloadError("payload is not an object")
            requests = handler(payload, await store.list_folders(pool))
            for r in requests:
                run_after = datetime.now(UTC) + timedelta(seconds=settle_s) if r.settle else None
                await jobs.enqueue(pool, r.kind, r.folder_id, r.path, run_after=run_after)
        except (json.JSONDecodeError, UnicodeDecodeError, PayloadError):
            return JSONResponse({"error": "unreadable payload"}, status_code=422)
        except psycopg.Error:  # includes the pool's timeout: the nightly reconcile catches up
            return JSONResponse({"error": "unavailable"}, status_code=503)
        return JSONResponse({"queued": len(requests)}, status_code=202)

    async def livez(request: Request):
        return PlainTextResponse("ok")

    @asynccontextmanager
    async def lifespan(app):
        await pool.open()
        await apply_migrations(pool)
        yield
        await pool.close()

    return Starlette(
        routes=[Route("/events/{source}", receive, methods=["POST"]), Route("/livez", livez)],
        lifespan=lifespan,
    )
