"""Worker registration and heartbeat lifecycle."""

import asyncio
import logging

import httpx
from starlette.concurrency import run_in_threadpool

from common.errors import APIError
from common.models import (
    ErrorEnvelope, HeartbeatResponse, RegistrationResponse, WorkerHeartbeat, WorkerState,
)
from worker.store import ChunkStore

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 2.0
INITIAL_RETRY_DELAY = 0.25
MAX_RETRY_DELAY = 4.0


class Membership:
    def __init__(self, store: ChunkStore, client: httpx.AsyncClient):
        self.store = store
        self.client = client
        self.ready = False

    async def _register(self) -> RegistrationResponse:
        async with asyncio.timeout(REQUEST_TIMEOUT):
            response = await self.client.post(
                "/internal/workers/register", json=self.store.identity.model_dump(mode="json"),
            )
        registration = RegistrationResponse.model_validate_json(response.raise_for_status().content)
        await run_in_threadpool(self.store.observe_controller_epoch, registration.controller_epoch)
        return registration

    async def _heartbeat(self) -> HeartbeatResponse | None:
        free_bytes = await run_in_threadpool(self.store.free_bytes)
        reports, more = await run_in_threadpool(self.store.integrity_batch)
        message = WorkerHeartbeat(
            **self.store.identity.model_dump(),
            free_bytes=free_bytes, integrity_reports=reports, integrity_reports_more=more,
        )
        async with asyncio.timeout(REQUEST_TIMEOUT):
            response = await self.client.post(
                "/internal/workers/heartbeat", json=message.model_dump(mode="json"),
            )
        if response.status_code == 409:
            error = ErrorEnvelope.model_validate_json(response.content).error
            if error.code == "registration_required":
                return None
        heartbeat = HeartbeatResponse.model_validate_json(response.raise_for_status().content)
        await run_in_threadpool(self.store.observe_controller_epoch, heartbeat.controller_epoch)
        sent = {report.report_id for report in reports}
        await run_in_threadpool(self.store.acknowledge_reports,
                                [report_id for report_id in heartbeat.acknowledged_report_ids if report_id in sent])
        return heartbeat

    async def run(self) -> None:
        registration = None
        retry_delay = INITIAL_RETRY_DELAY
        while True:
            try:
                if registration is None:
                    registration = await self._register()
                heartbeat = await self._heartbeat()
                if heartbeat is None or heartbeat.controller_epoch != registration.controller_epoch:
                    registration = None
                else:
                    self.ready = heartbeat.state == WorkerState.HEALTHY
                    retry_delay = INITIAL_RETRY_DELAY
                    await asyncio.sleep(registration.heartbeat_interval_seconds)
                    continue
            except (httpx.HTTPError, ValueError, TimeoutError, APIError) as exc:
                if retry_delay == INITIAL_RETRY_DELAY:
                    logger.warning("Worker could not confirm controller membership (%s)", type(exc).__name__)
            self.ready = False
            await asyncio.sleep(retry_delay)
            retry_delay = min(2 * retry_delay, MAX_RETRY_DELAY)
