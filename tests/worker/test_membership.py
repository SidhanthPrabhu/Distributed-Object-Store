import time

import httpx
from fastapi.testclient import TestClient

from common.models import WorkerHeartbeat, WorkerRegistration
from worker.app import create_app
from worker.config import WorkerConfig


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Worker membership did not reach the expected state")


def test_membership_retries_and_reconciles_again_after_controller_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("worker.membership.INITIAL_RETRY_DELAY", 0.01)
    config = WorkerConfig(worker_id="worker2", data_dir=tmp_path, controller_url="http://controller:8000")
    controller = {"reachable": False, "epoch": 1, "state": "RECONCILING"}
    registrations = []
    heartbeats = []
    attempts = []

    def exchange(request):
        attempts.append(request.url.path)
        if not controller["reachable"]:
            raise httpx.ConnectError("Controller offline", request=request)
        if request.url.path == "/internal/workers/register":
            registrations.append((controller["epoch"], WorkerRegistration.model_validate_json(request.content)))
            return httpx.Response(200, json={
                "controller_epoch": controller["epoch"], "heartbeat_interval_seconds": 0.02,
                "state": "RECONCILING",
            })
        assert request.url.path == "/internal/workers/heartbeat"
        heartbeat = WorkerHeartbeat.model_validate_json(request.content)
        if registrations[-1][0] != controller["epoch"]:
            return httpx.Response(409, json={"error": {
                "code": "registration_required", "message": "Register this worker boot first",
                "request_id": str(heartbeat.boot_id),
            }})
        assert heartbeat.model_dump(exclude={"free_bytes", "integrity_reports", "integrity_reports_more"}) == (
            registrations[-1][1].model_dump()
        )
        assert heartbeat.free_bytes >= 0
        assert not heartbeat.integrity_reports and not heartbeat.integrity_reports_more
        heartbeats.append(controller["epoch"])
        return httpx.Response(200, json={
            "controller_epoch": controller["epoch"], "state": controller["state"],
            "acknowledged_report_ids": [],
        })

    app = create_app(config, http_transport=httpx.MockTransport(exchange))
    with TestClient(app) as client:
        wait_until(lambda: len(attempts) >= 2)
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        controller["reachable"] = True
        wait_until(lambda: 1 in heartbeats)
        assert client.get("/health/ready").status_code == 503
        controller["state"] = "HEALTHY"
        wait_until(lambda: client.get("/health/ready").status_code == 200)

        controller["reachable"] = False
        wait_until(lambda: client.get("/health/ready").status_code == 503)
        controller.update(epoch=2, state="RECONCILING", reachable=True)
        wait_until(lambda: 2 in heartbeats)
        assert client.get("/health/ready").status_code == 503
        assert [epoch for epoch, _ in registrations] == [1, 2]
        assert registrations[0][1] == registrations[1][1]
        controller["state"] = "HEALTHY"
        wait_until(lambda: client.get("/health/ready").status_code == 200)
        membership = app.state.membership

    assert membership.client.is_closed
