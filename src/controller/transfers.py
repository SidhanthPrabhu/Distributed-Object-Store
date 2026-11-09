"""Bounded worker HTTP, inventory reconciliation, and the course object path.

One async lock serializes reconciliation and foreground transfer preparation.
Heartbeats remain independent; identity/revision/freshness gates fence replies.
No worker-provided hash can enter an authoritative manifest.
"""

import asyncio
import logging
import tempfile
from datetime import datetime, timezone
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError
from starlette.responses import StreamingResponse

from common.config import WORKER_IDS
from common.errors import APIError
from common.models import (
    ChunkMetadata, ChunkPutHeaders, ChunkReadHeaders, ChunkReceipt, Checksum,
    ObjectMetadata, TombstoneReceipt, TombstoneRequest, WorkerInventory,
)

logger = logging.getLogger(__name__)


def unavailable(code="storage_unavailable", message="Required storage is unavailable"):
    return APIError(503, code, message)


class VerifiedDownload(StreamingResponse):
    """Own the anonymous spool until success, disconnect, or send failure."""

    def __init__(self, file, manifest, release):
        self.file = file
        self.release = release
        super().__init__(self.blocks(), media_type=manifest.content_type,
                         headers={"Content-Length": str(manifest.size_bytes)})

    async def blocks(self):
        while data := self.file.read(64 * 1024):
            yield data

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.file.close()
            self.release()


