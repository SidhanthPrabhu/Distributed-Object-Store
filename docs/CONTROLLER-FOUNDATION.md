# A1 and independent A2 foundation

This records the original foundation slice. See the [A2/A3 continuation handoff](CONTROLLER-A2-A3.md) for current inventory reconciliation, upload/download, recovery behavior, checks, and B1/B2/B3 integration dependencies.

This handoff records the controller foundation as of 8 November 2026. The [course tracker](COURSE-TRACKER.md#joint-milestone-gates) records the later integration and acceptance results.

## Available interfaces

Python uses the agreed `src/` layout. The factory is `controller.app:create_app`; importing or creating the application does not open a database. Lifespan startup opens SQLite, increments a durable controller epoch, and opens one bounded HTTPX asynchronous client. Shutdown closes the client and database, including when client initialization fails. Sync metadata/registry routes execute in FastAPI's thread pool; transactions contain no network operations.

| Interface | Implemented behavior |
|---|---|
| `GET /health/live` | 200, `{"status":"live"}` |
| `GET /health/ready` | 503 `storage_unavailable` until at least two reconciled workers are eligible; resource failures also prevent readiness |
| `POST /internal/workers/register` | Contract 200 response, configured names only, persistent storage binding, fresh boot tracking, `RECONCILING` |
| `POST /internal/workers/heartbeat` | Contract 200 response; unknown/stale/unregistered boot gets 409 `registration_required`; fault reports persist transactionally before acknowledgement |

No public object or admin-status routes are implemented yet. Registration and heartbeat do **not** currently fetch inventories, run cleanup, or change a worker to HEALTHY. Consequently readiness intentionally remains 503 in this slice, even after four registrations. B1 must let workers start registration using controller liveness/service startup; waiting for controller readiness first would create a startup dependency cycle.

`common.models` provides the shared types for B2/B3/B4/B5:

- `WorkerRegistration`, `RegistrationResponse`, `WorkerHeartbeat`, `HeartbeatResponse`, `IntegrityReport`, `WorkerIdentity`, and `WorkerState`.
- `WorkerInventory` and `InventoryChunk`. The current contract does not enumerate catalog-state strings, so `state` accepts a bounded nonempty string. Agree on B2's state vocabulary during review; no catalog state proves current byte integrity.
- `MutationHeaders`, `ChunkPutHeaders`, `ChunkReadHeaders`, and their `to_headers()` helpers; chunk IDs remain URL parameters. A worker still validates lengths against its configured chunk size and actual bytes against SHA-256.
- `ChunkReceipt`, `TombstoneRequest`, and `TombstoneReceipt`. Receipts require durable/cleanup success. Tombstone responses carry object, generation, operation, epoch/sequence, and worker/storage/boot identity.
- `ObjectMetadata`, `ChunkMetadata`, `Checksum`, `ObjectSummary`, and `ObjectList`. Manifests validate UUIDs, lowercase SHA-256, ordered unique chunks, exact lengths, distinct locations, UTC timestamps, and empty-file semantics. Custom metadata permits JSON values under string keys. Integers are bounded to SQLite's signed 64-bit range.

`common.errors.APIError` and `install_error_handlers(app)` let the worker reuse the same envelope. Validation errors become 400 `invalid_request`; unknown routes get JSON 404; explicit errors keep stable codes; unexpected errors get a sanitized 500. Each response carries an `X-Request-ID` UUID matching errors; a supplied valid UUID is preserved. Request input values are not echoed in validation errors.

These models implement the existing contract. Human agreement and cross-service checks are still pending; importing models is not evidence that a worker stored a chunk.

## Configuration and B1 dependencies

`common.config.StorageConfig` provides decimal `chunk_size_bytes=1000000` and fixed `replication_factor=2`; workers can reuse it. `ControllerConfig.from_env()` reads:

| Environment variable | Default |
|---|---|
| `OBJECT_STORE_DATABASE_PATH` | `.local-data/controller/metadata.sqlite3` |
| `OBJECT_STORE_WORKER_ADDRESSES` | JSON object mapping worker1–worker4 to `http://workerN:8000` |
| `OBJECT_STORE_CHUNK_SIZE_BYTES` | `1000000` |
| `OBJECT_STORE_HEARTBEAT_INTERVAL_SECONDS` | `1.0` |
| `OBJECT_STORE_SUSPECT_AFTER_SECONDS` | `3.0` |
| `OBJECT_STORE_UNAVAILABLE_AFTER_SECONDS` | `5.0` |
| `OBJECT_STORE_HTTP_TIMEOUT_SECONDS` | `5.0` |
| `OBJECT_STORE_HTTP_MAX_CONNECTIONS` | `16` |

Configure a persistent controller volume at the database parent directory. Configured worker origins must be distinct HTTP(S) addresses without paths, credentials, queries, or fragments. Registration cannot override them. HTTPX disables environment proxies and redirects, uses explicit timeouts, and owns a bounded connection pool. Only one controller application process may own the database; this is not a multiprocess coordination design.

B1 owns packaging and the final tested version lock. Controller runtime imports require FastAPI, Pydantic 2, and HTTPX; SQLite is from Python's standard library. Uvicorn supplies the agreed server entry point; pytest is needed for these checks. A3 will additionally need multipart parsing. No dependency declaration or lock was created here.

Local checks used a disposable environment at `/tmp/cc-controller-foundation-venv`, Python 3.12.3, SQLite 3.45.1, FastAPI 0.162.2, Pydantic 2.14.3, HTTPX 0.28.1, pytest 9.2.0, Uvicorn 0.54.0, Starlette 1.10.0, and AnyIO 4.16.0. This is tested-version evidence, not a lock or a compatibility claim for other combinations. Starlette emitted one deprecation warning recommending `httpx2` for its test adapter; B1 should choose and verify a compatible dependency set. Production HTTPX usage follows the course agreement.

## Persistence boundaries

`controller.metadata.MetadataStore` uses schema version 1, foreign keys, WAL, full synchronous durability, a busy timeout, and short serialized transactions. It stores objects, ordered chunks, replica receipts, workers/retired boots, operation intents, tombstones, all-four-worker cleanup needs, and pending integrity reports. Metadata has no method to replace an expected checksum from inventory. The configured address map remains configuration rather than worker-supplied data.

An upload starts hidden as UPLOADING. PUT intents allocate durable globally increasing sequences before storage work. `record_receipt()` checks all identity, hash, length, epoch/sequence, and worker fields against the recorded intent, rejects stale completions, and treats retries as one placement. `publish_upload()` checks two distinct durable placements for each chunk, rejecting receipts from an earlier controller epoch or worker boot. These are local persistence gates: the future upload coordinator must additionally select freshly eligible workers and verify actual responses. Unit-test receipts do not prove real durability.

`begin_deletion()` atomically hides an object, records a terminal tombstone with a newer sequence, and creates cleanup needs for all four workers. Existing PUT intents remain available to recovery code, but late receipts/new PUT intents/publication cannot revive the object. Cleanup acknowledgements, public DELETE, and remote storage effects are not implemented yet.

Heartbeat freshness uses controller-local monotonic time. Registry snapshots age registrations through SUSPECT and UNAVAILABLE; neither is eligible. Restart retains diagnostic wall timestamps, identities, manifests, reports, and pending operations, while discarding process-local reachability. Workers must register again. A new boot resets capacity/heartbeat observations and invalidates old-boot completions. Storage rebinding and retired-boot re-registration are rejected with 409; replacing a worker volume needs an explicit future recovery policy.

Fault reports are acknowledged only after their complete batch commits. Duplicate IDs with identical identity/content are idempotent; conflicting IDs roll back the batch. Persisted reports are outstanding reconciliation/repair needs, **not completed repairs**. Reports can describe unknown chunks without creating manifests. B4's sender must retain/coalesce outstanding local faults and repeat unacknowledged reports as specified in the contract.

## Checks and next slice

Executed:

```text
PYTHONPATH=src /tmp/cc-controller-foundation-venv/bin/python -m pytest -q -o faulthandler_timeout=20 tests/controller
56 passed, 1 dependency deprecation warning

python3 -m compileall -q src tests/controller
passed

git diff --check
passed (tracked changes; new source files also checked for whitespace separately)
```

The restricted runner passed 45 initial metadata/model/registry checks, but stalled when TestClient started its background event loop. Those stalled runs were interrupted. The complete suite passed with execution permission outside that restriction; the final run includes two additional restart/publication checks. Tests use temporary SQLite databases, synthetic receipts, FastAPI TestClient, and an HTTPX mock transport. No real worker or Compose/inventory/restart demo passed by implication.

Next: finish A2 inventory fetching/retry, validate the responding worker/storage/boot identity, compare inventory against controller intents without rewriting expected content, reapply pending tombstones/retirements, and reconcile before HEALTHY. Respect `integrity_reports_more` while draining reports and pending fault work. Add bounded retry/recovery for interrupted uploads and ambiguous PUTs; old-epoch intents are retained but cannot be directly committed. Persisted cleanup needs still need remote acknowledgement handling. A5 supplies serial repair execution and safe excess-copy retirement.

Integration requires B1's installable packages/lock and persistent deployment, B2's real inventory/checked GET/PUT/DELETE with durable receipts, and B3's persistent identity, new-boot registration, heartbeat retries, and tombstone cleanup. Then exercise four distinct workers, expiry/removal from candidates, controller/worker restart, correct inventories, stale replies, and cleanup before starting A3's public upload/download slice. C0 additionally requires both people's contract review and a real durable chunk PUT/GET; C1 requires the real two-copy object path.
