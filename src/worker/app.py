"""Uvicorn factory target: worker.app:create_app."""

from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool

from common.errors import APIError, install_error_handlers
from common.models import (
    ChunkPutHeaders, ChunkReadHeaders, ChunkReceipt, MutationHeaders, WireModel, WorkerInventory,
)
from worker.config import WorkerConfig
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


def create_app(config: WorkerConfig | None = None) -> FastAPI:
    settings = config if config is not None else WorkerConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = ChunkStore(settings)
        app.state.store = store
        try:
            yield
        finally:
            store.close()

    app = FastAPI(title="Course object store worker", lifespan=lifespan)
    app.state.config = settings
    install_error_handlers(app)

    @app.get("/health/live")
    def live():
        return {"status": "live"}

    @app.get("/health/ready")
    def ready():
        raise APIError(503, "worker_not_ready", "Waiting for worker registration and reconciliation")

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

    @app.get("/internal/workers/{worker_id}/chunks", response_model=WorkerInventory)
    def inventory(worker_id: str, request: Request):
        store = request.app.state.store
        if worker_id != store.identity.worker_id:
            raise APIError(404, "worker_not_found", "This worker has a different ID")
        return store.inventory()

    return app
