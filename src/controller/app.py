"""Uvicorn factory target: controller.app:create_app."""

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request

from common.config import ControllerConfig
from common.errors import APIError, install_error_handlers
from common.models import (
    HeartbeatResponse, RegistrationResponse, WorkerHeartbeat, WorkerRegistration,
)
from controller.metadata import MetadataStore
from controller.registry import WorkerRegistry


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
                yield
        finally:
            store.close()

    app = FastAPI(title="Course object store controller", lifespan=lifespan)
    app.state.config = settings
    install_error_handlers(app)

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
    def register(identity: WorkerRegistration, request: Request):
        return request.app.state.registry.register(identity)

    @app.post("/internal/workers/heartbeat", response_model=HeartbeatResponse)
    def heartbeat(message: WorkerHeartbeat, request: Request):
        return request.app.state.registry.heartbeat(message)

    return app
