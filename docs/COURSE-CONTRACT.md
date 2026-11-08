# Course controller/worker contract

Initial implementation agreement, 8 November 2026. This is the target contract; implementation is partial. [A1/A2 foundation status](CONTROLLER-FOUNDATION.md) records available models and registration/heartbeat behavior; inventory reconciliation and real-service acceptance remain pending. You own changes; Friend reviews them. This is the small course contract; use the handoff's [system specification](../CC-research-handoff/docs/system-spec.md) for detailed safety rationale. The assignment's required routes remain unchanged.

## Topology and common rules

- One controller and worker1–worker4 on the Compose network; one application process per service. External clients use the controller. Each service has a separate persistent volume.
- Initial startup targets: `controller.app:create_app` and `worker.app:create_app`, both FastAPI factories, using a Python `src/` package layout (`src/controller/app.py`, `src/worker/app.py`). Containers run Uvicorn with `--factory`, bind `0.0.0.0`, and use internal port 8000; Compose maps only the public controller port. Packaging belongs to Friend, the controller entry point to You, and the worker entry point to Friend. These are agreed targets, not commands verified against an application yet.
- Initial chunk size is `1000000` bytes, replication factor is `2`, and hashes use SHA-256 lowercase hex. UUIDs identify objects, generations, chunks, operations, storage identities, and boots. Worker IDs are configured names `worker1`–`worker4`.
- The controller's configured worker address map is authoritative. Registration cannot supply arbitrary fetch URLs or make two worker names share a storage identity.
- Timestamps in JSON use UTC RFC3339. Measure heartbeat age locally at the controller; do not compare remote monotonic clocks.
- Shared Pydantic/wire models live in `src/common/`. Python imports do not replace HTTP integration checks.
- Suggested initial heartbeat timing: every 1 second; suspect after 3 seconds; unavailable after 5 seconds. Make these configurable and test eventual transitions, not exact millisecond timing. Only reconciled workers are eligible for new storage work.
- Binary routes use raw bytes with a correct Content-Type and length. JSON routes never contain base64 file bodies.

## Error envelope

```json
{"error":{"code":"chunk_corrupt","message":"Stored chunk failed integrity verification","request_id":"uuid"}}
```

Use meaningful stable codes. Public APIs use assignment statuses: 400 invalid request, 404 unknown object, 409 conflict, 500 unexpected internal failure, 503 unavailable storage/service. A missing/corrupt chunk of a known object maps to public 503 if no valid copy is readable, rather than 404 for the object. Successful DELETE has no response body. Normalize request validation failures to 400.

## Required worker routes

### PUT `/internal/chunks/{chunk_id}`

Raw binary request, `Content-Type: application/octet-stream`, with headers:

| Header | Meaning |
|---|---|
| `X-Object-ID` | Parent object UUID |
| `X-Object-Generation` | Immutable object generation UUID |
| `X-Operation-ID` | Stable UUID for this mutation; reuse on retry |
| `X-Controller-Epoch` | Persisted integer incremented at controller startup |
| `X-Operation-Sequence` | Persisted globally increasing mutation sequence |
| `X-Chunk-SHA256` | Trusted expected hex digest |
| `X-Chunk-Length` | Trusted expected byte length, at most configured chunk size |

Workers validate actual body length/hash, stage and flush data, atomically publish, and durably record the receipt before returning **200**, including for a verified idempotent retry. Receipt fields are `operation_id`, `operation_sequence`, `controller_epoch`, `object_id`, `generation`, `chunk_id`, `worker_id`, `storage_id`, `boot_id`, `length_bytes`, `sha256`, and `durable: true`. The controller validates the receipt against its intent.

Conflicting immutable identity/content is 409. A tombstone or stale mutation is 409. Invalid body length/hash is 400 with a specific code. Storage errors are 503. A newer authorized operation may atomically replace a known corrupt copy with bytes matching the original expected identity; it cannot change that expected digest. A crash between file and receipt publication must be reconciled at startup.

### GET `/internal/chunks/{chunk_id}`

Send `X-Object-Generation`, `X-Chunk-SHA256`, and `X-Chunk-Length` from the controller manifest. The worker compares them to its catalog, freshly reads and verifies the file, then returns **200** with exact raw bytes and matching hash/length headers. The controller also checks received bytes against its manifest before using them.

Return 404 `chunk_missing`, 409 `chunk_corrupt`/`identity_conflict`, or 503 `storage_unavailable` as appropriate. Neither side updates the expected digest to match damaged data.

### DELETE `/internal/chunks/{chunk_id}`

Send object/generation and operation ID/epoch/sequence headers. This removes a single placement and records its retirement sequence; it is distinct from terminal object deletion. Return **204** after durable cleanup, also when an authorized retry finds the chunk already absent. Reject a stale retirement that would remove a newer placement. A later authorized placement may reuse the location, unless the object has a terminal tombstone.

### GET `/internal/workers/{worker_id}/chunks`

