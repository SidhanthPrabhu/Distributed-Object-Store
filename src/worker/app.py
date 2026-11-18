"""Uvicorn factory target: worker.app:create_app."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from uuid import UUID

import httpx
from fastapi import FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool

from common.errors import APIError, install_error_handlers
from common.models import (
    ChunkPutHeaders, ChunkReadHeaders, ChunkReceipt, MutationHeaders, TombstoneReceipt,
    TombstoneRequest, WireModel, WorkerInventory,
)
from worker.config import WorkerConfig
from worker.membership import Membership
from worker.store import ChunkStore

MUTATION_HEADERS = {
    "object_id": "X-Object-ID",
    "generation": "X-Object-Generation",
    "operation_id": "X-Operation-ID",
    "controller_epoch": "X-Controller-Epoch",
    "operation_sequence": "X-Operation-Sequence",
}
READ_HEADERS = {
    "generation": "X-Object-Generation",
    "sha256": "X-Chunk-SHA256",
    "length_bytes": "X-Chunk-Length",
}
INTEGER_FIELDS = {"controller_epoch", "operation_sequence", "length_bytes"}
BINARY = "application/octet-stream"
logger = logging.getLogger(__name__)


async def scan_placements(store: ChunkStore, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await run_in_threadpool(store.scan_once)
        except APIError:
            if not store.scan_degraded:
                logger.warning("Worker scan could not persist progress or a fault")
            store.scan_degraded = True
        else:
            store.scan_degraded = False


def parse_headers[Model: WireModel](
    request: Request, model: type[Model], names: dict[str, str],
) -> Model:
    values = {}
    try:
        for field, name in names.items():
            matches = request.headers.getlist(name)
            if len(matches) != 1:
                raise ValueError("Expected exactly one header")
            value = matches[0]
            if field in INTEGER_FIELDS:
                if len(value) > 19 or not value.isascii() or not value.isdecimal():
                    raise ValueError("Expected a decimal integer")
                values[field] = int(value)
            else:
                values[field] = value
        return model.model_validate(values)
    except ValueError as exc:
        raise APIError(400, "invalid_request", "Invalid or missing chunk headers") from exc


def create_app(config: WorkerConfig | None = None, *,
               http_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = config if config is not None else WorkerConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = ChunkStore(settings)
        app.state.store = store
        app.state.membership = None
        try:
            if settings.controller_url is None:
                yield
                return
            async with httpx.AsyncClient(
                base_url=str(settings.controller_url), trust_env=False, transport=http_transport,
            ) as client:
                membership = Membership(store, client)
                app.state.membership = membership
                task = asyncio.create_task(membership.run())
                scan = asyncio.create_task(scan_placements(store, settings.scan_interval_seconds))
                try:
                    yield
                finally:
                    scan.cancel()
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    with suppress(asyncio.CancelledError):
                        await scan
                    app.state.membership = None
        finally:
            store.close()

    app = FastAPI(title="Course object store worker", lifespan=lifespan)
    app.state.config = settings
    install_error_handlers(app)

    @app.get("/health/live")
    def live():
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready(request: Request):
        membership = getattr(request.app.state, "membership", None)
        if membership is None or not membership.ready:
            raise APIError(503, "worker_not_ready", "Waiting for worker registration and reconciliation")
        return {"status": "ready"}

    @app.put("/internal/chunks/{chunk_id}", response_model=ChunkReceipt)
    async def put(chunk_id: UUID, request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != BINARY:
            raise APIError(400, "invalid_request", "Chunk PUT requires application/octet-stream")
        headers = parse_headers(request, ChunkPutHeaders, MUTATION_HEADERS | READ_HEADERS)
        if headers.length_bytes > settings.chunk_size_bytes:
            raise APIError(400, "invalid_chunk_length", "Declared length exceeds the chunk limit")
        body = bytearray()
        async for part in request.stream():
            if len(body) + len(part) > headers.length_bytes:
                raise APIError(400, "invalid_chunk_length", "Chunk body exceeds its declared length")
            body.extend(part)
        return await run_in_threadpool(request.app.state.store.put, chunk_id, headers, body)

    @app.get("/internal/chunks/{chunk_id}", response_class=Response)
    def get(chunk_id: UUID, request: Request):
        headers = parse_headers(request, ChunkReadHeaders, READ_HEADERS)
        data = request.app.state.store.get(chunk_id, headers)
        return Response(data, media_type=BINARY, headers=headers.to_headers())

    @app.delete("/internal/chunks/{chunk_id}", status_code=204, response_class=Response)
    def delete(chunk_id: UUID, request: Request):
        headers = parse_headers(request, MutationHeaders, MUTATION_HEADERS)
        request.app.state.store.delete(chunk_id, headers)
        return Response(status_code=204)

    @app.post("/internal/objects/{object_id}/tombstone", response_model=TombstoneReceipt)
    def tombstone(object_id: UUID, message: TombstoneRequest, request: Request):
        return request.app.state.store.tombstone(object_id, message)

    @app.get("/internal/workers/{worker_id}/chunks", response_model=WorkerInventory)
    def inventory(worker_id: str, request: Request):
        store = request.app.state.store
        if worker_id != store.identity.worker_id:
            raise APIError(404, "worker_not_found", "This worker has a different ID")
        return store.inventory()

    return app
