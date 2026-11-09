from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from common.errors import APIError
from common.models import (
    ChunkPutHeaders, ChunkReadHeaders, ChunkReceipt, MutationHeaders,
    TombstoneReceipt, TombstoneRequest, WorkerInventory,
)
from worker.app import create_app
from worker.config import WorkerConfig
from worker.store import ChunkStore

DATA = bytes(range(256)) + b"\x00\xff"


def put_headers(data=DATA):
    return ChunkPutHeaders(
        object_id=uuid4(), generation=uuid4(), operation_id=uuid4(),
        controller_epoch=1, operation_sequence=1, sha256=sha256(data).hexdigest(),
        length_bytes=len(data),
    )


def read_headers(put):
    return ChunkReadHeaders(generation=put.generation, sha256=put.sha256, length_bytes=put.length_bytes)


def next_mutation(put, sequence):
    return MutationHeaders(
        object_id=put.object_id, generation=put.generation, operation_id=uuid4(),
        controller_epoch=put.controller_epoch, operation_sequence=sequence,
    )


def later_put(put, sequence):
    return put.model_copy(update={"operation_id": uuid4(), "operation_sequence": sequence})


def tombstone_request(put, sequence):
    return TombstoneRequest(
        generation=put.generation, operation_id=uuid4(),
        controller_epoch=put.controller_epoch, operation_sequence=sequence,
    )


@pytest.fixture
def config(tmp_path):
    return WorkerConfig(worker_id="worker2", data_dir=tmp_path / "worker2")


@pytest.fixture
def store(config):
    storage = ChunkStore(config)
    try:
        yield storage
    finally:
        storage.close()


