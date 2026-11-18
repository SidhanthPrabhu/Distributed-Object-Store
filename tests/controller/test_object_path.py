"""Controller-only contract doubles. These do not certify worker durability."""

import asyncio
import json
from contextlib import contextmanager
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from common.config import ControllerConfig, WORKER_IDS
from common.errors import APIError
from common.models import WorkerRegistration
from controller.app import create_app
from controller.metadata import MetadataStore


class WorkerDouble:
    def __init__(self, identities):
        self.identities = {i.worker_id: i.model_dump(mode="json") for i in identities}
        self.chunks = {w: {} for w in WORKER_IDS}
        self.puts, self.calls = [], []
        self.bad_inventory, self.bad_receipt = {}, None
        self.get_modes = {}
        self.offline = set()
        self.lost_put_replies = 0
        self.tombstones = {w: {} for w in WORKER_IDS}
        self.cleanup_bad = False
        self.observe_put = None
        self.observe_get = None
        self.inventory_reply = None

    def __call__(self, request):
        worker = request.url.host
        self.calls.append((worker, request.method, request.url.path))
        if worker in self.offline:
            raise httpx.ConnectError("injected offline", request=request)
        path = request.url.path
        if path.endswith("/chunks"):
            if self.inventory_reply:
                return self.inventory_reply
            identity = self.bad_inventory.get(worker, self.identities[worker])
            return httpx.Response(200, json={**identity, "chunks": [
                {"object_id": p["object_id"], "generation": p["generation"], "chunk_id": cid,
                 "length_bytes": p["length_bytes"], "expected_sha256": p["sha256"],
                 "state": "committed", "operation_sequence": p["operation_sequence"],
                 "last_verified_at": None}
                for cid, (p, _) in self.chunks[worker].items()]})
        if path.endswith("/tombstone"):
            oid = path.split("/")[-2]
            body = json.loads(request.content)
            self.tombstones[worker][oid] = body
            self.chunks[worker] = {cid: value for cid, value in self.chunks[worker].items()
                                   if value[0]["object_id"] != oid}
            receipt = {**body, "object_id": oid, **self.identities[worker], "cleanup_complete": True}
            if self.cleanup_bad:
                receipt["boot_id"] = str(uuid4())
            return httpx.Response(200, json=receipt)
        cid = path.split("/")[-1]
        if request.method == "DELETE":
            self.chunks[worker].pop(cid, None)
            return httpx.Response(204)
        if request.method == "PUT":
            fields = {"object_id": request.headers["X-Object-ID"],
                "generation": request.headers["X-Object-Generation"],
                "operation_id": request.headers["X-Operation-ID"],
                "operation_sequence": int(request.headers["X-Operation-Sequence"]),
                "controller_epoch": int(request.headers["X-Controller-Epoch"]),
                "sha256": request.headers["X-Chunk-SHA256"],
                "length_bytes": int(request.headers["X-Chunk-Length"]),
                "chunk_id": cid, **self.identities[worker], "durable": True}
            self.puts.append(fields.copy())
            if fields["object_id"] in self.tombstones[worker]:
                return httpx.Response(409)
            self.chunks[worker][cid] = (fields, request.content)
            if self.observe_put:
                self.observe_put(fields)
            if self.lost_put_replies:
                self.lost_put_replies -= 1
                raise httpx.ReadTimeout("reply lost after storage", request=request)
            if self.bad_receipt:
                fields.update(self.bad_receipt)
            return httpx.Response(200, json=fields)
        value = self.chunks[worker].get(cid)
        mode = self.get_modes.get((worker, cid))
        if mode == "missing" or value is None:
            return httpx.Response(404)
        fields, data = value
        if self.observe_get:
            self.observe_get(worker, cid)
        if mode == "corrupt":
            data = bytes([data[0] ^ 1]) + data[1:]
        elif mode == "truncated":
            data = data[:-1]
        elif mode == "oversized":
            data += b"x"
        headers = {"X-Chunk-SHA256": fields["sha256"], "X-Chunk-Length": str(fields["length_bytes"])}
        if mode == "bad_headers":
            headers["X-Chunk-SHA256"] = "0" * 64
        return httpx.Response(200, content=data, headers=headers)


