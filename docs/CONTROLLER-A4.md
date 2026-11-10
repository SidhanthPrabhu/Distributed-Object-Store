# A4 controller handoff

10 November 2026. A4 is **IN REVIEW**, awaiting Friend's review and C2 joint acceptance with B5's initial client/inspection. C0/C1 remain DONE with their already-confirmed reviews; older foundation notes describe historical slices. Work is uncommitted on `main`. Only controller source, controller tests and controller documentation changed; no worker, client, deployment, lock or packaging source was edited.

## Public behavior and interfaces

All six required public routes now exist:

| Route | Success | Behavior |
|---|---|---|
| `POST /api/v1/objects` | 201 | Existing multipart upload; complete `ObjectMetadata`; hidden until two durable placements per chunk |
| `GET /api/v1/objects/{object_id}` | 200 | Existing fully checked binary download with original content type/length |
| `GET /api/v1/objects/{object_id}/metadata` | 200 | Complete system/custom metadata with ordinal-ordered chunks and persisted locations; no file bodies or worker network calls |
| `PATCH /api/v1/objects/{object_id}/metadata` | 200 | Transactional custom-key merge, modification time and revision advance; full metadata for that revision |
| `GET /api/v1/objects` | 200 | `{"objects":[...]}` with ID/name/size/type, ACTIVE only, ordered by creation time then ID |
| `DELETE /api/v1/objects/{object_id}` | 204 | Empty response after all four workers acknowledge durable terminal cleanup |

PATCH uses the handoff's **direct custom-key map**, explicitly confirmed by the owner during this task. For example:

```json
{"course":"CS838","settings":{"enabled":true},"note":null}
```

Top-level keys merge; a supplied nested value replaces that entire custom value. Untouched keys survive. Null is stored, rather than removing a key. Every accepted PATCH, including `{}`, advances the revision once and advances modification time monotonically even if the wall clock moves backward. Object/generation/chunk IDs, creation time, checksums, bytes, size, content type and placements remain unchanged. A custom key named `checksum` or `size_bytes` remains inside `custom_metadata`; it cannot assign a system field. There is no wrapper or system-field update operation. JSON Patch arrays are rejected. This scoping preserves legal system-looking custom names instead of banning them.

PATCH requires application/json and accepts an optional charset parameter. Bodies larger than 64 KiB, malformed/non-object/non-finite/overdeep JSON and malformed UUIDs return 400 `invalid_request`. The limit applies per PATCH request, not to the accumulated custom map. Upload retains its existing multipart/file limits and shares the strict custom-map parser. Malformed multipart parser errors now receive the same 400 envelope. Unexpected public failures receive sanitized 500 `internal_error`; the revision ceiling returns 409 `metadata_revision_exhausted` without a partial write. Errors carry the existing `error.code/message/request_id` envelope and matching X-Request-ID header.

Metadata GET/PATCH, download and list hide incomplete, DELETING and DELETED objects. Unknown/hidden IDs return 404 `object_not_found`. Valid metadata/list requests do not depend on worker availability. Locations are durable manifest placements, not live health assertions. Worker read failures still become public 503 when no valid copy is available.

## Deletion and concurrency

`MetadataStore.begin_deletion(..., public=True)` atomically admits ACTIVE/pending DELETING objects and rejects unknown, UPLOADING or fully DELETED IDs. It preserves the existing durable terminal operation, global sequence allocation and all-four-worker cleanup table. Logical hiding and terminal intent commit **before acquiring the transfer lock or making remote requests**. A concurrent PATCH either commits while ACTIVE, or receives 404 after deletion wins; no custom update commits to a hidden object.

`Transfers.delete()` reuses `cleanup_worker()` with an object filter, so unrelated pending objects cannot prevent this object's foreground cleanup. It tries all configured workers, including workers with no placements for this object. Failed, malformed, conflicting or stale receipts leave cleanup pending and map to public 503 `deletion_pending`, rather than leaking an internal worker 409 as a client conflict. Unknown/unregistered/offline workers cannot satisfy cleanup. Only matching durable receipts with the requested intent, current controller epoch and current registered worker/storage/boot/token can acknowledge it.