def test_wire_round_trip_and_restart(config):
    chunk_id, put = uuid4(), put_headers()
    path = f"/internal/chunks/{chunk_id}"
    binary = {**put.to_headers(), "Content-Type": "application/octet-stream"}
    expected = read_headers(put)
    with TestClient(create_app(config)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        response = client.put(path, content=DATA, headers=binary)
        assert response.status_code == 200
        first = ChunkReceipt.model_validate(response.json())
        assert first.model_dump(include=set(ChunkPutHeaders.model_fields)) == put.model_dump()
        assert first.chunk_id == chunk_id and first.worker_id == "worker2" and first.durable
        assert client.put(path, content=DATA, headers=binary).json() == response.json()

    with TestClient(create_app(config)) as client:
        response = client.get(path, headers=expected.to_headers())
        assert response.status_code == 200 and response.content == DATA
        assert response.headers["Content-Type"] == "application/octet-stream"
        assert response.headers["X-Chunk-SHA256"] == put.sha256
        assert response.headers["X-Chunk-Length"] == str(len(DATA))
        assert client.get("/internal/workers/worker1/chunks").status_code == 404
        inventory = WorkerInventory.model_validate(
            client.get("/internal/workers/worker2/chunks").json(),
        )
        assert inventory.storage_id == first.storage_id and inventory.boot_id != first.boot_id
        assert len(inventory.chunks) == 1
        placement = inventory.chunks[0]
        assert placement.chunk_id == chunk_id and placement.state == "valid"
        assert placement.expected_sha256 == put.sha256 and placement.last_verified_at is not None

        deletion = next_mutation(put, 2).to_headers()
        for _ in range(2):
            response = client.delete(path, headers=deletion)
            assert response.status_code == 204 and response.content == b""
        assert client.get(path, headers=expected.to_headers()).json()["error"]["code"] == "chunk_missing"
        assert client.get("/internal/workers/worker2/chunks").json()["chunks"] == []


def test_invalid_writes_leave_no_placement(config):
    chunk_id, put = uuid4(), put_headers()
    path = f"/internal/chunks/{chunk_id}"
    binary = {**put.to_headers(), "Content-Type": "application/octet-stream"}
    cases = [
        (DATA[:-1], binary, "invalid_chunk_length"),
        (DATA + b"x", binary, "invalid_chunk_length"),
        (b"z" * len(DATA), binary, "invalid_chunk_checksum"),
        (DATA, {**binary, "X-Chunk-Length": "1000001"}, "invalid_chunk_length"),
        (DATA, {**binary, "X-Chunk-Length": "1.0"}, "invalid_request"),
        (DATA, {**binary, "X-Object-ID": ""}, "invalid_request"),
        (DATA, {**binary, "Content-Type": "text/plain"}, "invalid_request"),
        (DATA, [*binary.items(), ("X-Chunk-Length", str(len(DATA)))], "invalid_request"),
    ]
    with TestClient(create_app(config)) as client:
        for body, headers, code in cases:
            response = client.put(path, content=body, headers=headers)
            assert response.status_code == 400
            assert response.json()["error"]["code"] == code
        assert client.put("/internal/chunks/invalid-id", content=DATA, headers=binary).status_code == 400
        assert client.get("/internal/workers/worker2/chunks").json()["chunks"] == []
        assert list((config.data_dir / "chunks").iterdir()) == []
        assert client.put(path, content=DATA, headers=binary).status_code == 200
        bad_retry = client.put(path, content=bytes(len(DATA)), headers=binary)
        assert bad_retry.status_code == 400
        assert client.get(path, headers=read_headers(put).to_headers()).content == DATA


@pytest.mark.parametrize(("damage", "code"), [
    ("missing", "chunk_missing"), ("truncated", "chunk_corrupt"), ("bit_flip", "chunk_corrupt"),
])
def test_checked_reads_and_new_repair(config, store, damage, code):
    chunk_id, put = uuid4(), put_headers()
    expected = read_headers(put)
    store.put(chunk_id, put, DATA)
    path = config.data_dir / "chunks" / f"{chunk_id}.bin"
    if damage == "missing":
        path.unlink()
    elif damage == "truncated":
        path.write_bytes(DATA[:-1])
    else:
        path.write_bytes(bytes([DATA[0] ^ 1]) + DATA[1:])

    with pytest.raises(APIError) as error:
        store.get(chunk_id, expected)
    assert error.value.code == code
    with pytest.raises(APIError):
        store.put(chunk_id, put, DATA)
    item = store.inventory().chunks[0]
    assert item.state == ("missing" if damage == "missing" else "corrupt")
    assert item.expected_sha256 == put.sha256

    repair = later_put(put, 2)
    assert store.put(chunk_id, repair, DATA).durable
    assert store.get(chunk_id, expected) == DATA
    with pytest.raises(APIError, match="Read expectations"):
        store.get(chunk_id, expected.model_copy(update={"generation": uuid4()}))
    other = DATA[::-1]
    conflict = later_put(put, 3).model_copy(update={"sha256": sha256(other).hexdigest()})
    with pytest.raises(APIError) as error:
        store.put(chunk_id, conflict, other)
    assert error.value.code == "identity_conflict"


def test_retirement_and_epoch_fences_survive_restart(config):
    chunk_id, put = uuid4(), put_headers()
    delete = next_mutation(put, 3)
    store = ChunkStore(config)
    try:
        store.delete(chunk_id, delete)  # Cleanup can reach a worker before a delayed PUT.
    finally:
        store.close()

    store = ChunkStore(config)
    try:
        store.delete(chunk_id, delete)
        with pytest.raises(APIError, match="superseded"):
            store.put(chunk_id, put, DATA)
        new = later_put(put, 4).model_copy(update={"controller_epoch": 2})
        store.put(chunk_id, new, DATA)
        with pytest.raises(APIError, match="epoch has been superseded"):
            store.put(uuid4(), put, DATA)
        with pytest.raises(APIError, match="Placement mutation has been superseded"):
            store.delete(chunk_id, delete.model_copy(update={"controller_epoch": 2}))
        assert store.get(chunk_id, read_headers(put)) == DATA
    finally:
        store.close()


def test_sequences_on_distinct_chunks_can_arrive_out_of_order(store):
    first, second = uuid4(), uuid4()
    put = put_headers()
    high = later_put(put, 4)
    store.put(first, high, DATA)
    store.put(second, put, DATA)
    store.put(first, later_put(put, 5), DATA)
    reused = put.model_copy(update={"operation_id": high.operation_id, "operation_sequence": 6})
    with pytest.raises(APIError) as error:
        store.put(uuid4(), reused, DATA)
    assert error.value.code == "operation_conflict"


def test_concurrent_retries_share_one_placement(store):
    chunk_id, put = uuid4(), put_headers()
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: store.put(chunk_id, put, DATA), range(8)))
    assert all(receipt == receipts[0] for receipt in receipts)
    assert len(store.inventory().chunks) == 1


