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
        self._lock = RLock()

    def register(self, identity: WorkerRegistration) -> RegistrationResponse:
        with self._lock:
            self.store.register(identity)
            self._last_seen[identity.worker_id] = self.clock()
            return RegistrationResponse(
                controller_epoch=self.store.epoch,
                heartbeat_interval_seconds=self.config.heartbeat_interval_seconds,
            )

    def heartbeat(self, heartbeat: WorkerHeartbeat) -> HeartbeatResponse:
        with self._lock:
            if heartbeat.worker_id not in self._last_seen:
                raise APIError(409, "registration_required", "Register with this controller process first")
            acknowledged = self.store.accept_heartbeat(heartbeat)
            self._last_seen[heartbeat.worker_id] = self.clock()
            return HeartbeatResponse(
                controller_epoch=self.store.epoch, state=WorkerState.RECONCILING,
                acknowledged_report_ids=acknowledged,
            )

    def snapshot(self) -> list[dict]:
        with self._lock:
            now = self.clock()
            workers = self.store.workers()
            for worker in workers:
                seen = self._last_seen.get(worker["worker_id"])
                state = WorkerState.RECONCILING
                if seen is not None:
                    age = max(0.0, now - seen)
                    if age >= self.config.unavailable_after_seconds:
                        state = WorkerState.UNAVAILABLE
                    elif age >= self.config.suspect_after_seconds:
                        state = WorkerState.SUSPECT
                worker["state"] = state
            return workers

    def eligible_worker_ids(self) -> list[str]:
        # Neither registration, heartbeat nor a catalog assertion proves that
        # inventory/cleanup reconciliation has completed. A2's next slice adds
        # the guarded HEALTHY transition; no worker can bypass it here.
        return [w["worker_id"] for w in self.snapshot() if w["state"] == WorkerState.HEALTHY]
