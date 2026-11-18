"""Deterministic checks for controller repair and retirement decisions."""

from uuid import UUID, uuid4

from common.config import WORKER_IDS
from test_object_path import put, register_all, running, workers  # noqa: F401


def beat(client, identity, reports=None):
    response = client.post("/internal/workers/heartbeat", json={
        **identity, "free_bytes": 100_000_000,
        "integrity_reports": reports or [], "integrity_reports_more": False,
    })
    assert response.status_code == 200
    return response.json()


def lose_worker2(client, app, now, workers):
    workers.offline.add("worker2")
    now[0] += 6
    for worker, identity in workers.identities.items():
        if worker != "worker2":
            beat(client, identity)
    client.portal.call(app.state.transfers.maintenance_once)


def run_repair(client, app):
    async def run():
        async with app.state.transfers.lock:
            await app.state.transfers.repairs.run_once()
    client.portal.call(run)


def test_worker_outage_repairs_then_rejoin_retires_extra(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, now):
        register_all(client, app, workers)
        obj = put(client, b"course").json()
        path = f"/api/v1/objects/{obj['object_id']}"
        cid = obj["chunks"][0]["chunk_id"]
        lose_worker2(client, app, now, workers)
        status = client.get("/api/v1/admin/status").json()
        assert status["replication_deficits"] == [{"chunk_id": cid, "available_copy_count": 1}]
        assert client.get(path).content == b"course"
        run_repair(client, app)
        assert workers.chunks["worker3"][cid][1] == b"course"
        status = client.get("/api/v1/admin/status").json()
        assert status["replication_deficits"] == []
        assert client.get(path + "/metadata").json()["chunks"][0]["locations"] == [
            "worker1", "worker2", "worker3"]

        workers.offline.clear()
        workers.identities["worker2"]["boot_id"] = str(uuid4())
        assert client.post("/internal/workers/register", json=workers.identities["worker2"]).status_code == 200
        beat(client, workers.identities["worker2"])
        client.portal.call(app.state.transfers.maintenance_once)
        run_repair(client, app)
        assert client.get("/api/v1/admin/status").json()["repairs"] == []
        remaining = client.get(path + "/metadata").json()["chunks"][0]["locations"]
        assert len(remaining) == 2 and "worker3" in remaining
        assert {w for w in WORKER_IDS if cid in workers.chunks[w]} == set(remaining)


def test_no_valid_copy_never_writes_a_repair(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        obj = put(client).json()
        cid = obj["chunks"][0]["chunk_id"]
        workers.get_modes["worker1", cid] = "corrupt"
        workers.get_modes["worker2", cid] = "missing"
        response = client.get(f"/api/v1/objects/{obj['object_id']}")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "no_valid_copy"
        run_repair(client, app)
        status = client.get("/api/v1/admin/status").json()
        assert status["replication_deficits"] == [{"chunk_id": cid, "available_copy_count": 0}]
        assert status["repairs"][0]["error_reason"] == "no_valid_copy"
        assert len(workers.puts) == 2


def test_lost_repair_reply_is_reauthorized_after_controller_restart(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, now):
        register_all(client, app, workers)
        obj = put(client).json()
        oid, cid = obj["object_id"], obj["chunks"][0]["chunk_id"]
        lose_worker2(client, app, now, workers)
        workers.lost_put_replies = app.state.config.http_attempts
        run_repair(client, app)
        before = workers.chunks["worker3"][cid][0]
        assert app.state.store.get_manifest(UUID(oid)).chunks[0].locations == ["worker1", "worker2"]
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        after = workers.chunks["worker3"][cid][0]
        assert after["controller_epoch"] > before["controller_epoch"]
        assert after["operation_sequence"] > before["operation_sequence"]
        assert "worker3" in app.state.store.get_manifest(UUID(oid)).chunks[0].locations
        assert client.get(f"/api/v1/objects/{oid}").content == b"hello"


def test_deletion_while_repair_is_in_flight_prevents_publication(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, now):
        register_all(client, app, workers)
        obj = put(client).json()
        oid, cid = obj["object_id"], obj["chunks"][0]["chunk_id"]
        lose_worker2(client, app, now, workers)
        workers.observe_put = lambda _: app.state.store.begin_deletion(UUID(oid), WORKER_IDS, public=True)
        run_repair(client, app)
        assert "worker3" not in app.state.store.get_manifest(UUID(oid), include_hidden=True).chunks[0].locations
        assert client.get(f"/api/v1/objects/{oid}").status_code == 404
        assert client.delete(f"/api/v1/objects/{oid}").status_code == 503
        workers.offline.clear()
        beat(client, workers.identities["worker2"])
        client.portal.call(app.state.transfers.maintenance_once)
        assert app.state.store.object_state(UUID(oid)) == "DELETED"
        assert all(cid not in placements for placements in workers.chunks.values())