@pytest.fixture
def workers(identities):
    return WorkerDouble(identities)


@contextmanager
def running(tmp_path, workers, **settings):
    config = ControllerConfig(database_path=tmp_path / "metadata.sqlite3",
        reconciliation_interval_seconds=60, **settings)
    app = create_app(config, http_transport=httpx.MockTransport(workers))
    with TestClient(app) as client:
        now = [100.0]
        app.state.registry.clock = lambda: now[0]
        yield client, app, now


def register_all(client, app, workers, *, more=False, free=100_000_000):
    for body in workers.identities.values():
        response = client.post("/internal/workers/register", json=body)
        assert response.status_code == 200
        assert response.json()["state"] == "RECONCILING"
        response = client.post("/internal/workers/heartbeat", json={**body,
            "free_bytes": free, "integrity_reports": [], "integrity_reports_more": more})
        assert response.status_code == 200
    client.portal.call(app.state.transfers.maintenance_once)


def put(client, data=b"hello", metadata=None):
    return client.post("/api/v1/objects", files={"file": ("same.bin", data, "application/x-test")},
                       data={"metadata": json.dumps(metadata or {})})


@pytest.mark.parametrize("size", [0, 1, 999_999, 1_000_000, 1_000_001, 2_000_000, 2_000_001])
def test_decimal_boundaries_binary_round_trip_and_distinct_placements(tmp_path, workers, size):
    data = (bytes(range(256)) * ((size + 255) // 256))[:size]
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        assert client.get("/health/ready").status_code == 200
        response = put(client, data, {"nested": {"value": [True, None, 3]}})
        assert response.status_code == 201, response.text
        manifest = response.json()
        assert manifest["size_bytes"] == size
        assert manifest["checksum"]["value"] == sha256(data).hexdigest()
        assert manifest["chunk_count"] == (size + 999_999) // 1_000_000
        assert [c["ordinal"] for c in manifest["chunks"]] == list(range(manifest["chunk_count"]))
        assert all(len(c["locations"]) == len(set(c["locations"])) == 2 for c in manifest["chunks"])
        assert [c["locations"] for c in manifest["chunks"]] == [
            ["worker1", "worker2"], ["worker2", "worker3"], ["worker3", "worker4"]
        ][:manifest["chunk_count"]]
        result = client.get(f"/api/v1/objects/{manifest['object_id']}")
        assert result.status_code == 200
        assert result.content == data
        assert result.headers["content-type"] == "application/x-test"
        assert int(result.headers["content-length"]) == size
        assert app.state.transfers.temp_bytes == 0
        other = put(client, data).json()
        assert other["object_id"] != manifest["object_id"]
        assert other["generation"] != manifest["generation"]
        assert not {c["chunk_id"] for c in other["chunks"]} & {c["chunk_id"] for c in manifest["chunks"]}


@pytest.mark.parametrize("mode", ["corrupt", "missing", "truncated", "oversized", "bad_headers"])
def test_checked_fallback_never_returns_corrupt_bytes(tmp_path, workers, mode):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        manifest = put(client).json()
        cid = manifest["chunks"][0]["chunk_id"]
        workers.get_modes["worker1", cid] = mode
        response = client.get(f"/api/v1/objects/{manifest['object_id']}")
        assert response.status_code == 200 and response.content == b"hello"
        assert (cid, "worker1") in app.state.store.faulty_placements()
        assert app.state.store.get_manifest(UUID(manifest["object_id"])).checksum.value == sha256(b"hello").hexdigest()
        workers.get_modes["worker2", cid] = "corrupt"
        failure = client.get(f"/api/v1/objects/{manifest['object_id']}")
        assert failure.status_code == 503
        assert failure.json()["error"]["code"] == "no_valid_copy"
        assert app.state.transfers.temp_bytes == 0


def test_later_chunk_failure_returns_json_before_success(tmp_path, workers):
    with running(tmp_path, workers, chunk_size_bytes=4) as (client, app, _):
        register_all(client, app, workers)
        manifest = put(client, b"abcdefgh").json()
        chunk = manifest["chunks"][-1]
        for worker in chunk["locations"]:
            workers.get_modes[worker, chunk["chunk_id"]] = "missing"
        response = client.get(f"/api/v1/objects/{manifest['object_id']}")
        assert response.status_code == 503 and response.headers["content-type"] == "application/json"
        assert app.state.transfers.temp_bytes == 0


@pytest.mark.parametrize("bad", [
    {"durable": False}, {"durable": 1}, {"boot_id": str(uuid4())}, {"storage_id": str(uuid4())},
    {"sha256": "0" * 64}, {"length_bytes": 4}, {"operation_id": str(uuid4())},
    {"operation_sequence": 900}, {"controller_epoch": 90}, {"worker_id": "worker4"},
])
def test_invalid_receipt_hides_upload_and_tracks_terminal_cleanup(tmp_path, workers, bad):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        workers.bad_receipt = bad
        observed = []
        def hidden(fields):
            oid = UUID(fields["object_id"])
            with pytest.raises(APIError) as exc:
                app.state.store.get_manifest(oid)
            assert exc.value.status_code == 404
            observed.append(oid)
        workers.observe_put = hidden
        response = put(client)
        assert response.status_code == 503, response.text
        assert app.state.store.object_state(observed[0]) in {"DELETING", "DELETED"}
        assert client.get(f"/api/v1/objects/{observed[0]}").status_code == 404
        client.portal.call(app.state.transfers.maintenance_once)
        assert app.state.store.pending_cleanup() == []
        assert app.state.store.object_state(observed[0]) == "DELETED"
        assert all(not chunks for chunks in workers.chunks.values())
        assert all(str(observed[0]) in t for t in workers.tombstones.values())


def test_ambiguous_put_reuses_durable_operation_and_sequence(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        workers.lost_put_replies = 1
        response = put(client)
        assert response.status_code == 201, response.text
        assert workers.puts[0] == workers.puts[1]
        assert len(app.state.store.pending_operations()) == 0


def test_capacity_skips_workers_and_reserves_space_between_chunks(tmp_path, workers):
    with running(tmp_path, workers, chunk_size_bytes=4) as (client, app, _):
        register_all(client, app, workers, free=4)
        response = put(client, b"abcdefgh")
        assert response.status_code == 201, response.text
        assert [c["locations"] for c in response.json()["chunks"]] == [
            ["worker1", "worker2"], ["worker3", "worker4"]]
        assert put(client).status_code == 503


def test_expiry_excludes_reads_and_placement_until_reconciled(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, now):
        register_all(client, app, workers)
        manifest = put(client).json()
        now[0] += 6
        assert app.state.registry.eligible_worker_ids() == []
        assert client.get(f"/api/v1/objects/{manifest['object_id']}").status_code == 503
        assert put(client).status_code == 503
        workers.bad_inventory = {w: {**i, "boot_id": str(uuid4())}
                                 for w, i in workers.identities.items()}
        for identity in workers.identities.values():
            response = client.post("/internal/workers/heartbeat", json={**identity,
                "free_bytes": 100, "integrity_reports": [], "integrity_reports_more": False})
            assert response.json()["state"] == "RECONCILING"
        client.portal.call(app.state.transfers.maintenance_once)
        assert app.state.registry.eligible_worker_ids() == []


@pytest.mark.parametrize("field,value", [("worker_id", "worker4"),
    ("storage_id", str(uuid4())), ("boot_id", str(uuid4()))])
def test_inventory_identity_conflicts_cannot_establish_health(tmp_path, workers, field, value):
    workers.bad_inventory["worker1"] = {**workers.identities["worker1"], field: value}
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        assert "worker1" not in app.state.registry.eligible_worker_ids()
        assert app.state.transfers.reconciliation_errors["worker1"] == "identity_conflict"


def test_inventory_hash_conflict_preserves_expected_hash_after_restart(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        manifest = put(client).json()
        epoch = app.state.store.epoch
    cid = manifest["chunks"][0]["chunk_id"]
    fields, data = workers.chunks["worker1"][cid]
    workers.chunks["worker1"][cid] = ({**fields, "sha256": "0" * 64}, data)
    for identity in workers.identities.values():
        identity["boot_id"] = str(uuid4())
    with running(tmp_path, workers) as (client, app, _):
        assert app.state.store.epoch == epoch + 1
        assert app.state.registry.eligible_worker_ids() == []
        register_all(client, app, workers)
        assert "worker1" not in app.state.registry.eligible_worker_ids()
        assert app.state.store.get_manifest(UUID(manifest["object_id"])).chunks[0].sha256 == sha256(b"hello").hexdigest()
        result = client.get(f"/api/v1/objects/{manifest['object_id']}")
        assert result.status_code == 200 and result.content == b"hello"
        uploaded = put(client).json()
        assert min(p["operation_sequence"] for p in workers.puts if p["object_id"] == uploaded["object_id"]) > max(
            p["operation_sequence"] for p in workers.puts if p["object_id"] == manifest["object_id"])


def test_report_backlog_blocks_health_until_drained(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers, more=True)
        assert app.state.registry.eligible_worker_ids() == []
        body = workers.identities["worker1"]
        client.post("/internal/workers/heartbeat", json={**body, "free_bytes": 100,
            "integrity_reports": [], "integrity_reports_more": False})
        client.portal.call(app.state.transfers.maintenance_once)
        assert app.state.registry.eligible_worker_ids() == ["worker1"]


def test_startup_aborts_interrupted_upload_and_replays_cleanup(tmp_path, workers, manifest_factory):
    path = tmp_path / "metadata.sqlite3"
    store = MetadataStore(path)
    for identity in workers.identities.values():
        store.register(WorkerRegistration(**identity))
    manifest = manifest_factory(data=b"abcd", chunk_size=4)
    store.create_upload(manifest)
    op = store.create_put_intent(manifest.object_id, manifest.chunks[0].chunk_id, "worker1")
    # The remote effect happened but its reply/receipt never reached SQLite.
    workers.chunks["worker1"][str(manifest.chunks[0].chunk_id)] = (op, b"abcd")
    old_epoch = store.epoch
    store.close()
    workers.offline.add("worker4")
    with running(tmp_path, workers) as (client, app, _):
        assert app.state.store.epoch == old_epoch + 1
        assert app.state.store.object_state(manifest.object_id) == "DELETING"
        register_all(client, app, workers)
        pending = app.state.store.pending_cleanup()
        assert pending == [{"object_id": str(manifest.object_id), "worker_id": "worker4", "acknowledged": 0}]
        assert "worker4" not in app.state.registry.eligible_worker_ids()
        assert client.get(f"/api/v1/objects/{manifest.object_id}").status_code == 404
        tombstone = app.state.store.cleanup_intent(manifest.object_id)
        assert tombstone["operation_sequence"] > op["operation_sequence"]
        assert tombstone["controller_epoch"] > op["controller_epoch"]
    workers.offline.clear()
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        assert app.state.store.pending_cleanup() == []
        assert app.state.store.pending_operations() == []
        assert app.state.store.object_state(manifest.object_id) == "DELETED"
        assert all(not chunks for chunks in workers.chunks.values())
        assert workers.tombstones["worker4"][str(manifest.object_id)]["controller_epoch"] == app.state.store.epoch


def test_cleanup_receipt_requires_current_boot_and_retries_keep_identity(tmp_path, workers, manifest_factory):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        manifest = manifest_factory()
        app.state.store.create_upload(manifest)
        op = app.state.store.begin_deletion(manifest.object_id, WORKER_IDS)
        workers.cleanup_bad = True
        client.portal.call(app.state.transfers.maintenance_once)
        assert len(app.state.store.pending_cleanup()) == 4
        assert app.state.registry.eligible_worker_ids() == []
        client.portal.call(app.state.transfers.maintenance_once)
        assert all(t[str(manifest.object_id)]["operation_id"] == op["operation_id"]
                   for t in workers.tombstones.values())
        workers.cleanup_bad = False
        client.portal.call(app.state.transfers.maintenance_once)
        assert app.state.store.pending_cleanup() == []


@pytest.mark.parametrize("metadata", ["[]", "null", "5", "broken", '{"x": NaN}', '{"x": 1e999}'])
def test_invalid_custom_metadata_is_400(tmp_path, workers, metadata):
    with running(tmp_path, workers) as (client, _, _):
        response = client.post("/api/v1/objects", files={"file": ("x", b"x")}, data={"metadata": metadata})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"


def test_unknown_invalid_id_and_missing_file_errors(tmp_path, workers):
    with running(tmp_path, workers) as (client, _, _):
        assert client.get(f"/api/v1/objects/{uuid4()}").status_code == 404
        assert client.get("/api/v1/objects/not-a-uuid").status_code == 400
        assert client.post("/api/v1/objects", data={"metadata": "{}"}).status_code == 400


def test_quotas_and_spool_cleanup_on_response_send_failure(tmp_path, workers):
    with running(tmp_path, workers, download_temp_quota_bytes=5, upload_max_bytes=10) as (client, app, _):
        register_all(client, app, workers)
        assert put(client, b"01234567890").status_code == 400
        assert put(client, b"x" * 70_000).status_code == 400
        manifest = put(client).json()
        response = client.portal.call(app.state.transfers.download, UUID(manifest["object_id"]))
        assert app.state.transfers.temp_bytes == 5
        blocked = client.get(f"/api/v1/objects/{manifest['object_id']}")
        assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "temporary_capacity"
        async def fail_send():
            async def receive():
                return {"type": "http.disconnect"}
            async def send(message):
                raise RuntimeError("injected send failure")
            with pytest.raises(RuntimeError, match="send failure"):
                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        client.portal.call(fail_send)
        assert response.file.closed and app.state.transfers.temp_bytes == 0
        assert client.get(f"/api/v1/objects/{manifest['object_id']}").status_code == 200


@pytest.mark.parametrize("change", ["boot", "expiry", "tombstone"])
def test_worker_change_or_terminal_fence_during_put_prevents_publication(tmp_path, workers, change):
    with running(tmp_path, workers) as (client, app, now):
        register_all(client, app, workers)
        seen = []
        def mutate(fields):
            if seen:
                return
            seen.append(UUID(fields["object_id"]))
            if change == "boot":
                body = {**workers.identities["worker1"], "boot_id": str(uuid4())}
                workers.identities["worker1"] = body
                app.state.registry.register(WorkerRegistration(**body))
            elif change == "expiry":
                now[0] += 6
            else:
                app.state.store.begin_deletion(seen[0], WORKER_IDS)
        workers.observe_put = mutate
        response = put(client)
        assert response.status_code == 503, response.text
        assert client.get(f"/api/v1/objects/{seen[0]}").status_code == 404
        assert app.state.store.object_state(seen[0]) != "ACTIVE"
        assert app.state.store.get_manifest(seen[0], include_hidden=True).checksum.value == sha256(b"hello").hexdigest()


def test_reconciliation_discards_checked_read_after_new_registration(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        manifest = put(client).json()
        changed = []
        def re_register(worker, cid):
            if worker == "worker1" and not changed:
                changed.append(cid)
                app.state.registry.register(WorkerRegistration(**workers.identities[worker]))
        workers.observe_get = re_register
        app.state.registry.invalidate("worker1")
        client.portal.call(app.state.transfers.maintenance_once)
        assert changed
        assert "worker1" not in app.state.registry.eligible_worker_ids()
        assert app.state.transfers.reconciliation_errors["worker1"] == "stale_operation"
        workers.observe_get = None
        client.portal.call(app.state.transfers.maintenance_once)
        assert "worker1" in app.state.registry.eligible_worker_ids()
        assert client.get(f"/api/v1/objects/{manifest['object_id']}").status_code == 200


def test_reconciliation_tracks_missing_copy_and_rechecks_returning_copy(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        manifest = put(client).json()
        cid = manifest["chunks"][0]["chunk_id"]
        saved = workers.chunks["worker1"].pop(cid)
        app.state.registry.invalidate("worker1")
        client.portal.call(app.state.transfers.maintenance_once)
        assert (cid, "worker1") in app.state.store.faulty_placements()
        assert "worker1" in app.state.registry.eligible_worker_ids()
        assert client.get(f"/api/v1/objects/{manifest['object_id']}").content == b"hello"
        workers.chunks["worker1"][cid] = saved
        app.state.registry.register(WorkerRegistration(**workers.identities["worker1"]))
        client.portal.call(app.state.transfers.maintenance_once)
        assert (cid, "worker1") not in app.state.store.faulty_placements()


@pytest.mark.parametrize("kind", ["malformed", "oversized", "redirect", "unknown_chunk"])
def test_inventory_failure_does_not_make_worker_healthy(tmp_path, workers, kind):
    if kind == "malformed":
        workers.inventory_reply = httpx.Response(200, content=b"broken-json")
    elif kind == "oversized":
        workers.inventory_reply = httpx.Response(200, content=b" " * 1025)
    elif kind == "redirect":
        workers.inventory_reply = httpx.Response(302, headers={"Location": "http://untrusted/inventory"})
    else:
        workers.chunks["worker1"][str(uuid4())] = ({
            "object_id": str(uuid4()), "generation": str(uuid4()), "length_bytes": 1,
            "sha256": sha256(b"x").hexdigest(), "operation_sequence": 1}, b"x")
    with running(tmp_path, workers, inventory_max_bytes=1024) as (client, app, _):
        register_all(client, app, workers)
        assert "worker1" not in app.state.registry.eligible_worker_ids()
        assert all(call[0] in WORKER_IDS for call in workers.calls)


def test_download_tempfile_disk_failure_releases_quota(tmp_path, workers, monkeypatch):
    from controller import transfers
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        manifest = put(client).json()
        def fail(*args, **kwargs):
            raise OSError("injected disk failure")
        monkeypatch.setattr(transfers.tempfile, "TemporaryFile", fail)
        response = client.get(f"/api/v1/objects/{manifest['object_id']}")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "temporary_storage_unavailable"
        assert app.state.transfers.temp_bytes == 0


def test_complete_digest_is_checked_even_when_chunk_digests_match(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        manifest = put(client).json()
        # Fault injection into persisted controller state, never an inventory update.
        with app.state.store.transaction() as db:
            db.execute("UPDATE objects SET sha256=? WHERE object_id=?", ("0" * 64, manifest["object_id"]))
        response = client.get(f"/api/v1/objects/{manifest['object_id']}")
        assert response.status_code == 503 and response.json()["error"]["code"] == "object_corrupt"
        assert app.state.transfers.temp_bytes == 0


def test_overall_http_deadline_retries_are_bounded_and_keep_put_identity(tmp_path, workers):
    async def check():
        from controller.transfers import Transfers
        from controller.registry import WorkerRegistry
        seen = []
        async def slow(request):
            seen.append(request.headers["X-Operation-ID"])
            await asyncio.sleep(10)
        config = ControllerConfig(database_path=tmp_path / "deadline.sqlite3", http_timeout_seconds=0.01)
        store = MetadataStore(config.database_path)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
                coordinator = Transfers(store, WorkerRegistry(store, config), client, config)
                operation_id = str(uuid4())
                with pytest.raises(APIError) as exc:
                    await asyncio.wait_for(coordinator.request("PUT", "worker1", "/internal/chunks/x",
                        limit=100, headers={"X-Operation-ID": operation_id}, content=b"x"), timeout=1)
                assert exc.value.status_code == 503
                assert seen == [operation_id, operation_id]
        finally:
            store.close()
    asyncio.run(check())


def test_schema_one_upgrade_preserves_manifest_and_intents(tmp_path, identities, manifest_factory):
    path = tmp_path / "upgrade.sqlite3"
    store = MetadataStore(path)
    manifest = manifest_factory()
    for identity in identities:
        store.register(identity)
    store.create_upload(manifest)
    op = store.create_put_intent(manifest.object_id, manifest.chunks[0].chunk_id, "worker1")
    with store.transaction() as db:
        db.execute("DROP TABLE placement_faults")
        db.execute("DROP TABLE placement_rotation")
        db.execute("PRAGMA user_version=1")
    store.close()
    store = MetadataStore(path)
    try:
        assert store.get_manifest(manifest.object_id, include_hidden=True) == manifest
        assert store.pending_operations() == [op]
        assert store.faulty_placements() == set()
        assert store.next_placement_cursor() == 0
    finally:
        store.close()


def test_registration_drives_automatic_reconciliation_without_manual_pass(tmp_path, workers):
    import time
    with running(tmp_path, workers) as (client, app, _):
        for identity in workers.identities.values():
            assert client.post("/internal/workers/register", json=identity).status_code == 200
            assert client.post("/internal/workers/heartbeat", json={**identity, "free_bytes": 100,
                "integrity_reports": [], "integrity_reports_more": False}).status_code == 200
        deadline = time.monotonic() + 2
        while len(app.state.registry.eligible_worker_ids()) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(app.state.registry.eligible_worker_ids()) == 4
        assert put(client).status_code == 201


def test_repeated_lost_put_replies_leave_hidden_terminal_cleanup(tmp_path, workers):
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        workers.lost_put_replies = 2
        response = put(client)
        assert response.status_code == 503
        assert workers.puts[0] == workers.puts[1]
        oid = UUID(workers.puts[0]["object_id"])
        assert app.state.store.object_state(oid) in {"DELETING", "DELETED"}
        assert client.get(f"/api/v1/objects/{oid}").status_code == 404
        client.portal.call(app.state.transfers.maintenance_once)
        assert app.state.store.object_state(oid) == "DELETED"
        assert app.state.store.pending_operations() == []


def test_cancelled_upload_keeps_intent_and_fenced_cleanup(tmp_path, workers):
    from starlette.datastructures import UploadFile
    import io
    with running(tmp_path, workers) as (client, app, _):
        register_all(client, app, workers)
        effects = []
        async def cancel_after_effect(request):
            if request.method == "PUT":
                result = workers(request)  # Record the effect, then lose execution.
                assert result.status_code == 200
                op_id = request.headers["X-Operation-ID"]
                pending = app.state.store.pending_operations()
                assert any(op["operation_id"] == op_id for op in pending)
                effects.append(UUID(request.headers["X-Object-ID"]))
                raise asyncio.CancelledError()
            return workers(request)
        async def cancelled():
            async with httpx.AsyncClient(transport=httpx.MockTransport(cancel_after_effect)) as other:
                original_client = app.state.transfers.client
                app.state.transfers.client = other
                try:
                    with pytest.raises(asyncio.CancelledError):
                        await app.state.transfers.upload(UploadFile(io.BytesIO(b"hello"), filename="x"), {})
                finally:
                    app.state.transfers.client = original_client
        client.portal.call(cancelled)
        assert effects
        assert client.get(f"/api/v1/objects/{effects[0]}").status_code == 404
        client.portal.call(app.state.transfers.maintenance_once)
        assert app.state.store.object_state(effects[0]) == "DELETED"
        assert app.state.store.pending_cleanup() == []
