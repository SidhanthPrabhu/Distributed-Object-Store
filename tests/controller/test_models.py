from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from common.config import ControllerConfig, StorageConfig
from common.models import (
    ChunkPutHeaders, ChunkReadHeaders, ObjectMetadata, WorkerHeartbeat, WorkerRegistration,
)


def test_course_defaults_and_environment(monkeypatch, tmp_path):
    assert StorageConfig().model_dump() == {"chunk_size_bytes": 1_000_000, "replication_factor": 2}
    monkeypatch.setenv("OBJECT_STORE_DATABASE_PATH", str(tmp_path / "metadata.sqlite3"))
    monkeypatch.setenv("OBJECT_STORE_HTTP_MAX_CONNECTIONS", "7")
    config = ControllerConfig.from_env()
    assert config.database_path == tmp_path / "metadata.sqlite3"
    assert config.http_max_connections == 7
    assert config.worker_addresses["worker4"] == "http://worker4:8000"


@pytest.mark.parametrize("changes", [
    {"replication_factor": 3}, {"chunk_size_bytes": 0}, {"http_max_connections": True},
    {"suspect_after_seconds": 6}, {"heartbeat_interval_seconds": float("nan")},
    {"http_timeout_seconds": float("inf")},
    {"worker_addresses": {"worker1": "http://worker1"}},
    {"worker_addresses": {f"worker{i}": "http://same:8000" for i in range(1, 5)}},
    {"worker_addresses": {f"worker{i}": f"http://user:secret@worker{i}" for i in range(1, 5)}},
    {"worker_addresses": {f"worker{i}": f"http://worker{i}/fetch" for i in range(1, 5)}},
    {"worker_addresses": {f"worker{i}": f"file://worker{i}" for i in range(1, 5)}},
])
def test_invalid_configuration_fails_early(changes):
    with pytest.raises(ValidationError):
        ControllerConfig(**changes)


def test_equivalent_origins_cannot_alias_workers():
    addresses = dict(ControllerConfig().worker_addresses)
    addresses.update(worker1="http://same", worker2="http://SAME:80/")
    with pytest.raises(ValidationError):
        ControllerConfig(worker_addresses=addresses)


def test_identity_rejects_fetch_url_and_invalid_ids(identities):
    body = identities[0].model_dump(mode="json")
    for changes in ({"worker_id": "worker5"}, {"boot_id": "../../data"},
                    {"address": "http://untrusted"}):
        with pytest.raises(ValidationError):
            WorkerRegistration.model_validate({**body, **changes})


def test_heartbeat_bound_and_utc(identities):
    report = dict(report_id=uuid4(), object_id=uuid4(), generation=uuid4(), chunk_id=uuid4(),
                  observed_at="2026-11-08T10:00:00+05:30", outcome="corrupt")
    base = dict(**identities[0].model_dump(), free_bytes=0, integrity_reports_more=False)
    heartbeat = WorkerHeartbeat(**base, integrity_reports=[report])
    assert heartbeat.integrity_reports[0].observed_at == datetime(2026, 11, 8, 4, 30, tzinfo=timezone.utc)
    assert "Z" in heartbeat.model_dump_json()
    with pytest.raises(ValidationError):
        WorkerHeartbeat(**base, integrity_reports=[report] * 101)
    with pytest.raises(ValidationError):
        WorkerHeartbeat(**base, integrity_reports=[{**report, "observed_at": "2026-11-08T10:00:00"}])
    with pytest.raises(ValidationError):
        WorkerHeartbeat(**{**base, "free_bytes": 2**63}, integrity_reports=[])


def test_mutation_headers_match_contract():
    headers = ChunkPutHeaders(
        object_id=uuid4(), generation=uuid4(), operation_id=uuid4(), controller_epoch=1,
        operation_sequence=2, sha256="a" * 64, length_bytes=123,
    ).to_headers()
    assert set(headers) == {
        "X-Object-ID", "X-Object-Generation", "X-Operation-ID", "X-Controller-Epoch",
        "X-Operation-Sequence", "X-Chunk-SHA256", "X-Chunk-Length",
    }
    assert headers["X-Operation-Sequence"] == "2"
    assert ChunkReadHeaders(generation=uuid4(), sha256="b" * 64, length_bytes=0).to_headers()[
        "X-Chunk-Length"] == "0"
    with pytest.raises(ValidationError):
        ChunkReadHeaders(generation=uuid4(), sha256="B" * 64, length_bytes=0)


@pytest.mark.parametrize("data", [b"", b"x", b"xxx", b"xxxx", b"xxxxx", b"xxxxxxxx", b"xxxxxxxxx"])
def test_manifest_boundary_shapes(manifest_factory, data):
    manifest = manifest_factory(data)
    assert ObjectMetadata.model_validate_json(manifest.model_dump_json()) == manifest


def test_manifest_rejects_bad_order_lengths_hashes_and_locations(manifest_factory):
    raw = manifest_factory().model_dump(mode="json")
    variants = [
        {**raw, "chunk_count": 3},
        {**raw, "size_bytes": 7},
        {**raw, "chunks": list(reversed(raw["chunks"]))},
        {**raw, "chunks": [{**raw["chunks"][0], "locations": ["worker1", "worker1"]}, raw["chunks"][1]]},
        {**raw, "chunks": [{**raw["chunks"][0], "size_bytes": 3}, {**raw["chunks"][1], "size_bytes": 3}]},
    ]
    for variant in variants:
        with pytest.raises(ValidationError):
            ObjectMetadata.model_validate(variant)
    empty = manifest_factory(b"").model_dump(mode="json")
    empty["checksum"]["value"] = "0" * 64
    with pytest.raises(ValidationError):
        ObjectMetadata.model_validate(empty)
