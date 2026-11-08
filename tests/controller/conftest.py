from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

import pytest

from common.models import ChunkMetadata, ChunkReceipt, Checksum, ObjectMetadata, WorkerRegistration
from controller.metadata import MetadataStore


@pytest.fixture
def store(tmp_path):
    instance = MetadataStore(tmp_path / "controller.sqlite3")
    yield instance
    instance.close()


@pytest.fixture
def identities():
    return [WorkerRegistration(worker_id=f"worker{i}", storage_id=uuid4(), boot_id=uuid4())
            for i in range(1, 5)]


@pytest.fixture
def manifest_factory():
    def make(data=b"abcdef", chunk_size=4):
        now = datetime.now(timezone.utc)
        chunks = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]
        return ObjectMetadata(
            object_id=uuid4(), generation=uuid4(), name="example.bin", size_bytes=len(data),
            content_type="application/octet-stream", created_at=now, modified_at=now,
            checksum=Checksum(value=sha256(data).hexdigest()), chunk_size_bytes=chunk_size,
            chunk_count=len(chunks), custom_metadata={"course": {"part": 1}},
            chunks=[ChunkMetadata(chunk_id=uuid4(), ordinal=i, size_bytes=len(chunk),
                                  sha256=sha256(chunk).hexdigest())
                    for i, chunk in enumerate(chunks)],
        )
    return make


@pytest.fixture
def receipt_factory():
    def make(intent, **changes):
        fields = {name: intent[name] for name in ChunkReceipt.model_fields if name != "durable"}
        return ChunkReceipt(**{**fields, "durable": True, **changes})
    return make
