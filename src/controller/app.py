"""Uvicorn factory target: controller.app:create_app."""

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from uuid import UUID

import httpx
from fastapi import FastAPI, Request, Response
from pydantic import JsonValue, TypeAdapter, ValidationError
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException

from common.config import ControllerConfig
from common.errors import APIError, install_error_handlers
from common.models import (
    HeartbeatResponse, ObjectList, ObjectMetadata, RegistrationResponse, WorkerHeartbeat, WorkerRegistration,
)
from controller.metadata import MetadataStore
from controller.registry import WorkerRegistry
from controller.transfers import Transfers


_CUSTOM_METADATA = TypeAdapter(dict[str, JsonValue])


def custom_metadata(raw) -> dict:
    """Both upload and PATCH accept a direct JSON map, including nested values."""
    try:
        value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        # JSON exponents can overflow without invoking parse_constant.
        json.dumps(value, allow_nan=False)
        return _CUSTOM_METADATA.validate_python(value)
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise APIError(400, "invalid_request", "Metadata must be a JSON object") from None


def create_app(config: ControllerConfig | None = None, *,
               http_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = config if config is not None else ControllerConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = MetadataStore(settings.database_path)
        app.state.store = store
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(settings.http_timeout_seconds),
                limits=httpx.Limits(max_connections=settings.http_max_connections,
                                    max_keepalive_connections=settings.http_max_connections),
                follow_redirects=False, trust_env=False, transport=http_transport,
            ) as client:
                app.state.http_client = client
                app.state.registry = WorkerRegistry(store, settings)
                transfers = Transfers(store, app.state.registry, client, settings)
                app.state.transfers = transfers
                store.abort_interrupted_uploads(tuple(settings.worker_addresses))
                maintenance = asyncio.create_task(transfers.run())
                try:
                    yield
                finally:
                    maintenance.cancel()
                    with suppress(asyncio.CancelledError):
                        await maintenance
        finally:
            store.close()

    app = FastAPI(title="Course object store controller", lifespan=lifespan)
    app.state.config = settings
    install_error_handlers(app)

    @app.exception_handler(MultipartParseError)
    async def malformed_multipart(request: Request, exc: MultipartParseError):
        from common.errors import error_response
        return error_response(request, 400, "invalid_request", "Malformed multipart request")

    @app.get("/health/live")
    def live():
        return {"status": "live"}

    @app.get("/health/ready")
    def ready(request: Request):
        store = getattr(request.app.state, "store", None)
        client = getattr(request.app.state, "http_client", None)
        if store is None or store.closed or client is None or client.is_closed:
            raise APIError(503, "controller_unavailable", "Controller resources are not ready")
        store.ping()
        if len(request.app.state.registry.eligible_worker_ids()) < settings.replication_factor:
            raise APIError(503, "storage_unavailable", "Waiting for reconciled storage workers")
        return {"status": "ready", "controller_epoch": store.epoch}

    @app.post("/internal/workers/register", response_model=RegistrationResponse)
    async def register(identity: WorkerRegistration, request: Request):
        result = request.app.state.registry.register(identity)
        request.app.state.transfers.wake.set()
        return result

    @app.post("/internal/workers/heartbeat", response_model=HeartbeatResponse)
    async def heartbeat(message: WorkerHeartbeat, request: Request):
        result = request.app.state.registry.heartbeat(message)
        request.app.state.transfers.wake.set()
        return result

    @app.post("/api/v1/objects", status_code=201, response_model=ObjectMetadata)
    async def upload(request: Request):
        # Bound the multipart request before parsing/spooling. Starlette closes
        # partial spool files on MultiPartException, including this body limit.
        consumed = 0

        async def limited_receive():
            nonlocal consumed
            message = await request.receive()
            consumed += len(message.get("body", b""))
            if consumed > settings.upload_max_bytes + 64 * 1024:
                raise MultiPartException("Upload exceeds configured request limit")
            return message

        bounded = Request(request.scope, limited_receive)
        async with bounded.form(max_files=1, max_fields=1, max_part_size=64 * 1024) as form:
            if (set(form) - {"file", "metadata"} or len(form.getlist("file")) != 1
                    or len(form.getlist("metadata")) > 1
                    or not isinstance(form.get("file"), UploadFile)):
                raise APIError(400, "invalid_request", "Expected one multipart file and optional metadata")
            file = form["file"]
            if any(c in (file.content_type or "") for c in "\r\n"):
                raise APIError(400, "invalid_request", "Invalid file content type")
            metadata = custom_metadata(form.get("metadata", "{}"))
            return await request.app.state.transfers.upload(file, metadata)

    @app.get("/api/v1/objects", response_model=ObjectList)
    def list_objects(request: Request):
        return ObjectList(objects=request.app.state.store.list_objects())

    @app.get("/api/v1/admin/status")
    async def storage_status(request: Request):
        return request.app.state.transfers.repairs.status()

    @app.get("/api/v1/objects/{object_id}/metadata", response_model=ObjectMetadata)
    def metadata(object_id: UUID, request: Request):
        return request.app.state.store.get_manifest(object_id)

    @app.patch("/api/v1/objects/{object_id}/metadata", response_model=ObjectMetadata)
    async def patch_metadata(object_id: UUID, request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise APIError(400, "invalid_request", "Expected application/json metadata")
        body = bytearray()
        async for part in request.stream():
            if len(body) + len(part) > 64 * 1024:
                raise APIError(400, "invalid_request", "Metadata request exceeds 64 KiB")
            body.extend(part)
        return request.app.state.store.patch_metadata(object_id, custom_metadata(body))

    @app.delete("/api/v1/objects/{object_id}", status_code=204)
    async def delete(object_id: UUID, request: Request):
        await request.app.state.transfers.delete(object_id)
        return Response(status_code=204)

    @app.get("/api/v1/objects/{object_id}")
    async def download(object_id: UUID, request: Request):
        return await request.app.state.transfers.download(object_id)

    return app
