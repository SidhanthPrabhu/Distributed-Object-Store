"""Worker startup settings."""

import os
from pathlib import Path
from typing import Self

from common.config import StorageConfig
from common.models import WorkerID


class WorkerConfig(StorageConfig):
    worker_id: WorkerID
    data_dir: Path

    @classmethod
    def from_env(cls) -> Self:
        worker_id = os.environ.get("OBJECT_STORE_WORKER_ID", "worker1")
        return cls(
            worker_id=worker_id,
            data_dir=Path(os.environ.get("OBJECT_STORE_WORKER_DATA_DIR", f".local-data/{worker_id}")),
            chunk_size_bytes=int(os.environ.get("OBJECT_STORE_CHUNK_SIZE_BYTES", "1000000")),
        )
