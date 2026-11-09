# Worker and deployment foundation

B1 packages the two FastAPI factories in one Python 3.12 image. B2 implements chunk PUT, GET, DELETE, and inventory using the shared types from `src/common/`. Compose gives the controller and each of `worker1` through `worker4` its own named volume. Only the controller is published, on `127.0.0.1:8000` by default. Set `OBJECT_STORE_PORT` to change the host port.

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

Compose healthchecks use liveness so workers can start before registration. In this slice the controller and all four workers return 503 on readiness. Use `docker compose restart` for an ordinary restart or `docker compose down` to remove containers while retaining their volumes.

## Worker persistence

- The worker opens `catalog.sqlite3` and a `chunks/` directory under `OBJECT_STORE_WORKER_DATA_DIR`. It binds the catalog to `OBJECT_STORE_WORKER_ID`, persists its storage ID, and generates a new boot ID on startup. The local defaults are `worker1` and `.local-data/worker1`. Keep one worker process per volume.
- PUT checks length and SHA-256, flushes a staged file, and commits the pending placement and operation fence in SQLite before an atomic rename. It syncs the directory and commits the valid state before issuing a durable receipt. Exact retries verify the file again. A newer PUT can repair a damaged copy with the original expected bytes.
- Startup finishes a pending PUT from a checked staged or published file and removes abandoned staging files. A pending placement without valid bytes appears as `suspect` and can be completed by a valid retry. GET checks the catalog expectations and hashes the actual bytes before returning them. Inventory also reports known `missing` and `corrupt` placements. The controller should verify bytes against its manifest on every transfer.
- DELETE first commits the retirement fence, then unlinks the files and records completion. This also works for an absent chunk. Startup completes interrupted retirements. The worker remembers the latest accepted controller epoch and checks ordering per chunk, allowing operations on different chunks to arrive out of sequence.

## Verification

- The lock resolved 25 packages. `uv run --locked pytest -q` passed **69 tests** (56 controller and 13 worker) on Python 3.12.7. The suite emitted the existing Starlette TestClient warning about HTTPX. Compile and Compose config checks passed.
- `docker compose up --build --wait` started the real five-service topology on Docker Engine 29.8.0 and Compose 5.5.1. A request from the controller container reached each worker by service name. A PUT and checked GET through `worker2` returned the same 1,000,000 binary bytes before and after `docker compose restart`. The SHA-256 was `67870dfc9c64e7aa270a3f7e8051ae65d207f93fc3df04d7572e6365af69cd0d`.
- Four storage IDs survived the restart, four boot IDs changed, and the controller epoch incremented. The smoke check posted the four real worker identities to the existing registration route and verified the same identity records in the controller database after restart. All five services returned 200 on liveness and 503 on readiness before and after.

## Integration handoff

B3 will add automatic registration, heartbeat and readiness transitions, and the terminal object-generation tombstone. The controller owner needs to finish and push A2 inventory fetching, cleanup replay, reconciliation, and HEALTHY eligibility. Use the existing worker inventory route and keep controller manifest checksums authoritative. After A2 and B3 are on main, rerun with automatic registration and demonstrate controller readiness and worker rejoin. A3 owns public upload/download and two-copy placement.
