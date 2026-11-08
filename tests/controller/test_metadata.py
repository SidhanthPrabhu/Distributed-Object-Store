import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from common.config import WORKER_IDS
from common.errors import APIError
from controller.metadata import MetadataStore


def setup_upload(store, identities, manifest_factory):
    for identity in identities:
        store.register(identity)
    manifest = manifest_factory()
    store.create_upload(manifest)
    return manifest


def test_upload_hidden_until_two_durable_distinct_copies(store, identities, manifest_factory, receipt_factory):
    manifest = setup_upload(store, identities, manifest_factory)
    with pytest.raises(APIError) as exc:
        store.get_manifest(manifest.object_id)
    assert exc.value.status_code == 404
    for chunk in manifest.chunks:
        op = store.create_put_intent(manifest.object_id, chunk.chunk_id, "worker1")
        store.record_receipt(receipt_factory(op))
        store.record_receipt(receipt_factory(op))  # A retry is not a second copy.
    with pytest.raises(APIError, match="two durable"):
        store.publish_upload(manifest.object_id)
    for chunk in manifest.chunks:
        op = store.create_put_intent(manifest.object_id, chunk.chunk_id, "worker2")
        store.record_receipt(receipt_factory(op))
    store.publish_upload(manifest.object_id)
    result = store.get_manifest(manifest.object_id)
    assert all(c.locations == ["worker1", "worker2"] for c in result.chunks)
    assert result.checksum == manifest.checksum
    assert result.custom_metadata == manifest.custom_metadata


def test_empty_upload_has_no_fake_chunks(store, manifest_factory):
    manifest = manifest_factory(b"")
    store.create_upload(manifest)
    store.publish_upload(manifest.object_id)
    assert store.get_manifest(manifest.object_id) == manifest


@pytest.mark.parametrize("changes", [
    {"sha256": "0" * 64}, {"length_bytes": 100}, {"operation_sequence": 999},
    {"generation": uuid4()}, {"storage_id": uuid4()}, {"boot_id": uuid4()},
    {"worker_id": "worker2"}, {"controller_epoch": 999},
])
def test_receipt_must_match_durable_intent(store, identities, manifest_factory, receipt_factory, changes):
    manifest = setup_upload(store, identities, manifest_factory)
    op = store.create_put_intent(manifest.object_id, manifest.chunks[0].chunk_id, "worker1")
    with pytest.raises(APIError) as exc:
        store.record_receipt(receipt_factory(op, **changes))
    assert exc.value.code == "receipt_mismatch"
    result = store.get_manifest(manifest.object_id, include_hidden=True)
    assert result == manifest
    assert len(store.pending_operations()) == 1


def test_restart_retains_manifest_intents_tombstones_and_sequences(tmp_path, identities, manifest_factory, receipt_factory):
    path = tmp_path / "controller.sqlite3"
    first = MetadataStore(path)
    try:
        manifest = setup_upload(first, identities, manifest_factory)
        pending = first.create_put_intent(manifest.object_id, manifest.chunks[0].chunk_id, "worker1")
        old_epoch = first.epoch
        deletion = first.begin_deletion(manifest.object_id, WORKER_IDS)
    finally:
        first.close()
    second = MetadataStore(path)
    try:
        assert second.epoch == old_epoch + 1
        assert second.get_manifest(manifest.object_id, include_hidden=True) == manifest
        assert second.object_state(manifest.object_id) == "DELETING"
        assert len(second.pending_cleanup()) == 4
        assert len(second.pending_operations()) == 2
        assert second.begin_deletion(manifest.object_id, WORKER_IDS) == second.pending_operations()[1]
        with pytest.raises(APIError) as exc:
            second.record_receipt(receipt_factory(pending))
        assert exc.value.code == "stale_operation"
        new_manifest = manifest_factory()
        second.create_upload(new_manifest)
        new_op = second.create_put_intent(new_manifest.object_id, new_manifest.chunks[0].chunk_id, "worker1")
        assert new_op["operation_sequence"] > deletion["operation_sequence"]
        assert new_op["controller_epoch"] == second.epoch
    finally:
        second.close()


