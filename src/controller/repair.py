"""Serial restoration and excess-copy cleanup using trusted manifests."""

import logging
from collections import defaultdict
from time import monotonic
from uuid import UUID

from common.config import WORKER_IDS
from common.errors import APIError
from common.models import MutationHeaders, WorkerState

logger = logging.getLogger(__name__)


class Repairs:
    def __init__(self, transfers):
        self.transfers = transfers
        self.store = transfers.store
        self.registry = transfers.registry
        self.cursor = ""
        self.failures: dict[str, tuple[int, float, dict]] = {}
        self.running: dict | None = None

    def snapshot(self):
        workers = {w["worker_id"]: w for w in self.registry.snapshot()}
        healthy = {key: worker for key, worker in workers.items() if worker["state"] == WorkerState.HEALTHY}
        chunks = defaultdict(list)
        for row in self.store.active_chunk_placements():
            chunks[row["chunk_id"]].append(row)
        jobs = {}
        for cid, rows in chunks.items():
            placements = [p for p in rows if p["worker_id"] is not None]
            available = [p for p in placements if p["worker_id"] in healthy
                         and p["reason"] is None and p["retirement_id"] is None]
            if len(available) < 2 or len(placements) > 2:
                jobs[cid] = (rows[0], placements, available)
        return workers, healthy, jobs

    def status(self) -> dict:
        workers, _, jobs = self.snapshot()
        deficits = []
        repairs = []
        for cid, (_, _, available) in jobs.items():
            if len(available) < 2:
                deficits.append({"chunk_id": cid, "available_copy_count": len(available)})
            if self.running and self.running["chunk_id"] == cid:
                repairs.append(dict(self.running))
            elif cid in self.failures:
                repairs.append(dict(self.failures[cid][2]))
            else:
                repairs.append({"chunk_id": cid, "state": "queued", "source": None,
                                "destination": None, "error_reason": None})
        return {
            "workers": [workers.get(w) or {"worker_id": w, "storage_id": None, "boot_id": None,
                                          "last_heartbeat_at": None, "state": WorkerState.UNAVAILABLE}
                        for w in WORKER_IDS],
            "replication_deficits": deficits,
            "repairs": repairs,
        }

    async def read(self, chunk, worker_id, *, require_healthy=True) -> bytes:
        token = self.registry.reconciliation_token(worker_id)
        if token is None or (require_healthy and worker_id not in self.registry.eligible_worker_ids()):
            raise APIError(503, "source_unavailable", "Repair source is unavailable")
        data = await self.transfers.checked_chunk(worker_id, chunk["generation"], chunk["chunk_id"],
                                                  chunk["size_bytes"], chunk["sha256"])
        if token != self.registry.reconciliation_token(worker_id):
            raise APIError(503, "stale_operation", "Repair source changed during checked read")
        return data

    async def find_source(self, chunk, candidates) -> tuple[str, bytes]:
        for source in candidates:
            try:
                return source, await self.read(chunk, source)
            except APIError:
                self.store.placement_fault(UUID(chunk["chunk_id"]), source, "repair_read_failed")
        raise APIError(503, "no_valid_copy", "No valid copy is available for repair")

    async def recover_write(self, chunk, worker_id, token):
        """An exact journaled write survived an uncertain reply or a restart."""
        try:
            data = await self.read(chunk, worker_id, require_healthy=False)
        except APIError:
            eligible = set(self.registry.eligible_worker_ids()) - {worker_id}
            candidates = [p["worker_id"] for p in self.store.active_chunk_placements()
                          if p["chunk_id"] == chunk["chunk_id"] and p["worker_id"] in eligible
                          and p["reason"] is None and p["retirement_id"] is None]
            _, data = await self.find_source(chunk, candidates)
        # Never publish solely from an inventory claim. A new receipt fences
        # both the previous controller epoch and a possibly superseded worker boot.
        op = self.store.create_put_intent(UUID(chunk["object_id"]), UUID(chunk["chunk_id"]),
                                          worker_id, repair=True, renew=True)
        await self.transfers.write_chunk(op, data, token, require_healthy=False)

    async def restore(self, chunk, placements, available, healthy):
        sources = [p["worker_id"] for p in available]
        if not sources:
            raise APIError(503, "no_valid_copy", "No valid copy is available for repair")
        candidates = [w for w, worker in healthy.items() if w not in sources
                      and (worker["free_bytes"] or 0) >= chunk["size_bytes"]]
        if not candidates:
            raise APIError(503, "no_destination", "No distinct eligible worker has capacity")
        existing = {p["worker_id"] for p in placements}
        destination = min(candidates, key=lambda w: (w not in existing, w))
        self.running["destination"] = destination
        source, data = await self.find_source(chunk, sources)
        self.running["source"] = source
        token = self.registry.reconciliation_token(destination)
        op = self.store.create_put_intent(UUID(chunk["object_id"]), UUID(chunk["chunk_id"]),
                                          destination, repair=True)
        await self.transfers.write_chunk(op, data, token)

    async def trim(self, chunk, placements, available, healthy):
        # Keep two verified copies before issuing any remote DELETE. Prefer
        # the newest replacements and finish an existing retirement first.
        keep = [p["worker_id"] for p in
                sorted(available, key=lambda p: p["operation_sequence"], reverse=True)[:2]]
        excess = [p for p in placements if p["worker_id"] in healthy and p["worker_id"] not in keep]
        if not excess:
            raise APIError(503, "waiting_for_rejoin", "An excess placement is on an unavailable worker")
        target = min(excess, key=lambda p: (p["retirement_id"] is None, p["operation_sequence"]))["worker_id"]
        self.running["destination"] = target
        tokens = {worker: self.registry.reconciliation_token(worker) for worker in (*keep, target)}
        for worker in keep:
            try:
                await self.read(chunk, worker)
            except APIError:
                self.store.placement_fault(UUID(chunk["chunk_id"]), worker, "retirement_read_failed")
                raise
        if any(token is None or token != self.registry.reconciliation_token(w) for w, token in tokens.items()):
            raise APIError(503, "stale_operation", "A worker changed before excess-copy retirement")
        op = self.store.begin_retirement(chunk, target)
        headers = MutationHeaders(**{key: op[key] for key in MutationHeaders.model_fields}).to_headers()
        response = await self.transfers.request("DELETE", target, f"/internal/chunks/{chunk['chunk_id']}",
                                                headers=headers, limit=64 * 1024)
        if response.status_code != 204 or tokens[target] != self.registry.reconciliation_token(target):
            raise APIError(503, "retirement_pending", "Excess worker did not confirm current retirement")
        self.store.finish_retirement(op)

    async def run_once(self) -> None:
        _, healthy, jobs = self.snapshot()
        self.failures = {cid: retry for cid, retry in self.failures.items() if cid in jobs}
        now = monotonic()
        due = [cid for cid in jobs if cid not in self.failures or self.failures[cid][1] <= now]
        if not due:
            return
        cid = next((key for key in due if key > self.cursor), due[0])
        self.cursor = cid
        chunk, placements, available = jobs[cid]
        self.running = {"chunk_id": cid, "state": "running", "source": None,
                        "destination": None, "error_reason": None}
        try:
            if len(available) < 2:
                await self.restore(chunk, placements, available, healthy)
            else:
                await self.trim(chunk, placements, available, healthy)
        except APIError as exc:
            attempts = self.failures.get(cid, (0,))[0] + 1
            delay = 2 ** min(attempts - 1, 3)
            self.failures[cid] = (attempts, monotonic() + delay,
                                  {**self.running, "state": "blocked", "error_reason": exc.code})
            logger.debug("Repair of %s is waiting (%s)", cid, exc.code)
        else:
            self.failures.pop(cid, None)
        finally:
            self.running = None