Retries within an epoch retain operation ID and sequence. Existing maintenance resumes pending work, including after lost replies/cancellation. On restart, unfinished cleanup is reissued once in the new epoch with a higher sequence/new operation ID. Already acknowledged workers retain their terminal fences; pending workers acknowledge the refreshed intent. Completion retires replica records/pending operations and keeps the terminal manifest/history. Workers retain retired catalog rows and operation history, while removing staged/canonical files and excluding retired rows from inventory.

An admitted pending DELETE may return 204 if maintenance completes while it waits. A new DELETE after completion returns 404. A client therefore may see 404 on a later retry if background cleanup already finished; this is the agreed contract, not response replay/idempotency-key support.

PATCH/list/metadata use short SQLite transactions with no worker requests. PATCH's read/merge/revision/write/response snapshot is one transaction, preventing lost keys or returning a later concurrent revision. Deletion, transfers and maintenance retain the existing async transfer lock; registration/heartbeats remain independent. The existing controller gates reject delayed PUT receipts/new PUT intents/publication after terminal deletion. Actual workers enforce the corresponding terminal fence. No schema migration, new wire model or worker protocol change was needed.

## Automated checks: doubles and local worker tests

The combined locked suite includes the original controller/worker tests plus 47 A4 checks. Controller tests use disposable SQLite databases, FastAPI TestClient and HTTPX contract worker doubles. Existing worker tests use the real storage implementation locally; the suite is distinct from the five-service acceptance probe below.

A4 covers complete metadata and ordered two-copy locations; empty objects; nested/null values; shallow merge; immutable system fields; zero worker calls during PATCH; byte-identical downloads; 24 concurrent merges with unique revisions; ACTIVE-only visibility; all-four cleanup before 204; an offline first/last worker; stable pending retries; invalid/nonboolean/stale receipts; worker boot changes; lost acknowledgements; controller cancellation/restart during cleanup; PATCH/delete races; hiding before the transfer lock is available; delayed receipts/PUTs; and consistent sanitized errors across all six routes. JSON parser, content type, size/depth and malformed multipart cases are included.

Exact final commands/results:

```text
uv run --locked --offline pytest -q -o faulthandler_timeout=20
179 passed, 1 existing dependency deprecation warning in 40.31s

python3 -m compileall -q src/controller tests/controller
passed

git diff --check
passed
```

The initial combined baseline passed 132 tests in 23.94s. The first A4 run found an overdeep JSON input returning 500; the input is now validated before the metadata transaction and returns 400. A lost-reply test was adjusted to allow additional background retries while requiring the same intent. The focused final A4 suite passed 47 tests in 14.34s. The only warning is the existing Starlette TestClient HTTPX deprecation; no lock/dependency change was made.

## Real five-service evidence

The existing Compose project initially had no running services. Its named volumes were retained. These commands built/started the existing deployment and updated only the running controller after A4 source was complete:

```text
docker compose ps
no running services initially

docker compose up --build --wait
passed; five services started, existing volumes retained

docker compose build controller
passed

docker compose up -d --no-deps --wait controller
passed

python3 tests/controller/check_a4_services.py > /tmp/cc-a4-services.json
exit 0; all real-service assertions passed

docker compose ps
controller and worker1–worker4 healthy after the probe
```

The manual probe is retained under `tests/controller/` (not collected by pytest). It uses the real HTTP APIs, read-only SQLite snapshots of worker operation/catalog/tombstone records, actual worker files and the existing deployment. It creates disposable objects and cleans up only those IDs. Its recovery sequence uses `docker compose stop worker4`, pending DELETE/retry, `docker compose stop controller`, `docker start worker4`, `docker compose start controller`, and final service availability polling. It never removes volumes or directly writes runtime databases. The local runner required execution permission for the installed uv launcher and Docker build/cache/socket access; restricted attempts did not establish application failures.

The [small result artifact](testing/a4-services.json) records:

