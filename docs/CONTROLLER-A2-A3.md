# A2/A3 controller handoff

9 November 2026. Controller-owned A2 reconciliation/recovery and A3 upload/download are **IN REVIEW**. Human review and real-service acceptance remain pending. Work extends the existing A1/A2 foundation on `main`; no commit, push, branch, PR, or remote issue was created. Worker code, packaging, locks, deployment, client tools, and CI remain absent and were not created here. C0 and C1 remain open.

## Behavior available

The existing `controller.app:create_app` factory and shared imports remain available. Registration always returns the contract's `RECONCILING` response. An owned asynchronous maintenance task fetches inventory from the configured address, applies pending terminal cleanup, compares catalog identity/content/sequence with controller manifests and intents, and freshly checks known ACTIVE placements before granting HEALTHY. Heartbeats refresh reachability; they cannot bypass reconciliation. Expiry, re-registration, new boots, new fault reports, and report backlog invalidate reconciliation. A late heartbeat after expiry cannot restore eligibility by itself. `integrity_reports_more=true` holds the worker in reconciliation until the backlog is drained.

Inventory responses must match worker, storage, and boot identities. Requests have explicit HTTPX connection/idle limits, an overall per-attempt deadline, bounded response sizes, no redirects/proxies, and at most two attempts by default. Network failures, 5xx, and 429 can retry; retries preserve mutation headers and IDs. Identity/content conflicts never replace authoritative expected hashes. Unknown or ambiguous ACTIVE placements keep the worker RECONCILING for later recovery; this slice does not invent manifests, adopt an inventory as a durable receipt, or delete a possibly last valid extra copy.

A missing or unreadable known placement is recorded durably as a placement fault and excluded from read candidates. Resolving that discrepancy into a persisted fault completes the inventory comparison; the worker can still become HEALTHY for other placements/new writes. HEALTHY does not certify every catalog entry. A new reconciliation can clear a placement fault only after a successful fresh length/hash/header check and a current reconciliation token. Integrity reports and unresolved placement faults remain durable inputs for A5; this slice does not run repairs.

New public routes:

| Route | Behavior |
|---|---|
| `POST /api/v1/objects` | Multipart `file`, optional `metadata` JSON-object string; 201 with the contract's full `ObjectMetadata` |
| `GET /api/v1/objects/{object_id}` | 200 with verified original bytes, stored content type and exact content length |

Uploads assign independent UUID object/generation/chunk IDs, preserve filename as display metadata, and use ordered 1,000,000-byte chunks by default. Empty files have zero chunks and SHA-256 of empty bytes. The multipart parser spools file data to disk; the coordinator scans it in chunk-sized buffers, records the authoritative manifest, then rewinds for transfer. Placement rotates the starting worker through worker1–worker4, skipping workers without fresh HEALTHY eligibility or sufficient reported capacity. The rotation cursor persists across restart. Successful writes decrement the local capacity estimate until a new heartbeat supplies capacity. Two distinct durable receipts per chunk are required before visibility. Zero-chunk empty objects need no synthetic placements.

Each remote PUT has a committed intent/epoch/sequence first. Retries reuse the exact operation identity. Receipts must match the specific requested intent, worker/storage/boot, hash/length, epoch/sequence and boolean `durable: true`. Eligibility is checked after writes and again before publication. Incomplete, failed, cancelled, and interrupted uploads remain hidden from ordinary `get_manifest()` and public reads. Public list/metadata/delete routes remain A4 work.

Failure or cancellation records a terminal tombstone and all-four-worker cleanup needs. Startup converts any leftover UPLOADING object to the same durable cleanup path, including writes whose replies never arrived. Cleanup uses B3's existing tombstone route, validates complete receipts and current worker identity, and retries pending acknowledgements. Offline workers remain excluded until their cleanup/reconciliation succeeds. Within an epoch the tombstone identity stays stable; restart refreshes a pending old-epoch tombstone once with a newer sequence/operation before remote replay. Old PUT completions cannot publish or recreate visibility. A worker must enforce the corresponding remote fence. Completed cleanup retires pending operations and replica records while retaining original manifest/history. No public DELETE API was added.

Downloads use only freshly eligible workers and non-faulted placements, verify length/SHA-256 and response headers against the manifest, and try the next copy on failure. The complete object is assembled in an anonymous temporary file, then checked against the original total size and object hash before constructing a success response. An aggregate byte reservation covers all outstanding download spools until their responses finish. Failure, disconnect, send failure and cancellation close the spool and release its reservation. A missing later chunk therefore returns a JSON 503 before any 200/binary body is sent. Quota or disk failure also returns 503. Unknown/hidden UUIDs return 404; invalid IDs, malformed multipart/metadata and configured upload limit violations return 400. All errors use the existing request-ID envelope.

## Persistence and operational bounds

Schema version 2 adds only durable placement faults and placement rotation. Versions 0/1 upgrade without replacing existing manifests, receipts, worker bindings, pending operations, epochs or sequences. Expected checksums still come exclusively from original controller manifests/intents. Restart removes process-local eligibility; workers register again. SQLite retains WAL/full synchronous transactions and one-process ownership. Network operations occur outside transactions.

Transfer preparation and reconciliation are serialized through one asynchronous lock. Registration/heartbeats continue while remote requests are awaited. Reconciliation tokens fence identity changes, expiry and fault-report changes during a request. Upload/download memory is proportional to one bounded chunk plus manifest/catalog metadata, rather than file length. Responses stream from the fully verified spool in 64 KiB blocks. The inventory body is capped before model parsing; manifests and inventories themselves still require metadata proportional to chunk count. This is a small course implementation, not a throughput scheduler.

Additional controller-only environment settings (existing foundation settings remain supported):

