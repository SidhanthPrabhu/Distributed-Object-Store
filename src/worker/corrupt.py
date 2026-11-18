"""Flip a byte in one chunk of a disposable local worker for the course demo."""

import argparse
import json
import os
import sqlite3
from contextlib import closing
from hashlib import sha256
from uuid import UUID

from worker.config import WorkerConfig


def corrupt_chunk(config: WorkerConfig, object_id: UUID, chunk_id: UUID) -> dict:
    catalog = (config.data_dir / "catalog.sqlite3").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(catalog, uri=True)) as db:
        worker = db.execute("SELECT worker_id FROM worker WHERE singleton=1").fetchone()
        row = db.execute("""SELECT length_bytes, expected_sha256 FROM placements
            WHERE object_id=? AND chunk_id=? AND state='valid'""",
                         (str(object_id), str(chunk_id))).fetchone()
    if worker != (config.worker_id,) or row is None:
        raise ValueError("The selected object and chunk are not a valid local placement")
    length, expected = row
    if not 0 < length <= config.chunk_size_bytes:
        raise ValueError("Chunk size is outside the worker limit")

    path = config.data_dir / "chunks" / f"{chunk_id}.bin"
    with os.fdopen(os.open(path, os.O_RDWR | os.O_NOFOLLOW), "r+b") as file:
        data = file.read(length + 1)
        if len(data) != length or sha256(data).hexdigest() != expected:
            raise ValueError("The selected file already fails its trusted checksum")
        file.seek(0)
        changed = bytes([data[0] ^ 1])
        file.write(changed)
        file.flush()
        os.fsync(file.fileno())
    return {"object_id": str(object_id), "chunk_id": str(chunk_id), "worker_id": config.worker_id,
            "expected_sha256": expected, "corrupted_sha256": sha256(changed + data[1:]).hexdigest()}


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("object_id", type=UUID)
    cli.add_argument("chunk_id", type=UUID)
    args = cli.parse_args()
    if os.environ.get("OBJECT_STORE_ALLOW_FAULT_INJECTION") != "1":
        cli.error("Set OBJECT_STORE_ALLOW_FAULT_INJECTION=1 for a disposable worker")
    try:
        print(json.dumps(corrupt_chunk(WorkerConfig.from_env(), args.object_id, args.chunk_id)))
    except (OSError, ValueError, sqlite3.Error) as exc:
        cli.error(str(exc))


if __name__ == "__main__":
    main()
