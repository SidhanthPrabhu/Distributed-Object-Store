"""Worker startup settings."""

import os
from pathlib import Path
from typing import Self

from pydantic import AnyHttpUrl, field_validator

from common.config import StorageConfig
from common.models import WorkerID


class WorkerConfig(StorageConfig):
    worker_id: WorkerID
    data_dir: Path
    controller_url: AnyHttpUrl | None = None

    @field_validator("controller_url")
    @classmethod
    def controller_origin(cls, url: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if url is not None and (
            url.username is not None or url.password is not None
            or url.query is not None or url.fragment is not None or url.path not in (None, "/")
        ):
            raise ValueError("Controller URL must be an HTTP(S) origin without credentials")
        return url

    @classmethod
    def from_env(cls) -> Self:
        worker_id = os.environ.get("OBJECT_STORE_WORKER_ID", "worker1")
        return cls(
            worker_id=worker_id,
            data_dir=Path(os.environ.get("OBJECT_STORE_WORKER_DATA_DIR", f".local-data/{worker_id}")),
            chunk_size_bytes=int(os.environ.get("OBJECT_STORE_CHUNK_SIZE_BYTES", "1000000")),
            controller_url=os.environ.get("OBJECT_STORE_CONTROLLER_URL"),
        )