class Transfers:
    def __init__(self, store, registry, client, config):
        self.store, self.registry, self.client, self.config = store, registry, client, config
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.temp_bytes = 0
        self.reconciliation_errors: dict[str, str] = {}

    async def request(self, method, worker_id, path, *, limit, **kwargs):
        """Limit decoded bytes AND whole-attempt time (HTTPX also limits idle time).

        Retry only network failures, 5xx and throttling, preserving supplied intent.
        Never follow redirects or use an address supplied by a registering worker.
        """
        for attempt in range(self.config.http_attempts):
            try:
                async with asyncio.timeout(self.config.http_timeout_seconds):
                    async with self.client.stream(
                        method, self.config.worker_addresses[worker_id] + path, **kwargs,
                    ) as response:
                        body = bytearray()
                        async for part in response.aiter_bytes(64 * 1024):
                            if len(body) + len(part) > limit:
                                raise unavailable("invalid_worker_response", "Worker response exceeds limit")
                            body.extend(part)
                        result = httpx.Response(response.status_code, headers=response.headers,
                                                content=bytes(body))
                if result.status_code < 500 and result.status_code != 429:
                    return result
            except (httpx.HTTPError, TimeoutError):
                if attempt + 1 == self.config.http_attempts:
                    raise unavailable() from None
            if attempt + 1 < self.config.http_attempts:
                await asyncio.sleep(0.05 * (attempt + 1))
        raise unavailable()

    async def checked_chunk(self, worker_id, generation, chunk_id, length, digest):
        headers = ChunkReadHeaders(generation=generation, length_bytes=length,
                                   sha256=digest).to_headers()
        response = await self.request("GET", worker_id, f"/internal/chunks/{chunk_id}",
                                      headers=headers, limit=length)
        if (response.status_code != 200 or len(response.content) != length
                or sha256(response.content).hexdigest() != digest
                or response.headers.get("X-Chunk-SHA256") != digest
                or response.headers.get("X-Chunk-Length") != str(length)):
            raise unavailable("chunk_unavailable", "Stored chunk failed integrity or availability checks")
        return response.content

    async def cleanup_worker(self, worker_id, token):
        for need in self.store.pending_cleanup():
            if need["worker_id"] != worker_id:
                continue
            op = self.store.cleanup_intent(UUID(need["object_id"]))
            body = TombstoneRequest(**{k: op[k] for k in TombstoneRequest.model_fields})
            response = await self.request(
                "POST", worker_id, f"/internal/objects/{op['object_id']}/tombstone",
                json=body.model_dump(mode="json"), limit=64 * 1024,
            )
            if response.status_code != 200:
                raise unavailable("cleanup_pending", "Worker cleanup is pending")
            try:
                receipt = TombstoneReceipt.model_validate_json(response.content)
            except ValidationError:
                raise unavailable("invalid_receipt", "Invalid worker cleanup receipt") from None
            if (receipt.worker_id != worker_id or str(receipt.storage_id) != token[0]
                    or str(receipt.boot_id) != token[1]
                    or self.registry.reconciliation_token(worker_id) != token):
                raise unavailable("stale_operation", "Worker changed during cleanup")
            self.store.acknowledge_cleanup(receipt)

    async def reconcile(self, worker_id):
        token = self.registry.reconciliation_token(worker_id)
        if token is None:
            return
        await self.cleanup_worker(worker_id, token)
        response = await self.request("GET", worker_id,
            f"/internal/workers/{worker_id}/chunks", limit=self.config.inventory_max_bytes)
        if response.status_code != 200:
            raise unavailable("inventory_unavailable", "Worker inventory is unavailable")
        try:
            inventory = WorkerInventory.model_validate_json(response.content)
        except ValidationError:
            raise unavailable("invalid_inventory", "Invalid worker inventory") from None
        if (inventory.worker_id != worker_id or str(inventory.storage_id) != token[0]
                or str(inventory.boot_id) != token[1]):
            raise unavailable("identity_conflict", "Inventory identity differs from registration")
        entries = {str(c.chunk_id): c for c in inventory.chunks}
        placements = {p["chunk_id"]: p for p in self.store.worker_placements(worker_id)}
        for entry in inventory.chunks:
            authority = self.store.inventory_authority(entry.chunk_id, worker_id)
            if authority is None:
                raise unavailable("identity_conflict", "Inventory contains an unowned placement")
            # Cleanup must remove catalog entries as well as data before eligibility.
            if authority["state"] != "ACTIVE":
                raise unavailable("cleanup_pending", "Inventory still contains incomplete or deleted data")
            if (str(entry.object_id) != authority["object_id"]
                    or str(entry.generation) != authority["generation"]
                    or entry.length_bytes != authority["size_bytes"]
                    or entry.expected_sha256 != authority["sha256"]
                    or entry.operation_sequence != authority["operation_sequence"]):
                self.store.placement_fault(entry.chunk_id, worker_id, "inventory_conflict")
                raise unavailable("identity_conflict", "Inventory conflicts with authoritative intent")
            if str(entry.chunk_id) not in placements:
                # A5 must resolve ambiguous/excess ACTIVE placements. Do not adopt
                # a catalog entry as a receipt or retire a potentially last good copy.
                raise unavailable("uncommitted_placement", "Uncommitted placement needs recovery")
        verified = []
        for chunk_id, placement in placements.items():
            if placement["state"] != "ACTIVE":
                continue
            entry = entries.get(chunk_id)
            if entry is None:
                self.store.placement_fault(UUID(chunk_id), worker_id, "inventory_missing")
                continue
            try:
                await self.checked_chunk(worker_id, placement["generation"], chunk_id,
                                         placement["size_bytes"], placement["sha256"])
            except APIError:
                self.store.placement_fault(UUID(chunk_id), worker_id, "reconciliation_read_failed")
            else:
                verified.append(chunk_id)
        if self.registry.reconciliation_token(worker_id) != token:
            raise unavailable("stale_operation", "Worker changed during reconciliation")
        if any(n["worker_id"] == worker_id for n in self.store.pending_cleanup()):
            raise unavailable("cleanup_pending", "New cleanup must finish before reconciliation")
        self.store.confirm_placements(worker_id, verified)
        if not self.registry.mark_reconciled(worker_id, token):
            raise unavailable("stale_operation", "Worker changed during reconciliation")

    async def maintenance_once(self):
        async with self.lock:
            for worker in self.registry.snapshot():
                worker_id = worker["worker_id"]
                if worker["state"] == "HEALTHY" and not any(
                    n["worker_id"] == worker_id for n in self.store.pending_cleanup()
                ):
                    continue
                try:
                    await self.reconcile(worker_id)
                    self.reconciliation_errors.pop(worker_id, None)
                except (APIError, ValidationError) as exc:
                    self.registry.invalidate(worker_id)
                    self.reconciliation_errors[worker_id] = getattr(exc, "code", "invalid_receipt")

    async def run(self):
        while True:
            self.wake.clear()
            try:
                await self.maintenance_once()
            except Exception:
                logger.exception("Controller reconciliation pass failed; retaining pending work")
            try:
                await asyncio.wait_for(self.wake.wait(), self.config.reconciliation_interval_seconds)
            except TimeoutError:
                pass

    def select_pair(self, length):
        cursor = self.store.next_placement_cursor()
        eligible = {w["worker_id"]: w for w in self.registry.snapshot()
                    if w["state"] == "HEALTHY" and w["free_bytes"] is not None
                    and w["free_bytes"] >= length}
        ordered = [WORKER_IDS[(cursor + i) % 4] for i in range(4)]
        pair = [worker_id for worker_id in ordered if worker_id in eligible][:2]
        if len(pair) != 2:
            raise unavailable("insufficient_capacity", "Two eligible workers with capacity are required")
        return pair

    async def upload(self, file, metadata):
        async with self.lock:
            # UploadFile is disk-spooled by the multipart parser. First scan stores
            # only hashes/lengths in memory, then rewind for bounded chunk transfers.
            chunks, size, digest = [], 0, sha256()
            while data := await file.read(self.config.chunk_size_bytes):
                size += len(data)
                if size > self.config.upload_max_bytes:
                    raise APIError(400, "invalid_request", "Upload exceeds configured size limit")
                digest.update(data)
                chunks.append(ChunkMetadata(chunk_id=uuid4(), ordinal=len(chunks),
                    size_bytes=len(data), sha256=sha256(data).hexdigest()))
            now = datetime.now(timezone.utc)
            try:
                manifest = ObjectMetadata(object_id=uuid4(), generation=uuid4(),
                    name=file.filename or "object", size_bytes=size,
                    content_type=file.content_type or "application/octet-stream",
                    created_at=now, modified_at=now, checksum=Checksum(value=digest.hexdigest()),
                    chunk_size_bytes=self.config.chunk_size_bytes, chunk_count=len(chunks),
                    custom_metadata=metadata, chunks=chunks)
            except ValidationError:
                raise APIError(400, "invalid_request", "Invalid upload metadata") from None
            self.store.create_upload(manifest)
            try:
                await file.seek(0)
                for chunk in chunks:
                    data = await file.read(chunk.size_bytes)
                    if len(data) != chunk.size_bytes or sha256(data).hexdigest() != chunk.sha256:
                        raise unavailable("upload_changed", "Upload changed during transfer")
                    for worker_id in self.select_pair(chunk.size_bytes):
                        token = self.registry.reconciliation_token(worker_id)
                        op = self.store.create_put_intent(manifest.object_id, chunk.chunk_id, worker_id)
                        headers = ChunkPutHeaders(**{k: op[k] for k in ChunkPutHeaders.model_fields})
                        response = await self.request("PUT", worker_id,
                            f"/internal/chunks/{chunk.chunk_id}", content=data,
                            headers={**headers.to_headers(), "Content-Type": "application/octet-stream"},
                            limit=64 * 1024)
                        if response.status_code != 200:
                            raise unavailable("write_failed", "Worker did not confirm durable storage")
                        try:
                            receipt = ChunkReceipt.model_validate_json(response.content)
                        except ValidationError:
                            raise unavailable("invalid_receipt", "Invalid worker storage receipt") from None
                        if str(receipt.operation_id) != op["operation_id"]:
                            raise unavailable("invalid_receipt", "Receipt belongs to another operation")
                        if (worker_id not in self.registry.eligible_worker_ids()
                                or self.registry.reconciliation_token(worker_id) != token):
                            raise unavailable("stale_operation", "Worker changed during upload")
                        try:
                            self.store.record_receipt(receipt)
                        except APIError:
                            raise unavailable("invalid_receipt", "Storage receipt does not match intent") from None
                        self.registry.consume_capacity(worker_id, len(data))
                completed = self.store.get_manifest(manifest.object_id, include_hidden=True)
                eligible = set(self.registry.eligible_worker_ids())
                faults = self.store.faulty_placements()
                if any(w not in eligible or (str(c.chunk_id), w) in faults
                       for c in completed.chunks for w in c.locations):
                    raise unavailable("stale_operation", "Placement eligibility changed before publication")
                self.store.publish_upload(manifest.object_id)
                return self.store.get_manifest(manifest.object_id)
            except BaseException:
                # This durable terminal fence covers replies lost after remote PUT,
                # cancellation, malformed receipts and controller shutdown. A crash
                # before this transaction is handled by startup recovery.
                self.store.begin_deletion(manifest.object_id, WORKER_IDS)
                for worker in self.store.workers():
                    self.registry.invalidate(worker["worker_id"])
                self.wake.set()
                raise

    async def download(self, object_id):
        async with self.lock:
            manifest = self.store.get_manifest(object_id)
            if self.temp_bytes + manifest.size_bytes > self.config.download_temp_quota_bytes:
                raise unavailable("temporary_capacity", "Download temporary storage quota is exhausted")
            self.temp_bytes += manifest.size_bytes
            file = None
            try:
                file = tempfile.TemporaryFile(dir=self.config.temporary_directory)
                digest = sha256()
                for chunk in manifest.chunks:
                    data = None
                    faults = self.store.faulty_placements()
                    for worker_id in chunk.locations:
                        if (worker_id not in self.registry.eligible_worker_ids()
                                or (str(chunk.chunk_id), worker_id) in faults):
                            continue
                        token = self.registry.reconciliation_token(worker_id)
                        try:
                            candidate = await self.checked_chunk(worker_id, manifest.generation,
                                chunk.chunk_id, chunk.size_bytes, chunk.sha256)
                            if (worker_id not in self.registry.eligible_worker_ids()
                                    or token != self.registry.reconciliation_token(worker_id)):
                                continue
                            data = candidate
                            break
                        except APIError:
                            self.store.placement_fault(chunk.chunk_id, worker_id, "download_read_failed")
                    if data is None:
                        raise unavailable("no_valid_copy", "No valid copy of a required chunk is readable")
                    file.write(data)
                    digest.update(data)
                if file.tell() != manifest.size_bytes or digest.hexdigest() != manifest.checksum.value:
                    raise unavailable("object_corrupt", "Complete object verification failed")
                if self.store.object_state(object_id) != "ACTIVE":
                    raise APIError(404, "object_not_found", "Object not found")
                file.seek(0)
                return VerifiedDownload(file, manifest, lambda: self.release_temp(manifest.size_bytes))
            except BaseException as exc:
                if file is not None:
                    file.close()
                self.release_temp(manifest.size_bytes)
                if isinstance(exc, OSError):
                    raise unavailable("temporary_storage_unavailable", "Download temporary storage failed") from None
                raise

    def release_temp(self, length):
        self.temp_bytes -= length
