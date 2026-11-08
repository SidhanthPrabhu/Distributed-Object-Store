"""Short SQLite transactions; no network calls or worker-derived expected hashes.

This is a persistence foundation, not placement/reconciliation orchestration.
The future coordinator must choose eligible workers before creating PUT intents.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import UUID, uuid4

from common.errors import APIError
from common.models import (
    ChunkMetadata, ChunkReceipt, Checksum, ObjectMetadata, WorkerHeartbeat, WorkerRegistration,
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
            if version not in (0, 1):
                raise RuntimeError("Unsupported controller schema version")
            self._db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA
                                   + "\nPRAGMA user_version = 1;\nCOMMIT;")
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
                acknowledged.append(report.report_id)
            db.execute("""UPDATE workers SET free_bytes=?, last_heartbeat_at=?,
                state='RECONCILING' WHERE worker_id=?""",
                       (heartbeat.free_bytes, utc_now(), heartbeat.worker_id))
        # Only return acknowledgements after COMMIT has succeeded.
        return acknowledged

    def _new_operation(self, db, obj, kind, **extra) -> dict:
        db.execute("UPDATE counters SET sequence = sequence + 1 WHERE singleton = 1")
        sequence = db.execute("SELECT sequence FROM counters").fetchone()[0]
        op = dict(operation_id=str(uuid4()), operation_sequence=sequence,
                  controller_epoch=self.epoch, object_id=obj["object_id"],
                  generation=obj["generation"], kind=kind, **extra)
        columns = ",".join(op)
        placeholders = ",".join("?" for _ in op)
        db.execute(f"INSERT INTO operations ({columns}) VALUES ({placeholders})", tuple(op.values()))
        return {**op, "state": "PENDING"}

    def create_put_intent(self, object_id: UUID, chunk_id: UUID, worker_id: str) -> dict:
        with self.transaction() as db:
            obj = self._object(db, object_id)
            if obj["state"] not in {"UPLOADING", "ACTIVE"}:
                raise conflict("object_not_writable", "Object is no longer writable")
            chunk = db.execute("SELECT * FROM chunks WHERE chunk_id = ? AND object_id = ?",
                               (str(chunk_id), str(object_id))).fetchone()
            worker = db.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
            if chunk is None or worker is None:
                raise conflict("identity_conflict", "Unknown chunk or worker")
            return self._new_operation(
                db, obj, "PUT", chunk_id=str(chunk_id), worker_id=worker_id,
                storage_id=worker["storage_id"], boot_id=worker["boot_id"],
                length_bytes=chunk["size_bytes"], sha256=chunk["sha256"],
            )

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
            db.execute("INSERT OR REPLACE INTO replicas VALUES (?,?,?,?)", (
                str(receipt.chunk_id), receipt.worker_id, str(receipt.operation_id),
                receipt.model_dump_json(),
            ))
            db.execute("UPDATE operations SET state = 'DONE' WHERE operation_id = ?",
                       (str(receipt.operation_id),))

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

    def begin_deletion(self, object_id: UUID, worker_ids: tuple[str, ...]) -> dict:
        """Persist terminal intent and all-worker cleanup; no storage effects here."""
        if set(worker_ids) != {"worker1", "worker2", "worker3", "worker4"}:
            raise ValueError("Terminal cleanup must cover all four configured workers")
        with self.transaction() as db:
            obj = self._object(db, object_id)
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
