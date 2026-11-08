from datetime import datetime, timezone
from uuid import uuid4

import pytest

from common.config import ControllerConfig
from common.errors import APIError
from common.models import IntegrityReport, WorkerHeartbeat, WorkerRegistration
from controller.metadata import MetadataStore
from controller.registry import WorkerRegistry


def heartbeat(identity, reports=None):
    return WorkerHeartbeat(**identity.model_dump(), free_bytes=123,
                           integrity_reports=reports or [], integrity_reports_more=False)


def report():
    return IntegrityReport(report_id=uuid4(), object_id=uuid4(), generation=uuid4(),
                           chunk_id=uuid4(), observed_at=datetime.now(timezone.utc), outcome="corrupt")


def test_four_workers_register_but_are_not_trusted(store, identities):
    registry = WorkerRegistry(store, ControllerConfig())
    for identity in identities:
        assert registry.register(identity).state == "RECONCILING"
        assert registry.heartbeat(heartbeat(identity)).state == "RECONCILING"
    assert len(registry.snapshot()) == 4
    assert registry.eligible_worker_ids() == []


def test_persistent_storage_bindings_and_stale_boots(store, identities):
    registry = WorkerRegistry(store, ControllerConfig())
    original = identities[0]
    registry.register(original)
    with pytest.raises(APIError) as exc:
        registry.register(WorkerRegistration(worker_id="worker2", storage_id=original.storage_id, boot_id=uuid4()))
    assert exc.value.code == "identity_conflict"
    with pytest.raises(APIError):
        registry.register(WorkerRegistration(worker_id="worker1", storage_id=uuid4(), boot_id=uuid4()))
    replacement = WorkerRegistration(worker_id="worker1", storage_id=original.storage_id, boot_id=uuid4())
    registry.register(replacement)
    for action in (lambda: registry.heartbeat(heartbeat(original)), lambda: registry.register(original)):
        with pytest.raises(APIError) as exc:
            action()
        assert exc.value.code == "registration_required"
    assert registry.heartbeat(heartbeat(replacement)).state == "RECONCILING"


def test_local_heartbeat_age_transitions_without_remote_clock(store, identities):
    now = [100.0]
    registry = WorkerRegistry(store, ControllerConfig(), clock=lambda: now[0])
    registry.register(identities[0])
    now[0] += 3.1
    assert registry.snapshot()[0]["state"] == "SUSPECT"
    now[0] += 2.1
    assert registry.snapshot()[0]["state"] == "UNAVAILABLE"
    assert registry.eligible_worker_ids() == []
    registry.heartbeat(heartbeat(identities[0]))
    assert registry.snapshot()[0]["state"] == "RECONCILING"


def test_reports_acknowledged_after_durable_commit_and_deduplicated(tmp_path, identities):
    path = tmp_path / "registry.sqlite3"
    store = MetadataStore(path)
    fault = report()
    try:
        registry = WorkerRegistry(store, ControllerConfig())
        registry.register(identities[0])
        message = heartbeat(identities[0], [fault])
        for _ in range(2):
            assert registry.heartbeat(message).acknowledged_report_ids == [fault.report_id]
        assert len(store.pending_reports()) == 1
    finally:
        store.close()
    restarted = MetadataStore(path)
    try:
        registry = WorkerRegistry(restarted, ControllerConfig())
        with pytest.raises(APIError) as exc:
            registry.heartbeat(message)
        assert exc.value.code == "registration_required"
        registry.register(identities[0])
        assert registry.heartbeat(message).acknowledged_report_ids == [fault.report_id]
        assert len(restarted.pending_reports()) == 1
    finally:
        restarted.close()


def test_conflicting_report_batch_rolls_back_without_ack(store, identities):
    registry = WorkerRegistry(store, ControllerConfig())
    registry.register(identities[0])
    fault = report()
    registry.heartbeat(heartbeat(identities[0], [fault]))
    new_fault = report()
    changed_fault = IntegrityReport(**{**fault.model_dump(), "outcome": "missing"})
    with pytest.raises(APIError) as exc:
        registry.heartbeat(heartbeat(identities[0], [new_fault, changed_fault]))
    assert exc.value.code == "report_conflict"
    assert [r["report_id"] for r in store.pending_reports()] == [str(fault.report_id)]


def test_report_storage_failure_is_not_acknowledged(store, identities):
    import sqlite3

    registry = WorkerRegistry(store, ControllerConfig())
    registry.register(identities[0])
    with store.transaction() as db:
        db.execute("""CREATE TRIGGER fail_report BEFORE INSERT ON integrity_reports
            BEGIN SELECT RAISE(ABORT, 'injected report storage failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        registry.heartbeat(heartbeat(identities[0], [report()]))
    assert store.pending_reports() == []
    assert store.workers()[0]["last_heartbeat_at"] is None