def interrupt(*args):
    raise OSError("simulated interruption")


@pytest.mark.parametrize(("method", "recoverable"), [
    ("_begin", False), ("_publish", True), ("_set_state", True),
])
def test_interrupted_put_recovery(config, monkeypatch, method, recoverable):
    chunk_id, put = uuid4(), put_headers()
    store = ChunkStore(config)
    try:
        monkeypatch.setattr(store, method, interrupt)
        with pytest.raises(APIError) as error:
            store.put(chunk_id, put, DATA)
        assert error.value.status_code == 503
    finally:
        store.close()

    recovered = ChunkStore(config)
    try:
        assert list((config.data_dir / "chunks").glob("*.stage")) == []
        if recoverable:
            assert recovered.get(chunk_id, read_headers(put)) == DATA
        else:
            assert recovered.inventory().chunks == []
        assert recovered.put(chunk_id, put, DATA).durable
    finally:
        recovered.close()


def test_pending_put_with_damaged_stage_waits_for_valid_retry(config, monkeypatch):
    chunk_id, put = uuid4(), put_headers()
    store = ChunkStore(config)
    try:
        monkeypatch.setattr(store, "_publish", interrupt)
        with pytest.raises(APIError):
            store.put(chunk_id, put, DATA)
        (config.data_dir / "chunks" / f"{chunk_id}.stage").write_bytes(bytes(len(DATA)))
    finally:
        store.close()
    recovered = ChunkStore(config)
    try:
        assert recovered.inventory().chunks[0].state == "suspect"
        with pytest.raises(APIError, match="publication is incomplete"):
            recovered.get(chunk_id, read_headers(put))
        assert recovered.put(chunk_id, put, DATA).durable
        assert recovered.get(chunk_id, read_headers(put)) == DATA
    finally:
        recovered.close()


def test_interrupted_delete_is_reconciled(config, monkeypatch):
    chunk_id, put = uuid4(), put_headers()
    delete = next_mutation(put, 2)
    store = ChunkStore(config)
    try:
        store.put(chunk_id, put, DATA)
        monkeypatch.setattr(store, "_retire", interrupt)
        with pytest.raises(APIError):
            store.delete(chunk_id, delete)
        assert store.inventory().chunks == []
        with pytest.raises(APIError, match="not stored"):
            store.get(chunk_id, read_headers(put))
    finally:
        store.close()
    recovered = ChunkStore(config)
    try:
        assert list((config.data_dir / "chunks").iterdir()) == []
        recovered.delete(chunk_id, delete)
        with pytest.raises(APIError, match="superseded"):
            recovered.put(chunk_id, put, DATA)
    finally:
        recovered.close()


