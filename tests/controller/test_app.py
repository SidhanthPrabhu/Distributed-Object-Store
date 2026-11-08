from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from common.config import ControllerConfig
from common.errors import APIError
from controller.app import create_app


def test_lifecycle_closes_resources_and_epoch_survives_restart(tmp_path):
    config = ControllerConfig(database_path=tmp_path / "metadata.sqlite3")
    app = create_app(config)
    assert not config.database_path.exists()  # Factory/import has no persistence side effects.
    with TestClient(app) as client:
        store, http_client = app.state.store, app.state.http_client
        epoch = store.epoch
        assert client.get("/health/live").json() == {"status": "live"}
        assert not http_client.is_closed
        assert http_client.timeout.read == 5.0
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "storage_unavailable"
    assert store.closed and http_client.is_closed
    with TestClient(create_app(config)) as client:
        assert client.app.state.store.epoch == epoch + 1


def test_shared_http_client_uses_injected_transport_and_does_not_follow_redirects(tmp_path):
    requested = []
    def transport(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://untrusted/"})
    app = create_app(ControllerConfig(database_path=tmp_path / "metadata.sqlite3"),
                     http_transport=httpx.MockTransport(transport))
    with TestClient(app) as client:
        response = client.portal.call(app.state.http_client.get, "http://worker1:8000/chunks")
        assert response.status_code == 302
    assert requested == ["http://worker1:8000/chunks"]


def test_registry_http_contract_and_no_premature_readiness(tmp_path, identities):
    app = create_app(ControllerConfig(database_path=tmp_path / "metadata.sqlite3"))
    with TestClient(app) as client:
        for identity in identities:
            body = identity.model_dump(mode="json")
            heartbeat = {**body, "free_bytes": 100, "integrity_reports": [], "integrity_reports_more": False}
            assert client.post("/internal/workers/heartbeat", json=heartbeat).status_code == 409
            registration = client.post("/internal/workers/register", json=body)
            assert registration.status_code == 200
            assert registration.json() == {
                "controller_epoch": app.state.store.epoch, "heartbeat_interval_seconds": 1.0,
                "state": "RECONCILING",
            }
            response = client.post("/internal/workers/heartbeat", json=heartbeat)
            assert response.status_code == 200
            assert response.json()["acknowledged_report_ids"] == []
        assert client.get("/health/ready").status_code == 503


@pytest.mark.parametrize("body", [
    {}, {"worker_id": "worker5", "storage_id": str(uuid4()), "boot_id": str(uuid4())},
    {"worker_id": "worker1", "storage_id": "../file", "boot_id": str(uuid4())},
    {"worker_id": "worker1", "storage_id": str(uuid4()), "boot_id": str(uuid4()), "url": "http://arbitrary"},
])
def test_invalid_registration_is_400_with_envelope(tmp_path, body):
    app = create_app(ControllerConfig(database_path=tmp_path / "metadata.sqlite3"))
    with TestClient(app) as client:
        response = client.post("/internal/workers/register", json=body)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
        assert str(UUID(response.json()["error"]["request_id"])) == response.headers["X-Request-ID"]


def test_errors_request_ids_and_validation_do_not_expose_inputs(tmp_path):
    app = create_app(ControllerConfig(database_path=tmp_path / "metadata.sqlite3"))

    @app.get("/probe/{object_id}")
    def probe(object_id: UUID):
        raise APIError(409, "example_conflict", "An explicit conflict")

    @app.get("/unexpected")
    def unexpected():
        raise RuntimeError("private database path /secret/location")

    with TestClient(app) as client:
        request_id = str(uuid4())
        response = client.get(f"/probe/{uuid4()}", headers={"X-Request-ID": request_id})
        assert response.status_code == 409
        assert response.json()["error"] == {
            "code": "example_conflict", "message": "An explicit conflict", "request_id": request_id,
        }
        response = client.get("/probe/private-input", headers={"X-Request-ID": "bad"})
        assert response.status_code == 400
        assert "private-input" not in response.text
        UUID(response.headers["X-Request-ID"])
        malformed = client.post("/internal/workers/register", content="{broken", headers={"Content-Type": "application/json"})
        assert malformed.status_code == 400
        unknown = client.get("/missing")
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "not_found"
        unexpected_response = client.get("/unexpected")
        assert unexpected_response.status_code == 500
        assert "private" not in unexpected_response.text
        assert unexpected_response.json()["error"]["code"] == "internal_error"


def test_failed_client_startup_closes_database(tmp_path, monkeypatch):
    stores = []
    from controller import app as app_module
    original = app_module.MetadataStore

    def tracked_store(path):
        store = original(path)
        stores.append(store)
        return store

    def fail_client(**kwargs):
        raise RuntimeError("client initialization failure")

    monkeypatch.setattr(app_module, "MetadataStore", tracked_store)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", fail_client)
    app = create_app(ControllerConfig(database_path=tmp_path / "metadata.sqlite3"))
    with pytest.raises(RuntimeError, match="initialization failure"):
        with TestClient(app):
            pass
    assert len(stores) == 1 and stores[0].closed
