"""Short SQLite transactions; no network calls or worker-derived expected hashes.

The transfer coordinator chooses eligible workers before creating PUT intents.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from uuid import UUID, uuid4

from common.errors import APIError
from common.models import (
    ChunkMetadata, ChunkReceipt, Checksum, ObjectMetadata, ObjectSummary, TombstoneReceipt,
    WorkerHeartbeat, WorkerRegistration,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    epoch INTEGER NOT NULL, sequence INTEGER NOT NULL
);
INSERT OR IGNORE INTO counters VALUES (1, 0, 0);
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY, storage_id TEXT NOT NULL UNIQUE,
    boot_id TEXT NOT NULL, last_heartbeat_at TEXT, free_bytes INTEGER,
    state TEXT NOT NULL DEFAULT 'RECONCILING'
);
CREATE TABLE IF NOT EXISTS worker_boots (
    worker_id TEXT NOT NULL REFERENCES workers(worker_id), boot_id TEXT NOT NULL,
    PRIMARY KEY (worker_id, boot_id)
);
CREATE TABLE IF NOT EXISTS objects (
    object_id TEXT PRIMARY KEY, generation TEXT NOT NULL,
    name TEXT NOT NULL, size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    content_type TEXT NOT NULL, created_at TEXT NOT NULL, modified_at TEXT NOT NULL,
    sha256 TEXT NOT NULL, chunk_size_bytes INTEGER NOT NULL,
    metadata_revision INTEGER NOT NULL, custom_metadata TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('UPLOADING','ACTIVE','DELETING','DELETED','ABORTED')),
    UNIQUE (object_id, generation)
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY, object_id TEXT NOT NULL REFERENCES objects(object_id),
    ordinal INTEGER NOT NULL, size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
    sha256 TEXT NOT NULL, UNIQUE (object_id, ordinal)
);
CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY, operation_sequence INTEGER NOT NULL UNIQUE,
    controller_epoch INTEGER NOT NULL,
    object_id TEXT NOT NULL REFERENCES objects(object_id), generation TEXT NOT NULL,
    chunk_id TEXT REFERENCES chunks(chunk_id), worker_id TEXT REFERENCES workers(worker_id),
    storage_id TEXT, boot_id TEXT, length_bytes INTEGER, sha256 TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('PUT','TOMBSTONE')),
    state TEXT NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING','DONE'))
);
CREATE TABLE IF NOT EXISTS replicas (
    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
    worker_id TEXT NOT NULL REFERENCES workers(worker_id),
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    receipt TEXT NOT NULL, PRIMARY KEY (chunk_id, worker_id)
);
CREATE TABLE IF NOT EXISTS tombstones (
    object_id TEXT PRIMARY KEY REFERENCES objects(object_id), generation TEXT NOT NULL,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cleanup (
    object_id TEXT NOT NULL REFERENCES tombstones(object_id), worker_id TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0,1)),
    PRIMARY KEY (object_id, worker_id)
);
CREATE TABLE IF NOT EXISTS integrity_reports (
    report_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL REFERENCES workers(worker_id),
    storage_id TEXT NOT NULL, chunk_id TEXT NOT NULL, report TEXT NOT NULL,
    pending INTEGER NOT NULL DEFAULT 1 CHECK (pending IN (0,1))
);
CREATE INDEX IF NOT EXISTS pending_operations ON operations(state);
CREATE INDEX IF NOT EXISTS object_chunks ON chunks(object_id, ordinal);
CREATE TABLE IF NOT EXISTS repair_writes (
    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
    worker_id TEXT NOT NULL REFERENCES workers(worker_id),
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    PRIMARY KEY (chunk_id, worker_id)
);
CREATE TABLE IF NOT EXISTS replica_retirements (
    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
    worker_id TEXT NOT NULL REFERENCES workers(worker_id),
    placement_id TEXT NOT NULL REFERENCES operations(operation_id),
    operation_id TEXT NOT NULL UNIQUE, operation_sequence INTEGER NOT NULL UNIQUE,
    controller_epoch INTEGER NOT NULL, storage_id TEXT NOT NULL, boot_id TEXT NOT NULL,
    PRIMARY KEY (chunk_id, worker_id)
);
CREATE TABLE IF NOT EXISTS placement_faults (
    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
    worker_id TEXT NOT NULL REFERENCES workers(worker_id), reason TEXT NOT NULL,
    PRIMARY KEY (chunk_id, worker_id)
);
CREATE TABLE IF NOT EXISTS placement_rotation (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1), cursor INTEGER NOT NULL
);
INSERT OR IGNORE INTO placement_rotation VALUES (1, 0);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def conflict(code: str, message: str) -> APIError:
    return APIError(409, code, message)


class MetadataStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self.closed = False
        try:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA busy_timeout = 5000")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise RuntimeError("Unsupported controller schema version")
            self._db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA
                                   + "\nPRAGMA user_version = 2;\nCOMMIT;")
            with self.transaction() as db:
                db.execute("UPDATE counters SET epoch = epoch + 1 WHERE singleton = 1")
                self.epoch = db.execute("SELECT epoch FROM counters").fetchone()[0]
                # A persisted timestamp never proves reachability in a new process.
                db.execute("UPDATE workers SET state = 'RECONCILING'")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self._lock:
            if not self.closed:
                self._db.close()
                self.closed = True

    @contextmanager
    def transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def ping(self) -> None:
        with self._lock:
            self._db.execute("SELECT 1").fetchone()

    def _object(self, db, object_id):
        row = db.execute("SELECT * FROM objects WHERE object_id = ?", (str(object_id),)).fetchone()
        if row is None:
            raise APIError(404, "object_not_found", "Object not found")
        return row

    def create_upload(self, manifest: ObjectMetadata) -> None:
        if any(c.locations for c in manifest.chunks):
            raise ValueError("New uploads cannot assert existing placements")
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM objects WHERE object_id = ?",
                          (str(manifest.object_id),)).fetchone():
                raise conflict("identity_conflict", "Object identity already exists")
            if any(db.execute("SELECT 1 FROM chunks WHERE chunk_id = ?", (str(c.chunk_id),)).fetchone()
                   for c in manifest.chunks):
                raise conflict("identity_conflict", "Chunk identity already exists")
            db.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?,?,?,?,?, 'UPLOADING')", (
                str(manifest.object_id), str(manifest.generation), manifest.name,
                manifest.size_bytes, manifest.content_type, manifest.created_at.isoformat(),
                manifest.modified_at.isoformat(), manifest.checksum.value,
                manifest.chunk_size_bytes, manifest.metadata_revision,
                json.dumps(manifest.custom_metadata),
            ))
            db.executemany("INSERT INTO chunks VALUES (?,?,?,?,?)", [
                (str(c.chunk_id), str(manifest.object_id), c.ordinal, c.size_bytes, c.sha256)
                for c in manifest.chunks
            ])

    def get_manifest(self, object_id: UUID, *, include_hidden: bool = False) -> ObjectMetadata:
        # One transaction makes the object/chunk/replica snapshot consistent.
        with self.transaction() as db:
            obj = self._object(db, object_id)
            if not include_hidden and obj["state"] != "ACTIVE":
                raise APIError(404, "object_not_found", "Object not found")
            return self._manifest(db, obj)

    def _manifest(self, db, obj) -> ObjectMetadata:
        object_id = obj["object_id"]
        chunks = db.execute("SELECT * FROM chunks WHERE object_id = ? ORDER BY ordinal",
                            (str(object_id),)).fetchall()
        return ObjectMetadata(
            object_id=obj["object_id"], generation=obj["generation"], name=obj["name"],
            size_bytes=obj["size_bytes"], content_type=obj["content_type"],
            created_at=obj["created_at"], modified_at=obj["modified_at"],
            checksum=Checksum(value=obj["sha256"]), chunk_size_bytes=obj["chunk_size_bytes"],
            chunk_count=len(chunks), metadata_revision=obj["metadata_revision"],
            custom_metadata=json.loads(obj["custom_metadata"]),
            chunks=[ChunkMetadata(
                chunk_id=c["chunk_id"], ordinal=c["ordinal"], size_bytes=c["size_bytes"],
                sha256=c["sha256"], locations=[r[0] for r in db.execute(
                    "SELECT worker_id FROM replicas WHERE chunk_id = ? ORDER BY worker_id",
                    (c["chunk_id"],),
                )],
            ) for c in chunks],
        )

    def patch_metadata(self, object_id: UUID, updates: dict) -> ObjectMetadata:
        """Merge a custom-key map and return its own committed revision snapshot."""
        with self.transaction() as db:
            obj = self._object(db, object_id)
            if obj["state"] != "ACTIVE":
                raise APIError(404, "object_not_found", "Object not found")
            if obj["metadata_revision"] == 2**63 - 1:
                raise conflict("metadata_revision_exhausted", "Metadata revision limit reached")
            custom = json.loads(obj["custom_metadata"])
            custom.update(updates)
            # Keep modification time advancing even if the wall clock moves back.
            modified = max(datetime.now(timezone.utc),
                           datetime.fromisoformat(obj["modified_at"]) + timedelta(microseconds=1))
            db.execute("""UPDATE objects SET custom_metadata=?, modified_at=?,
                metadata_revision=metadata_revision+1 WHERE object_id=?""",
                (json.dumps(custom, allow_nan=False), modified.isoformat(), str(object_id)))
            return self._manifest(db, self._object(db, object_id))

    def list_objects(self) -> list[ObjectSummary]:
        with self.transaction() as db:
            return [ObjectSummary(**dict(row)) for row in db.execute("""SELECT
                object_id, name, size_bytes, content_type FROM objects
                WHERE state='ACTIVE' ORDER BY created_at, object_id""")]

    def object_state(self, object_id: UUID) -> str:
        with self.transaction() as db:
            return self._object(db, object_id)["state"]

    def register(self, identity: WorkerRegistration) -> None:
        with self.transaction() as db:
            worker = db.execute("SELECT * FROM workers WHERE worker_id = ?",
                                (identity.worker_id,)).fetchone()
            owner = db.execute("SELECT worker_id FROM workers WHERE storage_id = ?",
                               (str(identity.storage_id),)).fetchone()
            if ((worker and worker["storage_id"] != str(identity.storage_id))
                    or (owner and owner[0] != identity.worker_id)):
                raise conflict("identity_conflict", "Worker/storage identity is already bound")
            if worker and worker["boot_id"] != str(identity.boot_id) and db.execute(
                "SELECT 1 FROM worker_boots WHERE worker_id = ? AND boot_id = ?",
                (identity.worker_id, str(identity.boot_id)),
            ).fetchone():
                raise conflict("registration_required", "Retired worker boot cannot register again")
            db.execute("""INSERT INTO workers(worker_id,storage_id,boot_id) VALUES (?,?,?)
                ON CONFLICT(worker_id) DO UPDATE SET boot_id=excluded.boot_id,
                free_bytes=NULL, last_heartbeat_at=NULL, state='RECONCILING'""",
                       (identity.worker_id, str(identity.storage_id), str(identity.boot_id)))
            db.execute("INSERT OR IGNORE INTO worker_boots VALUES (?,?)",
                       (identity.worker_id, str(identity.boot_id)))

    def workers(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute("SELECT * FROM workers ORDER BY worker_id")]

    def accept_heartbeat(self, heartbeat: WorkerHeartbeat) -> list[UUID]:
        with self.transaction() as db:
            worker = db.execute("SELECT * FROM workers WHERE worker_id=?",
                                (heartbeat.worker_id,)).fetchone()
            if (worker is None or worker["storage_id"] != str(heartbeat.storage_id)
                    or worker["boot_id"] != str(heartbeat.boot_id)):
                raise conflict("registration_required", "Register this worker boot first")
            acknowledged = []
            for report in heartbeat.integrity_reports:
                previous = db.execute("SELECT * FROM integrity_reports WHERE report_id=?",
                                      (str(report.report_id),)).fetchone()
                payload = report.model_dump_json()
                if previous and (previous["worker_id"] != heartbeat.worker_id
                                 or previous["storage_id"] != str(heartbeat.storage_id)
                                 or previous["report"] != payload):
                    raise conflict("report_conflict", "Report ID is bound to a different fault")
                db.execute("INSERT OR IGNORE INTO integrity_reports VALUES (?,?,?,?,?,1)", (
                    str(report.report_id), heartbeat.worker_id, str(heartbeat.storage_id),
                    str(report.chunk_id), payload,
                ))
                if not previous:
                    db.execute("""UPDATE integrity_reports SET pending=0
                        WHERE worker_id=? AND chunk_id=? AND report_id<>?""",
                        (heartbeat.worker_id, str(report.chunk_id), str(report.report_id)))
                known = db.execute("""SELECT 1 FROM chunks JOIN objects USING(object_id)
                    WHERE chunk_id=? AND object_id=? AND generation=? AND objects.state IN ('UPLOADING','ACTIVE')""",
                    (str(report.chunk_id), str(report.object_id), str(report.generation))).fetchone()
                if known and not previous:
                    db.execute("INSERT OR REPLACE INTO placement_faults VALUES (?,?,?)",
                               (str(report.chunk_id), heartbeat.worker_id, report.outcome))
                acknowledged.append(report.report_id)
            db.execute("""UPDATE workers SET free_bytes=?, last_heartbeat_at=?,
                state='RECONCILING' WHERE worker_id=?""",
                       (heartbeat.free_bytes, utc_now(), heartbeat.worker_id))
        # Only return acknowledgements after COMMIT has succeeded.
        return acknowledged

    def _next_sequence(self, db) -> int:
        db.execute("UPDATE counters SET sequence = sequence + 1 WHERE singleton = 1")
        return db.execute("SELECT sequence FROM counters").fetchone()[0]

    def _new_operation(self, db, obj, kind, **extra) -> dict:
        op = dict(operation_id=str(uuid4()), operation_sequence=self._next_sequence(db),
                  controller_epoch=self.epoch, object_id=obj["object_id"],
                  generation=obj["generation"], kind=kind, **extra)
        columns = ",".join(op)
        placeholders = ",".join("?" for _ in op)
        db.execute(f"INSERT INTO operations ({columns}) VALUES ({placeholders})", tuple(op.values()))
        return {**op, "state": "PENDING"}

    def create_put_intent(self, object_id: UUID, chunk_id: UUID, worker_id: str, *,
                          repair: bool = False, renew: bool = False) -> dict:
        with self.transaction() as db:
            obj = self._object(db, object_id)
            if obj["state"] not in {"UPLOADING", "ACTIVE"}:
                raise conflict("object_not_writable", "Object is no longer writable")
            chunk = db.execute("SELECT * FROM chunks WHERE chunk_id = ? AND object_id = ?",
                               (str(chunk_id), str(object_id))).fetchone()
            worker = db.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
            if chunk is None or worker is None:
                raise conflict("identity_conflict", "Unknown chunk or worker")
            old = None
            if repair:
                if obj["state"] != "ACTIVE":
                    raise conflict("stale_operation", "Repair requires an active object")
                old = db.execute("""SELECT operations.* FROM repair_writes
                    JOIN operations USING(operation_id) WHERE repair_writes.chunk_id=?
                    AND repair_writes.worker_id=?""", (str(chunk_id), worker_id)).fetchone()
                if (old and not renew and old["controller_epoch"] == self.epoch
                        and old["storage_id"] == worker["storage_id"] and old["boot_id"] == worker["boot_id"]):
                    return dict(old)
            op = self._new_operation(
                db, obj, "PUT", chunk_id=str(chunk_id), worker_id=worker_id,
                storage_id=worker["storage_id"], boot_id=worker["boot_id"],
                length_bytes=chunk["size_bytes"], sha256=chunk["sha256"],
            )
            if repair:
                if old:
                    db.execute("UPDATE operations SET state='DONE' WHERE operation_id=?", (old["operation_id"],))
                db.execute("INSERT OR REPLACE INTO repair_writes VALUES (?,?,?)",
                           (str(chunk_id), worker_id, op["operation_id"]))
            return op

    def record_receipt(self, receipt: ChunkReceipt) -> None:
        with self.transaction() as db:
            op = db.execute("SELECT * FROM operations WHERE operation_id = ?",
                            (str(receipt.operation_id),)).fetchone()
            values = receipt.model_dump(mode="json", exclude={"durable"})
            if op is None or op["kind"] != "PUT" or any(op[k] != v for k, v in values.items()):
                raise conflict("receipt_mismatch", "Receipt does not match the durable PUT intent")
            obj = self._object(db, receipt.object_id)
            worker = db.execute("SELECT * FROM workers WHERE worker_id = ?",
                                (receipt.worker_id,)).fetchone()
            if (obj["state"] not in {"UPLOADING", "ACTIVE"} or op["controller_epoch"] != self.epoch
                    or worker["storage_id"] != str(receipt.storage_id)
                    or worker["boot_id"] != str(receipt.boot_id)):
                raise conflict("stale_operation", "Object, controller, or worker has changed")
            current = db.execute("""SELECT operations.operation_sequence FROM replicas
                JOIN operations USING(operation_id) WHERE replicas.chunk_id=? AND replicas.worker_id=?""",
                                 (str(receipt.chunk_id), receipt.worker_id)).fetchone()
            if current and current[0] > receipt.operation_sequence:
                raise conflict("stale_operation", "A newer placement already exists")
            if op["state"] == "DONE" and (not current or current[0] != receipt.operation_sequence):
                raise conflict("stale_operation", "The PUT intent has been superseded")
            db.execute("INSERT OR REPLACE INTO replicas VALUES (?,?,?,?)", (
                str(receipt.chunk_id), receipt.worker_id, str(receipt.operation_id),
                receipt.model_dump_json(),
            ))
            db.execute("UPDATE operations SET state = 'DONE' WHERE operation_id = ?",
                       (str(receipt.operation_id),))
            db.execute("DELETE FROM placement_faults WHERE chunk_id=? AND worker_id=?",
                       (str(receipt.chunk_id), receipt.worker_id))
            db.execute("DELETE FROM repair_writes WHERE operation_id=?", (str(receipt.operation_id),))
            db.execute("DELETE FROM replica_retirements WHERE chunk_id=? AND worker_id=?",
                       (str(receipt.chunk_id), receipt.worker_id))
            db.execute("UPDATE integrity_reports SET pending=0 WHERE chunk_id=? AND worker_id=?",
                       (str(receipt.chunk_id), receipt.worker_id))

    def publish_upload(self, object_id: UUID) -> None:
        """Local visibility gate; caller still owns fresh worker eligibility checks."""
        with self.transaction() as db:
            obj = self._object(db, object_id)
            if obj["state"] != "UPLOADING":
                raise conflict("object_not_writable", "Object is not an incomplete upload")
            deficient = db.execute("""SELECT chunks.chunk_id FROM chunks
                LEFT JOIN replicas USING(chunk_id)
                LEFT JOIN operations ON replicas.operation_id=operations.operation_id
                LEFT JOIN workers ON replicas.worker_id=workers.worker_id
                WHERE chunks.object_id=? GROUP BY chunks.chunk_id
                HAVING COUNT(replicas.worker_id) != 2 OR SUM(CASE WHEN
                    operations.controller_epoch=? AND operations.boot_id=workers.boot_id
                    AND operations.storage_id=workers.storage_id THEN 1 ELSE 0 END) != 2""",
                                  (str(object_id), self.epoch)).fetchone()
            if deficient:
                raise conflict("incomplete_upload", "Every chunk needs two durable distinct placements")
            db.execute("UPDATE objects SET state = 'ACTIVE' WHERE object_id = ?", (str(object_id),))

    def begin_deletion(self, object_id: UUID, worker_ids: tuple[str, ...], *,
                       public: bool = False) -> dict:
        """Persist terminal intent and all-worker cleanup; no storage effects here."""
        if set(worker_ids) != {"worker1", "worker2", "worker3", "worker4"}:
            raise ValueError("Terminal cleanup must cover all four configured workers")
        with self.transaction() as db:
            obj = self._object(db, object_id)
            if public and obj["state"] not in {"ACTIVE", "DELETING"}:
                raise APIError(404, "object_not_found", "Object not found")
            existing = db.execute("""SELECT operations.* FROM tombstones JOIN operations
                USING(operation_id) WHERE tombstones.object_id=?""", (str(object_id),)).fetchone()
            if existing:
                return dict(existing)
            op = self._new_operation(db, obj, "TOMBSTONE")
            db.execute("INSERT INTO tombstones VALUES (?,?,?,?)", (
                str(object_id), obj["generation"], op["operation_id"], utc_now(),
            ))
            db.executemany("INSERT INTO cleanup(object_id, worker_id) VALUES (?,?)",
                           [(str(object_id), worker_id) for worker_id in worker_ids])
            db.execute("UPDATE objects SET state='DELETING' WHERE object_id=?", (str(object_id),))
            for table in ("repair_writes", "replica_retirements", "placement_faults"):
                db.execute(f"""DELETE FROM {table} WHERE chunk_id IN
                    (SELECT chunk_id FROM chunks WHERE object_id=?)""", (str(object_id),))
            db.execute("""UPDATE integrity_reports SET pending=0 WHERE chunk_id IN
                (SELECT chunk_id FROM chunks WHERE object_id=?)""", (str(object_id),))
            return op

    def pending_operations(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute(
                "SELECT * FROM operations WHERE state='PENDING' ORDER BY operation_sequence"
            )]

    def pending_cleanup(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute(
                "SELECT * FROM cleanup WHERE acknowledged=0 ORDER BY object_id, worker_id"
            )]

    def pending_reports(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute(
                "SELECT * FROM integrity_reports WHERE pending=1 ORDER BY report_id"
            )]

    def abort_interrupted_uploads(self, worker_ids: tuple[str, ...]) -> None:
        with self._lock:
            ids = [r[0] for r in self._db.execute(
                "SELECT object_id FROM objects WHERE state='UPLOADING'")]
        for object_id in ids:
            self.begin_deletion(UUID(object_id), worker_ids)

    def cleanup_intent(self, object_id: UUID) -> dict:
        """Re-fence old epochs once; retries in this epoch keep the same identity."""
        with self.transaction() as db:
            obj = self._object(db, object_id)
            op = db.execute("""SELECT operations.* FROM tombstones JOIN operations
                USING(operation_id) WHERE tombstones.object_id=?""", (str(object_id),)).fetchone()
            if op is None:
                raise conflict("identity_conflict", "No terminal cleanup intent")
            if op["controller_epoch"] != self.epoch:
                db.execute("UPDATE operations SET state='DONE' WHERE operation_id=?",
                           (op["operation_id"],))
                op = self._new_operation(db, obj, "TOMBSTONE")
                db.execute("UPDATE tombstones SET operation_id=? WHERE object_id=?",
                           (op["operation_id"], str(object_id)))
            return dict(op)

    def acknowledge_cleanup(self, receipt: TombstoneReceipt) -> None:
        with self.transaction() as db:
            op = db.execute("""SELECT operations.* FROM tombstones JOIN operations
                USING(operation_id) WHERE tombstones.object_id=?""",
                            (str(receipt.object_id),)).fetchone()
            worker = db.execute("SELECT * FROM workers WHERE worker_id=?",
                                (receipt.worker_id,)).fetchone()
            values = receipt.model_dump(mode="json", exclude={
                "worker_id", "storage_id", "boot_id", "cleanup_complete"})
            if (op is None or worker is None or any(op[k] != v for k, v in values.items())
                    or worker["storage_id"] != str(receipt.storage_id)
                    or worker["boot_id"] != str(receipt.boot_id)
                    or op["controller_epoch"] != self.epoch):
                raise conflict("receipt_mismatch", "Cleanup receipt does not match current intent/boot")
            db.execute("UPDATE cleanup SET acknowledged=1 WHERE object_id=? AND worker_id=?",
                       (str(receipt.object_id), receipt.worker_id))
            if not db.execute("SELECT 1 FROM cleanup WHERE object_id=? AND acknowledged=0",
                              (str(receipt.object_id),)).fetchone():
                db.execute("UPDATE objects SET state='DELETED' WHERE object_id=?",
                           (str(receipt.object_id),))
                db.execute("UPDATE operations SET state='DONE' WHERE object_id=?",
                           (str(receipt.object_id),))
                db.execute("""DELETE FROM replicas WHERE chunk_id IN
                    (SELECT chunk_id FROM chunks WHERE object_id=?)""",
                           (str(receipt.object_id),))

    def worker_placements(self, worker_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute("""SELECT chunks.*, objects.generation,
                objects.state, operations.operation_sequence FROM replicas
                JOIN chunks USING(chunk_id) JOIN objects USING(object_id)
                JOIN operations ON operations.operation_id=replicas.operation_id
                WHERE replicas.worker_id=?""", (worker_id,))]

    def inventory_authority(self, chunk_id: UUID, worker_id: str,
                            sequence: int | None = None) -> dict | None:
        with self._lock:
            row = self._db.execute("""SELECT chunks.*, objects.generation, objects.state,
                operations.operation_sequence, operations.operation_id,
                EXISTS (SELECT 1 FROM repair_writes WHERE repair_writes.chunk_id=chunks.chunk_id
                    AND repair_writes.worker_id=operations.worker_id)
                    AS pending_repair
                FROM chunks JOIN objects USING(object_id)
                JOIN operations USING(chunk_id) WHERE chunks.chunk_id=?
                AND operations.worker_id=? AND operations.kind='PUT'
                AND (? IS NULL OR operations.operation_sequence=?)
                ORDER BY operations.operation_sequence DESC LIMIT 1""",
                (str(chunk_id), worker_id, sequence, sequence)).fetchone()
            return dict(row) if row else None

    def active_chunk_placements(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute("""SELECT
                chunks.*, objects.generation, replicas.worker_id, operations.operation_sequence,
                placement_faults.reason, replica_retirements.operation_id AS retirement_id
                FROM chunks JOIN objects USING(object_id)
                LEFT JOIN replicas USING(chunk_id)
                LEFT JOIN operations ON replicas.operation_id=operations.operation_id
                LEFT JOIN placement_faults ON placement_faults.chunk_id=chunks.chunk_id
                    AND placement_faults.worker_id=replicas.worker_id
                LEFT JOIN replica_retirements ON replica_retirements.chunk_id=chunks.chunk_id
                    AND replica_retirements.worker_id=replicas.worker_id
                WHERE objects.state='ACTIVE' ORDER BY chunks.chunk_id, replicas.worker_id""")]

    def begin_retirement(self, chunk: dict, worker_id: str) -> dict:
        with self.transaction() as db:
            obj = self._object(db, UUID(chunk["object_id"]))
            placement = db.execute("""SELECT operation_id FROM replicas
                 WHERE chunk_id=? AND worker_id=?""", (chunk["chunk_id"], worker_id)).fetchone()
            worker = db.execute("SELECT * FROM workers WHERE worker_id=?", (worker_id,)).fetchone()
            if obj["state"] != "ACTIVE" or placement is None or worker is None:
                raise conflict("stale_operation", "Placement is no longer eligible for retirement")
            old = db.execute("""SELECT * FROM replica_retirements WHERE chunk_id=? AND worker_id=?""",
                             (chunk["chunk_id"], worker_id)).fetchone()
            if (old and old["placement_id"] == placement[0] and old["controller_epoch"] == self.epoch
                    and old["storage_id"] == worker["storage_id"] and old["boot_id"] == worker["boot_id"]):
                op = dict(old)
            else:
                op = dict(chunk_id=chunk["chunk_id"], worker_id=worker_id, placement_id=placement[0],
                          operation_id=str(uuid4()), operation_sequence=self._next_sequence(db),
                          controller_epoch=self.epoch, storage_id=worker["storage_id"], boot_id=worker["boot_id"])
                db.execute("INSERT OR REPLACE INTO replica_retirements VALUES (?,?,?,?,?,?,?,?)",
                           tuple(op.values()))
            return {**op, "object_id": obj["object_id"], "generation": obj["generation"]}

    def finish_retirement(self, op: dict) -> None:
        key = (op["chunk_id"], op["worker_id"])
        with self.transaction() as db:
            intent = db.execute("SELECT * FROM replica_retirements WHERE chunk_id=? AND worker_id=?",
                                key).fetchone()
            obj = self._object(db, UUID(op["object_id"]))
            if intent is None or intent["operation_id"] != op["operation_id"] or obj["state"] != "ACTIVE":
                raise conflict("stale_operation", "Retirement was superseded")
            db.execute("DELETE FROM replicas WHERE chunk_id=? AND worker_id=? AND operation_id=?",
                       (*key, op["placement_id"]))
            db.execute("""UPDATE operations SET state='DONE' WHERE operation_id IN
                (SELECT operation_id FROM repair_writes WHERE chunk_id=? AND worker_id=?)""", key)
            db.execute("DELETE FROM repair_writes WHERE chunk_id=? AND worker_id=?", key)
            db.execute("DELETE FROM replica_retirements WHERE chunk_id=? AND worker_id=?", key)
            db.execute("DELETE FROM placement_faults WHERE chunk_id=? AND worker_id=?", key)
            db.execute("UPDATE integrity_reports SET pending=0 WHERE chunk_id=? AND worker_id=?", key)

    def placement_fault(self, chunk_id: UUID, worker_id: str, reason: str) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO placement_faults VALUES (?,?,?)",
                       (str(chunk_id), worker_id, reason))

    def faulty_placements(self) -> set[tuple[str, str]]:
        with self._lock:
            return {(r[0], r[1]) for r in self._db.execute(
                "SELECT chunk_id,worker_id FROM placement_faults")}

    def confirm_placements(self, worker_id: str, chunk_ids: list[str]) -> None:
        """Clear only after fresh authoritative reads and a current registry token."""
        with self.transaction() as db:
            db.executemany("DELETE FROM placement_faults WHERE worker_id=? AND chunk_id=?",
                           [(worker_id, chunk_id) for chunk_id in chunk_ids])
            db.executemany("UPDATE integrity_reports SET pending=0 WHERE worker_id=? AND chunk_id=?",
                           [(worker_id, chunk_id) for chunk_id in chunk_ids])

    def next_placement_cursor(self) -> int:
        with self.transaction() as db:
            cursor = db.execute("SELECT cursor FROM placement_rotation").fetchone()[0]
            db.execute("UPDATE placement_rotation SET cursor=?", ((cursor + 1) % 4,))
            return cursor