Return **200** with `worker_id`, `storage_id`, `boot_id`, and `chunks`. Each chunk entry contains `object_id`, `generation`, `chunk_id`, `length_bytes`, `expected_sha256`, `state`, `operation_sequence`, and nullable `last_verified_at`. Expected fields are catalog values; they do not claim the current file was freshly verified. Exclude staging files from valid entries; report suspect/missing/corrupt catalog entries with their state. A mismatched requested worker ID is 404.

## Registration and heartbeat

Workers send `POST /internal/workers/register` to the controller with `worker_id`, `storage_id`, and `boot_id`. The controller uses its configured address, validates identity conflicts (409), and returns **200** with `controller_epoch`, `heartbeat_interval_seconds`, and `state: "RECONCILING"`. It then fetches inventory, reapplies pending cleanup, and reconciles before changing state to HEALTHY. Restart does not automatically make old replicas trusted.

Workers send `POST /internal/workers/heartbeat` with the same identity fields, `free_bytes`, `integrity_reports`, and `integrity_reports_more`. Each durable integrity report has a stable `report_id`, object/generation/chunk identity, `observed_at`, and `outcome` such as `corrupt`, `missing`, or `read_error`. Send at most 100 reports per heartbeat; retain the remainder and repeat unacknowledged reports. Persist/coalesce outstanding faults by chunk so failures do not grow an unlimited log. Never send file contents. If report persistence/capacity fails, expose degraded health and trigger inventory reconciliation; do not silently discard a fault.

The controller returns **200** with `controller_epoch`, current `state`, and `acknowledged_report_ids`. A report is acknowledged only after the corresponding fault/repair need is persisted. Duplicate reports are idempotent. Unknown/stale boot identity receives 409 `registration_required`, causing registration again. A heartbeat proves process reachability, not that every stored byte is sound.

Local scanning uses catalog expected values originally accepted from controller intents and checked during reconciliation. It checks every local committed replica in fixed order, with configurable pauses and bounded concurrency. Track verification age per exact placement, not per logical chunk across workers. Pause/avoid catalog entries whose object has been tombstoned.

## Object tombstone extension

For safe deletion add `POST /internal/objects/{object_id}/tombstone`, a small internal extension allowed by the assignment. JSON body: `generation`, `operation_id`, `controller_epoch`, `operation_sequence`. The worker durably fences future writes for that object/generation, coordinates ongoing local writes, removes canonical/staged/quarantined data, and returns **200** with identity fields and `cleanup_complete: true` only after cleanup. An absent object still gets a durable tombstone and successful acknowledgement. Retrying the same tombstone is idempotent.

The controller hides the object immediately after recording deletion. It returns public **204** only after all four workers acknowledge cleanup; otherwise public **503** with code `deletion_pending`. Retrying a pending deletion resumes cleanup. A later ordinary deletion of a fully deleted object is 404. Persist pending cleanup across restart. This conservative pending-deletion policy is our design choice for an unspecified assignment edge case.

## Public metadata and inspection

Use the exact six public routes in [PROJECT-SCOPE.md](../PROJECT-SCOPE.md). Upload uses multipart `file` and optional `metadata` JSON-object string. Return assigned object metadata on 201. Data stays immutable; PATCH merges custom keys and updates modification time/revision only.

Metadata includes `object_id`, `generation`, `name`, `size_bytes`, `content_type`, `created_at`, `modified_at`, `checksum: {algorithm, value}`, `chunk_size_bytes`, `chunk_count`, `metadata_revision`, `custom_metadata`, and ordered `chunks`. Each chunk contains `chunk_id`, `ordinal`, `size_bytes`, `sha256`, and `locations` (worker ID strings). Persist the upload's chunk size. An empty file has zero chunks and SHA-256 of empty bytes. List returns `{ "objects": [...] }` with at least ID/name/size/type per entry; pagination is deferred for course scope.

Add one read-only extension, `GET /api/v1/admin/status`, returning `workers` (identity/state/last heartbeat), `replication_deficits` (chunk ID and available-copy count), and `repairs` (chunk ID, queued/running/blocked state, source/destination, error reason). Exclude file bodies and secrets. Keep internal/control routes on the local project network; this course deployment does not add a public authentication product.

## Repair, restart, and deletion boundary

You own repair decisions; Friend owns the storage effects. Reuse chunk GET/PUT for serial controller-relayed repair, buffering at most one chunk per transfer. Persist repair intent, validate source and destination, then conditionally commit the placement. Recheck object state and intent before commit; the worker must also enforce tombstones during publication. A write arriving after controller deletion cannot become a visible resurrected object.

Controller metadata remains the authority across restart. Workers return inventory/receipts; they do not reconstruct arbitrary manifests. A returning third copy is verified before choosing two safe placements and retiring excess. If no valid readable source remains, expose unavailability and retry with bounded backoff instead of inventing recovery.

## Contract-change checklist

Both people review method/path changes, required fields, response statuses, mutation sequences/tombstones, heartbeat reports, and content verification. Update this document, shared models, consumers, and relevant checks together. Add the decision to the tracker. Never silently change an API because one service is easier to implement that way.
