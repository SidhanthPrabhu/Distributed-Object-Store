"""Shared defaults and explicit environment loading; no packaging dependency."""

import json
import os
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from common.models import PositiveInt, WireModel, WorkerID

WORKER_IDS: tuple[WorkerID, ...] = ("worker1", "worker2", "worker3", "worker4")


class StorageConfig(WireModel):
    chunk_size_bytes: PositiveInt = 1_000_000
    replication_factor: Literal[2] = 2


class ControllerConfig(StorageConfig):
    database_path: Path = Path(".local-data/controller/metadata.sqlite3")
    worker_addresses: dict[WorkerID, str] = Field(
        default_factory=lambda: {name: f"http://{name}:8000" for name in WORKER_IDS}
    )
    heartbeat_interval_seconds: Annotated[float, Field(gt=0)] = 1.0
    suspect_after_seconds: Annotated[float, Field(gt=0)] = 3.0
    unavailable_after_seconds: Annotated[float, Field(gt=0)] = 5.0
    http_timeout_seconds: Annotated[float, Field(gt=0)] = 5.0
    http_max_connections: PositiveInt = 16

    @field_validator("worker_addresses")
    @classmethod
    def configured_addresses(cls, addresses: dict[WorkerID, str]) -> dict[WorkerID, str]:
        if set(addresses) != set(WORKER_IDS):
            raise ValueError("Configure exactly worker1 through worker4")
        normalized = {}
        for worker_id, address in addresses.items():
            parsed = urlsplit(address)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.path not in {"", "/"}):
                raise ValueError("Worker addresses must be HTTP(S) origins without credentials")
            # Validate the port and normalize equivalent default-port origins.
            port = parsed.port or (80 if parsed.scheme == "http" else 443)
            host = parsed.hostname.lower()
            host = f"[{host}]" if ":" in host else host
            normalized[worker_id] = f"{parsed.scheme}://{host}:{port}"
        if len(set(normalized.values())) != len(WORKER_IDS):
            raise ValueError("Each worker must have a distinct configured address")
        return normalized

    @model_validator(mode="after")
    def timing_order(self) -> Self:
        if not (self.heartbeat_interval_seconds < self.suspect_after_seconds
                < self.unavailable_after_seconds):
            raise ValueError("Require heartbeat interval < suspect timeout < unavailable timeout")
        return self

    @classmethod
    def from_env(cls) -> Self:
        values = {}
        converters = {
            "DATABASE_PATH": Path,
            "WORKER_ADDRESSES": json.loads,
            "CHUNK_SIZE_BYTES": int,
            "HEARTBEAT_INTERVAL_SECONDS": float,
            "SUSPECT_AFTER_SECONDS": float,
            "UNAVAILABLE_AFTER_SECONDS": float,
            "HTTP_TIMEOUT_SECONDS": float,
            "HTTP_MAX_CONNECTIONS": int,
        }
        for suffix, convert in converters.items():
            name = f"OBJECT_STORE_{suffix}"
            if name in os.environ:
                values[suffix.lower()] = convert(os.environ[name])
        return cls(**values)
