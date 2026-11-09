# Worker and deployment foundation

B1 packages the two FastAPI factories in one Python 3.12 image. B2 implements chunk PUT, GET, DELETE, and inventory. B3 adds automatic registration, heartbeats, and object tombstones. Both use the shared types from `src/common/`. Compose gives the controller and each of `worker1` through `worker4` its own named volume. Only the controller is published, on `127.0.0.1:8000` by default. Set `OBJECT_STORE_PORT` to change the host port.

## Run locally

Use Python 3.12, uv 0.13.7, and Docker with the Compose plugin.

```bash
uv sync --locked
uv run --locked pytest
docker compose config --quiet
docker compose up --build --wait
docker compose ps
curl -i http://127.0.0.1:8000/health/live
curl -i http://127.0.0.1:8000/health/ready
```

Compose healthchecks use liveness so workers can start before registration. With A2/A3 and B2/B3 together, the controller becomes ready when at least two workers reconcile. Each worker becomes ready after a heartbeat confirms its own `HEALTHY` state. A brief 503 during startup or restart is expected. Use `docker compose restart` for an ordinary restart or `docker compose down` to remove containers while retaining their volumes.

## Worker persistence

- The worker opens `catalog.sqlite3` and a `chunks/` directory under `OBJECT_STORE_WORKER_DATA_DIR`. It binds the catalog to `OBJECT_STORE_WORKER_ID`, persists its storage ID, and generates a new boot ID on startup. The local defaults are `worker1` and `.local-data/worker1`. Keep one worker process per volume.
- PUT checks length and SHA-256 and commits the pending placement and operation fence in SQLite before creating the staged file. That gives cleanup a durable owner for every staged chunk. PUT flushes the stage, atomically renames it, syncs the directory, and commits the valid state before issuing a durable receipt. Exact retries verify the file again. A newer PUT can repair a damaged copy with the original expected bytes.
- Startup finishes a pending PUT from a checked staged or published file and removes abandoned staging files. A pending placement without valid bytes appears as `suspect` and can be completed by a valid retry. GET checks the catalog expectations and hashes the actual bytes before returning them. Inventory also reports known `missing` and `corrupt` placements. The controller should verify bytes against its manifest on every transfer.
- DELETE first commits the retirement fence, then unlinks the files and records completion. This also works for an absent chunk. Startup completes interrupted retirements. The worker remembers the latest accepted controller epoch and checks ordering per chunk, allowing operations on different chunks to arrive out of sequence.

## Registration and terminal cleanup

- Compose sets `OBJECT_STORE_CONTROLLER_URL=http://controller:8000`. A standalone worker with the setting unset can serve local storage tests and stays unready. The worker starts one registration task and sends heartbeats with free disk bytes at the interval returned by the controller. HTTP calls have a 2-second deadline. Retries back off from 0.25 to 4 seconds. A `registration_required` response triggers re-registration with the same boot identity.
- A registration or heartbeat advances the durable controller epoch, fencing older PUT and placement DELETE requests. Worker readiness returns 200 after a heartbeat confirms `HEALTHY` for the registered epoch. A failed exchange or a different controller epoch clears readiness. B4 will supply durable integrity reports to the existing heartbeat payload.
- `POST /internal/objects/{object_id}/tombstone` commits a fence for that object generation and hides its placements before unlinking any files. Cleanup removes the canonical and staged files and commits completion before the worker sends its receipt. Startup completes interrupted cleanup before attempting to recover staged PUTs. Within an epoch the controller retries the same operation. After a controller restart, it can reissue unfinished cleanup with a higher epoch and sequence and a fresh operation ID. The worker keeps the original fence and retry history and returns a receipt for the requested intent. Replaying an older terminal intent never lowers the worker's epoch fence.

## Independent worker verification

- With the controller foundation at `99cf592` and the worker changes applied, `uv run --locked --offline pytest -q -o faulthandler_timeout=15` passed **75 tests** (56 controller and 19 worker) on Python 3.12.7. The suite emitted the existing Starlette TestClient warning about HTTPX. Compile and Compose config checks passed.
- The five-service Compose check on Docker Engine 29.8.0 and Compose 5.5.1 verified four automatic registrations and heartbeats. It used isolated container names, volumes, and a separate image tag to keep the existing local deployment available. `worker2` restarted with the same storage ID, a new boot ID, and the same 1,000,000-byte chunk. After controller restart, all four workers re-registered and sent fresh heartbeats. Readiness stayed 503 against the A2 foundation.
- In the same Compose check, an old-epoch PUT was rejected. A tombstone from the previous epoch cleaned up the chunk, its retry returned the same receipt, and a later PUT for the deleted generation was rejected.
- A B2 catalog created by commit `99cf592` opened under the updated worker with the same storage ID and intact bytes. A separate local check recorded four actual worker receipts in the current A2 metadata store and read back both copies of two chunks totaling 1,000,017 bytes.

## Joint C1 verification

Tested the worker changes together with controller commit `15df687` in an isolated five-service Compose deployment. The combined `uv run --locked --offline pytest -q` suite passed **132 tests** with the existing Starlette TestClient warning. Compile, Compose config, and diff checks passed.

- The controller and all four workers returned 200 on readiness. A public multipart POST returned 201 for a 2,000,123-byte binary object. Public GET returned the original bytes and SHA-256 `f149b388751f9a97e52870fdec028275d827b4ce664ec57789b69848160e4f27`.
- The three chunk lengths were 1,000,000, 1,000,000, and 123 bytes. The worker pairs were `worker1/worker2`, `worker2/worker3`, and `worker3/worker4`. Each of the six placements was confirmed in a real worker inventory and read back from that worker.
- After restarting `worker2`, only its boot ID changed. After restarting the controller, its epoch advanced from 1 to 2. Both restarts recovered readiness and preserved the public download and all six checked copies.
- The restart check used a second disposable object and simulated a lost cleanup acknowledgement by recording a pending cleanup intent and applying it to one worker while the test controller was stopped. After startup the controller reissued the intent under epoch 2. All four workers acknowledged it, including the worker that had already applied the original tombstone. The deleted object returned 404 and the other object was intact.

The C1 technical gate passed. Joint human review remains open.