- Object `98add0c8-118d-4ff1-abd5-e95fa65bbe42`: 2,000,128 bytes, chunks 1,000,000 + 1,000,000 + 128 and six actual placements. SHA-256 `fa758c84e15620affff7e202d7f5b3e9d51577350c1c8d316bb4348bc6e5adca` matched the byte-identical public download after PATCH.
- Nested custom merge and twelve concurrent real HTTP PATCH requests preserved keys and produced revisions 3–14. Worker operation/catalog/file snapshots were unchanged across PATCH: zero chunk writes. The original system manifest fields remained identical.
- Empty upload/download/metadata/PATCH and empty 204 deletion worked. Every worker had a completed durable tombstone even though no chunks existed. Fully deleted IDs returned 404; invalid input used the public envelope.
- With worker4 stopped, DELETE and its retry returned 503 `deletion_pending`; download, metadata, PATCH and list immediately hid the object.
- While the controller was stopped, the probe sent the existing pending tombstone directly to worker4 over a real socket and deliberately discarded its response. Read-only worker inspection confirmed durable cleanup before controller restart. This is a controlled lost acknowledgement, not an actual crash or injected worker disk failure.
- On controller startup the epoch advanced 11 → 12; registration/reconciliation replayed the still-pending intent and all four durable cleanup records completed. The actual chunk files were absent and catalog rows retired. An old authorized PUT replay returned real worker 409 and did not restore visibility. A separately patched survivor retained exact metadata and bytes across restart.
- All disposable IDs were terminally deleted. The one pre-existing ACTIVE object's list summary remained intact. No unrelated data or persistent volume was removed. All five services were restored and healthy.

Earlier probe attempts failed only in the probe: it initially expected retired worker catalog rows to be removed, then needed corrected embedded request quoting and explicit startup polling. These assertions/startup failures were corrected without modifying worker behavior. Each attempt restored service availability and cleaned up its own disposable IDs; the final command above exited 0.

## Owner's independent verification

On 10 November 2026 the repository owner supplied terminal output from an independent rerun:

```text
uv run --locked --offline pytest -q -o faulthandler_timeout=20 tests/controller/test_public_api.py
47 passed, 1 existing dependency deprecation warning in 9.28s

uv run --locked --offline pytest -q -o faulthandler_timeout=20
179 passed, 1 existing dependency deprecation warning in 29.06s

docker compose ps
controller and worker1–worker4 healthy before the probe

python3 tests/controller/check_a4_services.py
all reported checks passed; controller healthy at completion
```

The owner's real-service object was `a7380912-566e-458c-a839-47fc667abe3c`, with 2,000,128 bytes and SHA-256 `fa758c84e15620affff7e202d7f5b3e9d51577350c1c8d316bb4348bc6e5adca`. The result reported zero PATCH chunk writes; passing metadata/list/empty/error, offline deletion, pending retry, interrupted cleanup, lost acknowledgement, survivor/restart and all-four durable tombstone checks; delayed PUT status 409; controller epochs 12 → 13; and one existing object preserved. These are owner-provided results, separate from the agent-run evidence above. They confirm independent technical checks, without implying Friend's implementation review or joint C2 acceptance.

## Remaining acceptance and teammate dependencies

A4 is ready for Friend's review of API semantics, public error mapping, terminal ordering and reuse of B3 fencing. No new runtime dependency, worker change or packaging request is needed. B5 should use the direct custom map, full shared `ObjectMetadata`, `ObjectList` and pending-delete semantics documented here. C2 still requires B5 initial client/inspection and joint end-to-end acceptance; this handoff does not claim either person's new review or mark C2 DONE.

Invalid/stale receipts, deterministic PATCH/delete interleavings, interrupted execution and delayed controller receipt publication are fault-injected double tests. Real HTTP services verify ordinary metadata/bytes, concurrent updates, worker outage, durable cleanup, controlled acknowledgement loss, restart/rejoin and terminal PUT rejection. No actual host crash/power loss or disk failure was performed. The full serial repair scheduler, safe excess-copy retirement and repair/delete race acceptance remain A5/A6; B4 scanning and later portfolio work are unchanged.