| Variable, prefixed `OBJECT_STORE_` | Default | Meaning |
|---|---|---|
| `HTTP_ATTEMPTS` | `2` | 1–3 attempts per worker request |
| `RECONCILIATION_INTERVAL_SECONDS` | `1.0` | Retry interval; registration/heartbeat also wake maintenance |
| `INVENTORY_MAX_BYTES` | `8000000` | Maximum inventory response bytes |
| `UPLOAD_MAX_BYTES` | `1000000000` | Maximum file bytes; multipart request has an additional 64 KiB envelope allowance |
| `DOWNLOAD_TEMP_QUOTA_BYTES` | `1000000000` | Aggregate outstanding verified-download spool bytes |
| `TEMPORARY_DIRECTORY` | system temporary directory | Directory for anonymous download spools |

Multipart supports one file and at most one metadata field, with a 64 KiB non-file part limit. Starlette's multipart upload spooling uses the system temporary directory; its disk use is bounded per request by the upload limit, separately from the aggregate **download** quota. Multiple simultaneously parsing uploads do not share that download quota. Configure upload limits and service admission sensibly for the course deployment. Anonymous download files disappear when closed or the process exits. The configured directory must already exist and have sufficient disk space.

No method/path/payload change to the agreed worker contract was necessary. Receipt success flags now reject numeric `1` as well as false/nonboolean values: the existing contract requires JSON boolean `true`. Catalog state vocabulary remains unconstrained as in A1; fresh checked GETs, rather than a guessed state spelling, establish byte validity.

## Actual checks

All checks below used temporary SQLite databases, in-memory contract worker doubles via HTTPX MockTransport, and FastAPI TestClient. None used worker filesystem storage, real workers, Compose, containers, or deployment. The doubles deliberately inject lost replies, bad receipts, identity changes, missing/corrupt/truncated/oversized responses, and cleanup failures. Their success cannot establish C0/C1 or remote durability/fencing.

```text
PYTHONPATH=src /tmp/cc-controller-a3-venv/bin/python -m pytest -q -o faulthandler_timeout=20 tests/controller
113 passed, 1 dependency deprecation warning in 22.06s

python3 -m compileall -q src tests/controller
passed

git diff --check
passed
```

The suite covers decimal boundaries 0/1/C−1/C/C+1/2C/2C+1, binary data, ordered rotating pairs, UUID uniqueness, empty files, capacity exhaustion, hidden incomplete uploads, invalid receipts, immutable expected hashes, inventory identity conflicts, worker expiry, corrupt/missing fallback and no-valid-copy errors, later-chunk failure before response success, full-object verification, download quota/disk/send-failure cleanup, lost-reply retries, cancellation after remote effect, startup orphan cleanup, current-boot cleanup receipts, terminal fencing, stale reconciliation completion, automatic reconciliation, report backlog, response limits/deadlines, schema migration, epoch/sequence persistence and continued reads after controller/worker boot changes. The original foundation checks are retained.

Runtime tested here: Python 3.12.3, SQLite 3.45.1, FastAPI 0.162.2, Pydantic 2.14.3, HTTPX 0.28.1, pytest 9.2.0, python-multipart 0.0.33, Uvicorn 0.54.0, Starlette 1.10.0, AnyIO 4.16.0. One Starlette deprecation warning recommends `httpx2` for its test adapter. This temporary environment is test evidence, not a dependency lock. The restricted runner passed 47 non-TestClient foundation checks, but its full run stalled while starting TestClient's event loop and was interrupted. Complete suites ran with the required test execution permission; do not interpret the interrupted run as a failed application assertion.

## Exact teammate dependencies and acceptance still open

- **B1:** Package/install `common` and `controller` from `src/`, lock a compatible FastAPI/Pydantic 2/HTTPX/Starlette set, and include **python-multipart** (new A3 runtime dependency), Uvicorn and pytest. Supply the factory startup, one controller process, four configured distinct worker origins, persistent controller/worker volumes, sufficient temporary disk, and build/test/Compose commands. No packaging files or lock were edited here.
- **B2:** Implement real PUT/checked GET/inventory routes with the existing models/headers and durable receipts. Inventory must report authoritative catalog identity/expected fields/operation sequences; GET must return exact bytes and matching `X-Chunk-SHA256`/`X-Chunk-Length`. Confirm idempotent retry after reply loss, disk flush/publication/receipt persistence, startup storage recovery, capacity errors, and read integrity against real files. B2's chunk DELETE remains for later A5 retirement; failed-upload cleanup here uses B3 tombstones.
- **B3:** Supply persistent storage identity, fresh boot identity, registration and bounded heartbeat retries (including report backlog/draining), and durable `POST /internal/objects/{object_id}/tombstone` cleanup receipts carrying current worker/storage/boot identity. Tombstones must reject delayed old PUTs and remove catalog/staging/canonical data before acknowledgement. Start registration using controller liveness/service startup; waiting for controller readiness creates a dependency cycle.

Human contract/implementation review remains pending. Run C0 with real packaged applications and one durable PUT/GET, then C1 with four persistent workers, >1 MB object round-trip and actual two-copy placement. Also repeat controller/worker restart, orphan/reply-loss cleanup, identity conflicts, expiry, invalid receipts, partial visibility and corruption fallback against real storage before accepting the slices. Test doubles cannot satisfy those gates.

Next: integrate A2/A3 with B1/B2/B3 and collect real C0/C1 evidence. The next controller card is A4 (metadata/list/PATCH/public deletion); A5 then consumes persisted fault/operation needs for serial repair and verified excess-copy retirement. Full public delete/metadata APIs, the repair scheduler, admin status, and portfolio features were not implemented here.
