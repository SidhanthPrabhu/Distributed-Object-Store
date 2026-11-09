"""Registration/heartbeat foundation. Eligibility waits for full reconciliation."""

import time
from collections.abc import Callable
from threading import RLock

from common.config import ControllerConfig
from common.errors import APIError
from common.models import (
    HeartbeatResponse, RegistrationResponse, WorkerHeartbeat, WorkerRegistration, WorkerState,
)
from controller.metadata import MetadataStore


class WorkerRegistry:
    def __init__(self, store: MetadataStore, config: ControllerConfig,
                 clock: Callable[[], float] = time.monotonic):
        self.store = store
        self.config = config
        self.clock = clock
        self._last_seen: dict[str, float] = {}
        self._reconciled: dict[str, tuple[str, str, int]] = {}
        self._revision: dict[str, int] = {}
        self._reports_more: set[str] = set()
        self._lock = RLock()

    def register(self, identity: WorkerRegistration) -> RegistrationResponse:
        with self._lock:
            self.store.register(identity)
            self.invalidate(identity.worker_id)
            self._last_seen[identity.worker_id] = self.clock()
            return RegistrationResponse(
                controller_epoch=self.store.epoch,
                heartbeat_interval_seconds=self.config.heartbeat_interval_seconds,
            )

    def heartbeat(self, heartbeat: WorkerHeartbeat) -> HeartbeatResponse:
        with self._lock:
            if heartbeat.worker_id not in self._last_seen:
                raise APIError(409, "registration_required", "Register with this controller process first")
            # Expiry cannot be erased by a late heartbeat without reconciliation.
            if self.clock() - self._last_seen[heartbeat.worker_id] >= self.config.suspect_after_seconds:
                self.invalidate(heartbeat.worker_id)
            acknowledged = self.store.accept_heartbeat(heartbeat)
            if heartbeat.integrity_reports or heartbeat.integrity_reports_more:
                self.invalidate(heartbeat.worker_id)
            if heartbeat.integrity_reports_more:
                self._reports_more.add(heartbeat.worker_id)
            else:
                self._reports_more.discard(heartbeat.worker_id)
            self._last_seen[heartbeat.worker_id] = self.clock()
            return HeartbeatResponse(
                controller_epoch=self.store.epoch, state=self.state(heartbeat.worker_id),
                acknowledged_report_ids=acknowledged,
            )

    def snapshot(self) -> list[dict]:
        with self._lock:
            now = self.clock()
            workers = self.store.workers()
            for worker in workers:
                seen = self._last_seen.get(worker["worker_id"])
                state = WorkerState.RECONCILING
                key = (worker["storage_id"], worker["boot_id"],
                       self._revision.get(worker["worker_id"], 0))
                if self._reconciled.get(worker["worker_id"]) == key:
                    state = WorkerState.HEALTHY
                if seen is not None:
                    age = max(0.0, now - seen)
                    if age >= self.config.unavailable_after_seconds:
                        state = WorkerState.UNAVAILABLE
                        self.invalidate(worker["worker_id"])
                    elif age >= self.config.suspect_after_seconds:
                        state = WorkerState.SUSPECT
                        self.invalidate(worker["worker_id"])
                worker["state"] = state
            return workers

    def eligible_worker_ids(self) -> list[str]:
        return [w["worker_id"] for w in self.snapshot() if w["state"] == WorkerState.HEALTHY]

    def state(self, worker_id: str) -> WorkerState:
        return next(w["state"] for w in self.snapshot() if w["worker_id"] == worker_id)

    def invalidate(self, worker_id: str) -> None:
        with self._lock:
            self._reconciled.pop(worker_id, None)
            self._revision[worker_id] = self._revision.get(worker_id, 0) + 1

    def reconciliation_token(self, worker_id: str) -> tuple[str, str, int] | None:
        with self._lock:
            worker = next((w for w in self.snapshot() if w["worker_id"] == worker_id), None)
            if (worker is None or worker_id not in self._last_seen
                    or worker["state"] in {WorkerState.SUSPECT, WorkerState.UNAVAILABLE}
                    or worker_id in self._reports_more):
                return None
            return worker["storage_id"], worker["boot_id"], self._revision.get(worker_id, 0)

    def mark_reconciled(self, worker_id: str, token: tuple[str, str, int]) -> bool:
        with self._lock:
            if self.reconciliation_token(worker_id) != token:
                return False
            self._reconciled[worker_id] = token
            return True

    def consume_capacity(self, worker_id: str, length: int) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE workers SET free_bytes=MAX(0,free_bytes-?) WHERE worker_id=?",
                       (length, worker_id))