def test_delete_fences_delayed_receipts_and_hides_object(store, identities, manifest_factory, receipt_factory):
    manifest = setup_upload(store, identities, manifest_factory)
    op = store.create_put_intent(manifest.object_id, manifest.chunks[0].chunk_id, "worker1")
    deletion = store.begin_deletion(manifest.object_id, WORKER_IDS)
    assert deletion["operation_sequence"] > op["operation_sequence"]
    with pytest.raises(APIError, match="changed"):
        store.record_receipt(receipt_factory(op))
    with pytest.raises(APIError):
        store.publish_upload(manifest.object_id)
    with pytest.raises(APIError):
        store.create_put_intent(manifest.object_id, manifest.chunks[0].chunk_id, "worker2")
    with pytest.raises(APIError):
        store.get_manifest(manifest.object_id)


def test_sequences_are_unique_under_concurrent_requests(store, identities, manifest_factory):
    manifest = setup_upload(store, identities, manifest_factory)
    def allocate(_):
        return store.create_put_intent(manifest.object_id, manifest.chunks[0].chunk_id, "worker1")
    with ThreadPoolExecutor(max_workers=8) as executor:
        operations = list(executor.map(allocate, range(32)))
    assert sorted(op["operation_sequence"] for op in operations) == list(range(1, 33))
    assert len({op["operation_id"] for op in operations}) == 32


def test_old_placement_cannot_replace_newer_receipt(store, identities, manifest_factory, receipt_factory):
    manifest = setup_upload(store, identities, manifest_factory)
    old = store.create_put_intent(manifest.object_id, manifest.chunks[0].chunk_id, "worker1")
    new = store.create_put_intent(manifest.object_id, manifest.chunks[0].chunk_id, "worker1")
    store.record_receipt(receipt_factory(new))
    with pytest.raises(APIError) as exc:
        store.record_receipt(receipt_factory(old))
    assert exc.value.code == "stale_operation"


def test_worker_restart_invalidates_unpublished_receipts(store, identities, manifest_factory, receipt_factory):
    from common.models import WorkerRegistration

    manifest = setup_upload(store, identities, manifest_factory)
    for chunk in manifest.chunks:
        for worker_id in ("worker1", "worker2"):
            op = store.create_put_intent(manifest.object_id, chunk.chunk_id, worker_id)
            store.record_receipt(receipt_factory(op))
    store.register(WorkerRegistration(worker_id="worker1", storage_id=identities[0].storage_id,
                                      boot_id=uuid4()))
    with pytest.raises(APIError, match="two durable"):
        store.publish_upload(manifest.object_id)


def test_controller_restart_requires_recovery_before_upload_publication(tmp_path, identities, manifest_factory, receipt_factory):
    path = tmp_path / "controller.sqlite3"
    store = MetadataStore(path)
    try:
        manifest = setup_upload(store, identities, manifest_factory)
        for chunk in manifest.chunks:
            for worker_id in ("worker1", "worker2"):
                op = store.create_put_intent(manifest.object_id, chunk.chunk_id, worker_id)
                store.record_receipt(receipt_factory(op))
    finally:
        store.close()
    store = MetadataStore(path)
    try:
        with pytest.raises(APIError, match="two durable"):
            store.publish_upload(manifest.object_id)
        assert store.object_state(manifest.object_id) == "UPLOADING"
    finally:
        store.close()


def test_failed_transaction_rolls_back_all_records(store, manifest_factory):
    manifest = manifest_factory()
    with pytest.raises(RuntimeError):
        with store.transaction() as db:
            db.execute("UPDATE counters SET sequence=99")
            raise RuntimeError("injected interruption")
    store.create_upload(manifest)
    with store.transaction() as db:
        assert db.execute("SELECT sequence FROM counters").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO replicas VALUES (?,?,?,?)", (str(uuid4()), "worker1", str(uuid4()), "{}"))