def test_tombstone_route_removes_only_target_generation(config):
    put = put_headers()
    other = later_put(put, 3).model_copy(update={"generation": uuid4()})
    targets = [uuid4(), uuid4()]
    survivor = uuid4()
    tombstone = tombstone_request(put, 4)
    with TestClient(create_app(config)) as client:
        for chunk_id, headers in zip([*targets, survivor], [put, later_put(put, 2), other]):
            response = client.put(
                f"/internal/chunks/{chunk_id}", content=DATA,
                headers={**headers.to_headers(), "Content-Type": "application/octet-stream"},
            )
            assert response.status_code == 200

        route = f"/internal/objects/{put.object_id}/tombstone"
        response = client.post(route, json=tombstone.model_dump(mode="json"))
        assert response.status_code == 200
        receipt = TombstoneReceipt.model_validate(response.json())
        assert receipt.cleanup_complete and receipt.object_id == put.object_id
        assert receipt.operation_id == tombstone.operation_id and receipt.worker_id == config.worker_id
        assert client.post(route, json=tombstone.model_dump(mode="json")).json() == response.json()
        for chunk_id in targets:
            assert client.get(
                f"/internal/chunks/{chunk_id}", headers=read_headers(put).to_headers(),
            ).status_code == 404
        inventory = WorkerInventory.model_validate(
            client.get("/internal/workers/worker2/chunks").json(),
        )
        assert [entry.chunk_id for entry in inventory.chunks] == [survivor]
        assert list((config.data_dir / "chunks").iterdir()) == [
            config.data_dir / "chunks" / f"{survivor}.bin",
        ]
        late = later_put(put, 5)
        response = client.put(
            f"/internal/chunks/{uuid4()}", content=DATA,
            headers={**late.to_headers(), "Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 409 and response.json()["error"]["code"] == "object_tombstoned"


def test_tombstone_for_absent_object_and_old_controller_epoch(config):
    put = put_headers()
    tombstone = tombstone_request(put, 2)
    store = ChunkStore(config)
    try:
        identity = store.identity
        store.observe_controller_epoch(2)
        # A2 can replay the original durable deletion even if this worker never saw it.
        assert store.tombstone(put.object_id, tombstone).cleanup_complete
        with pytest.raises(APIError, match="Tombstone identity"):
            store.tombstone(uuid4(), tombstone)
    finally:
        store.close()

    restarted = ChunkStore(config)
    try:
        receipt = restarted.tombstone(put.object_id, tombstone)
        assert receipt.storage_id == identity.storage_id and receipt.boot_id != identity.boot_id
        with pytest.raises(APIError, match="Controller epoch has been superseded"):
            restarted.put(uuid4(), put_headers(), DATA)
        late = later_put(put, 3).model_copy(update={"controller_epoch": 3})
        with pytest.raises(APIError) as error:
            restarted.put(uuid4(), late, DATA)
        assert error.value.code == "object_tombstoned"
    finally:
        restarted.close()


def test_mutation_identity_is_shared_between_chunks_and_tombstones(store):
    put = put_headers()
    store.put(uuid4(), put, DATA)
    with pytest.raises(APIError, match="Mutation identity"):
        store.tombstone(put.object_id, tombstone_request(put, 1))

    tombstone = tombstone_request(put, 2)
    store.tombstone(put.object_id, tombstone)
    another = put_headers().model_copy(update={"operation_sequence": 2})
    with pytest.raises(APIError, match="Mutation identity"):
        store.put(uuid4(), another, DATA)


def test_controller_can_reissue_tombstone_in_new_epoch(store):
    put = put_headers()
    original = tombstone_request(put, 2)
    first = store.tombstone(put.object_id, original)
    store.observe_controller_epoch(2)
    with pytest.raises(APIError, match="must advance the epoch and sequence"):
        store.tombstone(put.object_id, tombstone_request(put, 3))
    current = tombstone_request(put, 4).model_copy(update={"controller_epoch": 2})
    refreshed = store.tombstone(put.object_id, current)
    assert refreshed.cleanup_complete and refreshed.operation_id == current.operation_id
    assert refreshed.controller_epoch == 2 and refreshed.operation_sequence == 4
    assert store.tombstone(put.object_id, current) == refreshed
    assert store.tombstone(put.object_id, original) == first
    reused = put_headers().model_copy(update={"operation_sequence": 2, "controller_epoch": 2})
    with pytest.raises(APIError, match="Mutation identity"):
        store.put(uuid4(), reused, DATA)
    late = later_put(put, 5).model_copy(update={"controller_epoch": 2})
    with pytest.raises(APIError, match="has been deleted"):
        store.put(uuid4(), late, DATA)


def test_interrupted_tombstone_hides_chunks_and_recovers_pending_files(config, monkeypatch):
    put = put_headers()
    first, staged = uuid4(), uuid4()
    tombstone = tombstone_request(put, 3)
    store = ChunkStore(config)
    try:
        store.put(first, put, DATA)
        with monkeypatch.context() as patch:
            patch.setattr(store, "_publish", interrupt)
            with pytest.raises(APIError):
                store.put(staged, later_put(put, 2), DATA)
        paths = config.data_dir / "chunks"
        assert (paths / f"{staged}.stage").exists()
        monkeypatch.setattr(store, "_remove_files", interrupt)
        with pytest.raises(APIError) as error:
            store.tombstone(put.object_id, tombstone)
        assert error.value.status_code == 503
        assert store.inventory().chunks == []
        for chunk_id in (first, staged):
            with pytest.raises(APIError) as error:
                store.get(chunk_id, read_headers(put))
            assert error.value.code == "chunk_missing"
    finally:
        store.close()

    restarted = ChunkStore(config)
    try:
        assert list(paths.iterdir()) == []
        assert restarted.tombstone(put.object_id, tombstone).cleanup_complete
        with pytest.raises(APIError, match="has been deleted"):
            restarted.put(staged, later_put(put, 4), DATA)
    finally:
        restarted.close()
