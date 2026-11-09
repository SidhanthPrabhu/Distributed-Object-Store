"""Controller/worker wire types from docs/COURSE-CONTRACT.md (Pydantic 2)."""

from datetime import timezone
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, JsonValue,
    field_validator, model_validator,
)

WorkerID = Literal["worker1", "worker2", "worker3", "worker4"]
SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
PositiveInt = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
UTCDateTime = Annotated[
    AwareDatetime, AfterValidator(lambda value: value.astimezone(timezone.utc))
]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ErrorDetail(WireModel):
    code: str
    message: str
    request_id: UUID


class ErrorEnvelope(WireModel):
    error: ErrorDetail


class WorkerState(StrEnum):
    RECONCILING = "RECONCILING"
    HEALTHY = "HEALTHY"
    SUSPECT = "SUSPECT"
    UNAVAILABLE = "UNAVAILABLE"


class WorkerIdentity(WireModel):
    worker_id: WorkerID
    storage_id: UUID
    boot_id: UUID


class WorkerRegistration(WorkerIdentity):
    pass


class RegistrationResponse(WireModel):
    controller_epoch: PositiveInt
    heartbeat_interval_seconds: Annotated[float, Field(gt=0)]
    state: Literal["RECONCILING"] = "RECONCILING"


class IntegrityReport(WireModel):
    report_id: UUID
    object_id: UUID
    generation: UUID
    chunk_id: UUID
    observed_at: UTCDateTime
    outcome: Literal["corrupt", "missing", "read_error"]


class WorkerHeartbeat(WorkerIdentity):
    free_bytes: NonNegativeInt
    integrity_reports: Annotated[list[IntegrityReport], Field(max_length=100)]
    integrity_reports_more: Annotated[bool, Field(strict=True)]


class HeartbeatResponse(WireModel):
    controller_epoch: PositiveInt
    state: WorkerState
    acknowledged_report_ids: list[UUID]


class InventoryChunk(WireModel):
    object_id: UUID
    generation: UUID
    chunk_id: UUID
    length_bytes: NonNegativeInt
    expected_sha256: SHA256
    # The contract does not yet enumerate catalog state spellings. No state,
    # including a positive one, substitutes for a freshly checked read.
    state: Annotated[str, Field(min_length=1, max_length=64)]
    operation_sequence: PositiveInt
    last_verified_at: UTCDateTime | None


class WorkerInventory(WorkerIdentity):
    chunks: list[InventoryChunk]

    @model_validator(mode="after")
    def unique_chunks(self) -> Self:
        if len({c.chunk_id for c in self.chunks}) != len(self.chunks):
            raise ValueError("Inventory contains duplicate chunk IDs")
        return self


class MutationHeaders(WireModel):
    object_id: UUID
    generation: UUID
    operation_id: UUID
    controller_epoch: PositiveInt
    operation_sequence: PositiveInt

    def to_headers(self) -> dict[str, str]:
        return {
            "X-Object-ID": str(self.object_id),
            "X-Object-Generation": str(self.generation),
            "X-Operation-ID": str(self.operation_id),
            "X-Controller-Epoch": str(self.controller_epoch),
            "X-Operation-Sequence": str(self.operation_sequence),
        }


class ChunkReadHeaders(WireModel):
    generation: UUID
    sha256: SHA256
    length_bytes: NonNegativeInt

    def to_headers(self) -> dict[str, str]:
        return {
            "X-Object-Generation": str(self.generation),
            "X-Chunk-SHA256": self.sha256,
            "X-Chunk-Length": str(self.length_bytes),
        }


class ChunkPutHeaders(MutationHeaders):
    sha256: SHA256
    length_bytes: NonNegativeInt

    def to_headers(self) -> dict[str, str]:
        return {
            **super().to_headers(),
            "X-Chunk-SHA256": self.sha256,
            "X-Chunk-Length": str(self.length_bytes),
        }


class ChunkReceipt(MutationHeaders, WorkerIdentity):
    chunk_id: UUID
    length_bytes: NonNegativeInt
    sha256: SHA256
    durable: Literal[True]

    @field_validator("durable", mode="before")
    @classmethod
    def durable_boolean(cls, value):
        if value is not True:
            raise ValueError("Durability must be boolean true")
        return value


class TombstoneRequest(WireModel):
    generation: UUID
    operation_id: UUID
    controller_epoch: PositiveInt
    operation_sequence: PositiveInt


class TombstoneReceipt(MutationHeaders, WorkerIdentity):
    cleanup_complete: Literal[True]

    @field_validator("cleanup_complete", mode="before")
    @classmethod
    def cleanup_boolean(cls, value):
        if value is not True:
            raise ValueError("Cleanup completion must be boolean true")
        return value


class Checksum(WireModel):
    algorithm: Literal["sha256"] = "sha256"
    value: SHA256


class ChunkMetadata(WireModel):
    chunk_id: UUID
    ordinal: NonNegativeInt
    size_bytes: PositiveInt
    sha256: SHA256
    locations: list[WorkerID] = Field(default_factory=list)

    @model_validator(mode="after")
    def distinct_locations(self) -> Self:
        if len(set(self.locations)) != len(self.locations):
            raise ValueError("Locations must be distinct workers")
        return self


class ObjectSummary(WireModel):
    object_id: UUID
    name: Annotated[str, Field(min_length=1)]
    size_bytes: NonNegativeInt
    content_type: Annotated[str, Field(min_length=1)]


class ObjectMetadata(ObjectSummary):
    generation: UUID
    created_at: UTCDateTime
    modified_at: UTCDateTime
    checksum: Checksum
    chunk_size_bytes: PositiveInt
    chunk_count: NonNegativeInt
    metadata_revision: PositiveInt = 1
    custom_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    chunks: list[ChunkMetadata]

    @model_validator(mode="after")
    def ordered_manifest(self) -> Self:
        if self.chunk_count != len(self.chunks):
            raise ValueError("chunk_count does not match chunks")
        if [c.ordinal for c in self.chunks] != list(range(self.chunk_count)):
            raise ValueError("Chunks must be ordered with contiguous ordinals from zero")
        if len({c.chunk_id for c in self.chunks}) != self.chunk_count:
            raise ValueError("Chunk IDs must be unique")
        if sum(c.size_bytes for c in self.chunks) != self.size_bytes:
            raise ValueError("Chunk lengths do not match object size")
        if any(c.size_bytes != self.chunk_size_bytes for c in self.chunks[:-1]):
            raise ValueError("Only the final chunk may be smaller than chunk size")
        if self.chunks and self.chunks[-1].size_bytes > self.chunk_size_bytes:
            raise ValueError("Final chunk exceeds chunk size")
        if not self.chunks and self.checksum.value != sha256(b"").hexdigest():
            raise ValueError("An empty object must have SHA-256 of empty bytes")
        if self.modified_at < self.created_at:
            raise ValueError("Modification time precedes creation")
        return self


class ObjectList(WireModel):
    objects: list[ObjectSummary]
