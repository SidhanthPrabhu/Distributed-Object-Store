"""Durable local placements. One worker process owns each volume."""

import logging
import os
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

from common.errors import APIError
from common.models import (
    ChunkPutHeaders, ChunkReadHeaders, ChunkReceipt, InventoryChunk,
    MutationHeaders, TombstoneReceipt, TombstoneRequest, WorkerIdentity, WorkerInventory,
)
from worker.config import WorkerConfig

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS worker (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    worker_id TEXT NOT NULL,
    storage_id TEXT NOT NULL,
    controller_epoch INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS operations (
    operation_sequence INTEGER PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    controller_epoch INTEGER NOT NULL,
    chunk_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('put', 'delete'))
);
CREATE TABLE IF NOT EXISTS placements (
    chunk_id TEXT PRIMARY KEY,
    object_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    length_bytes INTEGER,
    expected_sha256 TEXT,
    operation_sequence INTEGER NOT NULL REFERENCES operations(operation_sequence),
    state TEXT NOT NULL CHECK (
        state IN ('pending_put', 'pending_delete', 'valid', 'missing', 'corrupt', 'retired')
    ),
    last_verified_at TEXT
);
CREATE INDEX IF NOT EXISTS placements_by_generation ON placements(object_id, generation);
CREATE TABLE IF NOT EXISTS object_tombstones (
    object_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    controller_epoch INTEGER NOT NULL,
    operation_sequence INTEGER NOT NULL UNIQUE,
    cleanup_complete INTEGER NOT NULL DEFAULT 0 CHECK (cleanup_complete IN (0, 1)),
    PRIMARY KEY (object_id, generation)
);
CREATE TABLE IF NOT EXISTS tombstone_history (
    object_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    controller_epoch INTEGER NOT NULL,
    operation_sequence INTEGER PRIMARY KEY,
    FOREIGN KEY (object_id, generation) REFERENCES object_tombstones(object_id, generation)
);
"""


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ChunkStore:
    def __init__(self, config: WorkerConfig):
        self._lock = Lock()
        self._max_length = config.chunk_size_bytes
        self._chunks = config.data_dir / "chunks"
        self._chunks.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(config.data_dir / "catalog.sqlite3", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.executescript(SCHEMA)
            with self._db:
                self._db.execute(
                    "INSERT OR IGNORE INTO worker VALUES (1, ?, ?, 0)",
                    (config.worker_id, str(uuid4())),
                )
            worker = self._db.execute("SELECT * FROM worker WHERE singleton = 1").fetchone()
            if worker["worker_id"] != config.worker_id:
                raise ValueError("The worker volume belongs to a different worker")
            self.identity = WorkerIdentity(
                worker_id=config.worker_id, storage_id=worker["storage_id"], boot_id=uuid4(),
            )
            sync_directory(config.data_dir)
            sync_directory(config.data_dir.parent)
            self._recover_tombstones()
            self._recover()
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _access(self) -> Iterator[None]:
        with self._lock:
            try:
                yield
            except (OSError, sqlite3.Error) as exc:
                logger.exception("Worker storage operation failed")
                raise APIError(503, "storage_unavailable", "Worker storage operation failed") from exc

    def _path(self, chunk_id: str) -> Path:
        return self._chunks / f"{chunk_id}.bin"

    def _stage(self, chunk_id: str) -> Path:
        return self._chunks / f"{chunk_id}.stage"

    def _placement(self, chunk_id: str) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM placements WHERE chunk_id = ?", (chunk_id,),
        ).fetchone()

    def free_bytes(self) -> int:
        with self._access():
            return shutil.disk_usage(self._chunks).free

    def _check_epoch(self, epoch: int) -> None:
        current = self._db.execute("SELECT controller_epoch FROM worker").fetchone()[0]
        if epoch < current:
            raise APIError(409, "stale_mutation", "Controller epoch has been superseded")

    def _advance_epoch(self, epoch: int) -> None:
        self._db.execute("UPDATE worker SET controller_epoch = ? WHERE controller_epoch < ?", (epoch, epoch))

    def observe_controller_epoch(self, epoch: int) -> None:
        """Fence old mutations as soon as the worker hears from a new controller boot."""
        with self._access(), self._db:
            self._check_epoch(epoch)
            self._advance_epoch(epoch)

    def _tombstone_operations(self, operation_id: str, sequence: int) -> list[sqlite3.Row]:
        return self._db.execute(
            """SELECT object_id, generation, operation_id, controller_epoch, operation_sequence
               FROM object_tombstones WHERE operation_id = ? OR operation_sequence = ?
               UNION ALL
               SELECT object_id, generation, operation_id, controller_epoch, operation_sequence
               FROM tombstone_history WHERE operation_id = ? OR operation_sequence = ?""",
            (operation_id, sequence, operation_id, sequence),
        ).fetchall()

    def _check_mutation(self, chunk_id: str, headers: MutationHeaders, kind: str,
                        row: sqlite3.Row | None) -> bool:
        """Return whether this is a retry of the current placement mutation."""
        self._check_epoch(headers.controller_epoch)
        if row is not None:
            if headers.operation_sequence < row["operation_sequence"]:
                raise APIError(409, "stale_mutation", "Placement mutation has been superseded")
            if (row["object_id"], row["generation"]) != (
                str(headers.object_id), str(headers.generation),
            ):
                raise APIError(409, "identity_conflict", "Chunk belongs to a different object generation")

        if self._tombstone_operations(str(headers.operation_id), headers.operation_sequence):
            raise APIError(409, "operation_conflict", "Mutation identity was already used")
        previous = self._db.execute(
            """SELECT operation_sequence, operation_id, controller_epoch, chunk_id, kind
               FROM operations WHERE operation_sequence = ? OR operation_id = ?""",
            (headers.operation_sequence, str(headers.operation_id)),
        ).fetchall()
        expected = (
            headers.operation_sequence, str(headers.operation_id), headers.controller_epoch,
            chunk_id, kind,
        )
        if previous and (len(previous) != 1 or tuple(previous[0]) != expected):
            raise APIError(409, "operation_conflict", "Mutation identity was already used")
        return bool(previous)

    def _begin(self, chunk_id: str, headers: MutationHeaders, kind: str,
               length: int | None, digest: str | None) -> None:
        # Journal ownership before creating a staging file or removing files.
        with self._db:
            self._advance_epoch(headers.controller_epoch)
            self._db.execute(
                "INSERT INTO operations VALUES (?, ?, ?, ?, ?)",
                (headers.operation_sequence, str(headers.operation_id),
                 headers.controller_epoch, chunk_id, kind),
            )
            self._db.execute(
                """INSERT INTO placements VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(chunk_id) DO UPDATE SET
                       length_bytes = coalesce(placements.length_bytes, excluded.length_bytes),
                       expected_sha256 = coalesce(placements.expected_sha256, excluded.expected_sha256),
                       operation_sequence = excluded.operation_sequence,
                       state = excluded.state""",
                (chunk_id, str(headers.object_id), str(headers.generation), length, digest,
                 headers.operation_sequence, f"pending_{kind}"),
            )

    def _set_state(self, chunk_id: str, state: str) -> None:
        verified_at = datetime.now(timezone.utc).isoformat() if state == "valid" else None
        with self._db:
            self._db.execute(
                """UPDATE placements SET state = ?,
                          last_verified_at = coalesce(?, last_verified_at) WHERE chunk_id = ?""",
                (state, verified_at, chunk_id),
            )

    @staticmethod
    def _check_file(path: Path, length: int, digest: str) -> tuple[str, bytes]:
        try:
            with path.open("rb") as source:
                data = source.read(length + 1)
        except FileNotFoundError:
            return "missing", b""
        if len(data) != length or sha256(data).hexdigest() != digest:
            return "corrupt", b""
        return "valid", data

    def _publish(self, chunk_id: str) -> None:
        os.replace(self._stage(chunk_id), self._path(chunk_id))
        sync_directory(self._chunks)
        self._set_state(chunk_id, "valid")

    def _remove_files(self, chunk_id: str) -> None:
        self._path(chunk_id).unlink(missing_ok=True)
        self._stage(chunk_id).unlink(missing_ok=True)

    def _retire(self, chunk_id: str) -> None:
        self._remove_files(chunk_id)
        sync_directory(self._chunks)
        self._set_state(chunk_id, "retired")

    def _finish_tombstone(self, object_id: str, generation: str) -> None:
        rows = self._db.execute(
            "SELECT chunk_id FROM placements WHERE object_id = ? AND generation = ?",
            (object_id, generation),
        )
        for row in rows:
            self._remove_files(row["chunk_id"])
        sync_directory(self._chunks)
        with self._db:
            self._db.execute(
                """UPDATE object_tombstones SET cleanup_complete = 1
                   WHERE object_id = ? AND generation = ?""",
                (object_id, generation),
            )

    def _recover_tombstones(self) -> None:
        pending = self._db.execute(
            "SELECT object_id, generation FROM object_tombstones WHERE cleanup_complete = 0",
        ).fetchall()
        for row in pending:
            self._finish_tombstone(row["object_id"], row["generation"])

    def _recover(self) -> None:
        rows = self._db.execute(
            "SELECT * FROM placements WHERE state IN ('pending_put', 'pending_delete')",
        ).fetchall()
        for row in rows:
            chunk_id = row["chunk_id"]
            if row["state"] == "pending_delete":
                self._retire(chunk_id)
                continue
            expected = (row["length_bytes"], row["expected_sha256"])
            if self._check_file(self._stage(chunk_id), *expected)[0] == "valid":
                self._publish(chunk_id)
            elif self._check_file(self._path(chunk_id), *expected)[0] == "valid":
                # The previous process renamed the flushed stage before committing.
                sync_directory(self._chunks)
                self._set_state(chunk_id, "valid")
            # An unrecoverable pending intent remains fenced and can be retried.
        for stage in self._chunks.glob("*.stage"):
            stage.unlink()
        sync_directory(self._chunks)

    def put(self, chunk_id: UUID, headers: ChunkPutHeaders, data: bytes | bytearray) -> ChunkReceipt:
        if len(data) != headers.length_bytes or len(data) > self._max_length:
            raise APIError(400, "invalid_chunk_length", "Chunk body does not match the allowed length")
        if sha256(data).hexdigest() != headers.sha256:
            raise APIError(400, "invalid_chunk_checksum", "Chunk body does not match the expected SHA-256")
        key = str(chunk_id)
        with self._access():
            if self._db.execute(
                "SELECT 1 FROM object_tombstones WHERE object_id = ? AND generation = ?",
                (str(headers.object_id), str(headers.generation)),
            ).fetchone() is not None:
                raise APIError(409, "object_tombstoned", "Object generation has been deleted")
            row = self._placement(key)
            retry = self._check_mutation(key, headers, "put", row)
            expected = (headers.length_bytes, headers.sha256)
            if row is not None and row["expected_sha256"] is not None:
                if (row["length_bytes"], row["expected_sha256"]) != expected:
                    raise APIError(409, "identity_conflict", "Chunk content identity cannot change")

            if retry and row["state"] != "pending_put":
                # A completed operation can only be replayed against a checked copy.
                self._read_checked(key, *expected)
            else:
                if not retry:
                    self._begin(key, headers, "put", *expected)
                with self._stage(key).open("wb") as staged:
                    staged.write(data)
                    staged.flush()
                    os.fsync(staged.fileno())
                sync_directory(self._chunks)
                self._publish(key)

            return ChunkReceipt(
                **headers.model_dump(), **self.identity.model_dump(), chunk_id=chunk_id, durable=True,
            )

    def _read_checked(self, key: str, length: int, digest: str) -> bytes:
        state, data = self._check_file(self._path(key), length, digest)
        self._set_state(key, state)
        if state == "missing":
            raise APIError(404, "chunk_missing", "Chunk file is missing")
        if state == "corrupt":
            raise APIError(409, "chunk_corrupt", "Chunk file failed length or checksum validation")
        return data

    def get(self, chunk_id: UUID, headers: ChunkReadHeaders) -> bytes:
        key = str(chunk_id)
        with self._access():
            row = self._placement(key)
            if row is None or row["state"] in {"pending_delete", "retired"}:
                raise APIError(404, "chunk_missing", "Chunk is not stored")
            if (row["generation"], row["length_bytes"], row["expected_sha256"]) != (
                str(headers.generation), headers.length_bytes, headers.sha256,
            ):
                raise APIError(409, "identity_conflict", "Read expectations do not match the catalog")
            if row["state"] == "pending_put":
                raise APIError(503, "storage_unavailable", "Chunk publication is incomplete")
            return self._read_checked(key, headers.length_bytes, headers.sha256)

    def delete(self, chunk_id: UUID, headers: MutationHeaders) -> None:
        key = str(chunk_id)
        with self._access():
            row = self._placement(key)
            retry = self._check_mutation(key, headers, "delete", row)
            if not retry:
                self._begin(key, headers, "delete", None, None)
            if not retry or row["state"] != "retired":
                self._retire(key)

    def tombstone(self, object_id: UUID, headers: TombstoneRequest) -> TombstoneReceipt:
        key = str(object_id), str(headers.generation)
        operation_id, sequence = str(headers.operation_id), headers.operation_sequence
        operation = (*key, operation_id, headers.controller_epoch, sequence)
        with self._access():
            previous = self._tombstone_operations(operation_id, sequence)
            if previous and (len(previous) != 1 or tuple(previous[0]) != operation):
                raise APIError(409, "operation_conflict", "Tombstone identity was already used")
            fence = self._db.execute(
                "SELECT * FROM object_tombstones WHERE object_id = ? AND generation = ?", key,
            ).fetchone()
            if not previous:
                if self._db.execute(
                    "SELECT 1 FROM operations WHERE operation_id = ? OR operation_sequence = ?",
                    (operation_id, sequence),
                ).fetchone() is not None:
                    raise APIError(409, "operation_conflict", "Mutation identity was already used")
                if fence is not None and (
                    headers.controller_epoch <= fence["controller_epoch"] or sequence <= fence["operation_sequence"]
                ):
                    raise APIError(409, "operation_conflict", "Tombstone reissue must advance the epoch and sequence")
                # A terminal intent remains authoritative across controller boots.
                # A worker that was offline may see it for the first time after rejoin.
                with self._db:
                    self._advance_epoch(headers.controller_epoch)
                    if fence is not None:
                        self._db.execute(
                            """INSERT INTO tombstone_history
                               SELECT object_id, generation, operation_id, controller_epoch, operation_sequence
                               FROM object_tombstones WHERE object_id = ? AND generation = ?""",
                            key,
                        )
                    self._db.execute(
                        """INSERT INTO object_tombstones
                           (object_id, generation, operation_id, controller_epoch, operation_sequence)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(object_id, generation) DO UPDATE SET
                               operation_id = excluded.operation_id,
                               controller_epoch = excluded.controller_epoch,
                               operation_sequence = excluded.operation_sequence""",
                        operation,
                    )
                    if fence is None:
                        self._db.execute(
                            """UPDATE placements SET state = 'retired'
                               WHERE object_id = ? AND generation = ?""",
                            key,
                        )
            if fence is None or not fence["cleanup_complete"]:
                self._finish_tombstone(*key)
            return TombstoneReceipt(
                object_id=object_id, **headers.model_dump(), **self.identity.model_dump(),
                cleanup_complete=True,
            )

    def inventory(self) -> WorkerInventory:
        with self._access():
            rows = self._db.execute(
                """SELECT * FROM placements WHERE state NOT IN ('pending_delete', 'retired')
                   ORDER BY chunk_id""",
            )
            chunks = []
            for row in rows:
                state = row["state"]
                if state == "pending_put":
                    state = "suspect"
                else:
                    try:
                        if self._path(row["chunk_id"]).stat().st_size != row["length_bytes"]:
                            state = "corrupt"
                    except FileNotFoundError:
                        state = "missing"
                chunks.append(InventoryChunk(
                    object_id=row["object_id"], generation=row["generation"], chunk_id=row["chunk_id"],
                    length_bytes=row["length_bytes"], expected_sha256=row["expected_sha256"],
                    state=state, operation_sequence=row["operation_sequence"],
                    last_verified_at=row["last_verified_at"],
                ))
            return WorkerInventory(**self.identity.model_dump(), chunks=chunks)
