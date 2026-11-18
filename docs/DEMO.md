# Course walkthrough

Run the walkthrough from the repository root with Docker Compose and the locked Python 3.12 environment. The walkthrough stops and restarts services and changes a byte in a file it uploads, so use a disposable local deployment. It removes its test objects and retains the named volumes.

```bash
docker compose config --quiet
docker compose up -d --build --wait
docker compose run --rm client health
uv run --locked python scripts/demo/course_walkthrough.py --disposable
```

Docker health checks confirm the processes are live. The walkthrough polls the status route for four reconciled workers before uploading anything. The script uses the controller port published by the same Compose project. If readiness is taking longer, inspect `docker compose ps`, `docker compose logs controller worker2`, and `docker compose run --rm client status`.

| Demo | What to look for |
|---|---|
| 1. Upload and download | The downloaded binary file matches the original 2,500,000 bytes and independently calculated SHA-256. |
| 2. Chunking and distribution | The manifest reports chunk lengths of 1,000,000, 1,000,000, and 500,000 bytes, with placements across all four workers. |
| 3. Replication | Read-only worker snapshots verify two actual files per chunk and both hashes against the manifest. |
| 4. Metadata | PATCH changes custom keys and the revision. Every worker placement sequence and data hash stays the same. |
| 5. Worker failure | The script stops worker2 and verifies the original download. It waits for two checked copies per chunk while worker2 is still down, then starts worker2 and checks that excess copies are removed. |
| 6. Data integrity | The script flips one byte of a chosen local chunk file. It waits for a durable fault report and a repaired file with the original hash before downloading again. |

The walkthrough also checks controller restart persistence and HTTP 503 when both copies are offline. It stops both replica holders while the controller is stopped so a replacement cannot be created between the two stops. After starting both workers, it waits for their new boot IDs and for the object to recover, then checks the original bytes and both replica files. Focused tests cover a lost repair reply and deletion overlapping an in-flight repair. See the [course tracker](COURSE-TRACKER.md) for recorded results.

## Try the CLI directly

```bash
uv run --locked python -m client upload ./sample.bin --metadata '{"course":"CS838"}'
uv run --locked python -m client inspect "$object_id"
uv run --locked python -m client patch "$object_id" '{"reviewed":true}'
uv run --locked python -m client download "$object_id" ./returned.bin --sha256 "$expected_sha256"
uv run --locked python -m client status
uv run --locked python -m client delete "$object_id"
```

Use the object ID from upload and the SHA-256 of your original file from `shasum -a 256 ./sample.bin`. The container client supports the same commands. To upload a file with it, bind mount the input directory using `docker compose run --rm -v "$PWD:/work:ro" client upload /work/sample.bin`.

The worker mutation utility accepts an object ID and chunk ID and checks the local catalog before touching the corresponding file. To run it by hand on a disposable object, pick its worker from `inspect` and set the opt-in for that one invocation:

```bash
docker compose exec -T -e OBJECT_STORE_ALLOW_FAULT_INJECTION=1 worker2 \
  python -m worker.corrupt "$object_id" "$chunk_id"
```

Replace `worker2` with an actual location for the selected chunk. To stop the stack after the walkthrough, run `docker compose down`. Data remains in the named volumes.
