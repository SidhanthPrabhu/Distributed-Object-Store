# Course object store client

The B5 client uses the public controller API. It runs with the existing Python 3.12 project and HTTPX dependency. The default controller address is `http://127.0.0.1:8000`. Set `OBJECT_STORE_URL` or pass `--url` before the command to use another local deployment.

```bash
uv run --locked python -m client health
uv run --locked python -m client upload sample.bin --metadata '{"course":"CS838"}'
uv run --locked python -m client list
uv run --locked python -m client metadata "$object_id"
uv run --locked python -m client patch "$object_id" '{"reviewed":true}'
uv run --locked python -m client inspect "$object_id"
uv run --locked python -m client download "$object_id" copy.bin --sha256 "$expected_sha256"
uv run --locked python -m client delete "$object_id"
uv run --locked python -m client status
```

Set `object_id` to the ID returned by upload. The optional `--sha256` argument takes the digest of the original file, for example from `shasum -a 256 sample.bin`. Download always checks the response length and prints the computed digest. It stages the download beside its destination and publishes the complete file after validation. Choose a destination that does not already exist.

Commands write JSON to standard output on success. Controller errors go to standard error with the HTTP status, error code, and request ID when supplied. A pending deletion exits with an error so a script can distinguish it from complete cleanup.

`inspect` shows the controller's recorded chunk placements. `health` checks controller readiness. `status` shows worker states, replication deficits, and queued, running, or blocked repair work. A worker that is returning can still appear in the manifest while the controller checks and retires extra copies.

## Checks

The local CLI checks use HTTPX test responses. The opt-in C2 check uses an independent binary fixture and SHA-256, exercises all six public routes, compares chunk locations before and after PATCH, and checks invalid and missing-object errors.

```bash
uv run --locked pytest -q tests/integration/test_client_cli.py
OBJECT_STORE_C2_TEST_URL=http://127.0.0.1:8000 uv run --locked pytest -q tests/integration/test_c2_live.py
```

Run the second command against a disposable, ready deployment with A4. It creates and deletes a test object. With the environment variable unset, pytest skips that check. Actual repair status and failure demonstrations follow A5.

On 10 November 2026, `uv run --locked --offline pytest -q -o faulthandler_timeout=20` passed 142 tests with one opt-in live test skipped and the existing Starlette TestClient deprecation warning. `uv run --locked --offline python -m client --help` started the installed CLI and listed all nine commands.

## Integrated C2 verification

On 10 November 2026, B5 was tested with A4 commit `94315b8`. The combined local suite passed **189 tests**, with the opt-in live case skipped and the existing Starlette TestClient warning. CLI help and Compose configuration validation passed.

The opt-in C2 test passed separately at `http://127.0.0.1:18083` in an isolated five-service Compose project. The CLI exercised all six public operations, inspected two recorded locations for each chunk, and reproduced the 1,000,123-byte input and its independently calculated SHA-256 after PATCH. The test confirmed the updated custom fields, unchanged chunk metadata, invalid PATCH returning 400, and the deleted object returning 404 and disappearing from the list.

The controller owner's [A4 probe](../tests/controller/check_a4_services.py) ran against the same isolated project. It verified six worker placements for a separate three-chunk object, zero worker chunk writes during concurrent PATCH, a byte-identical download, and durable deletion on all four workers. It also stopped an isolated worker and the controller in sequence, checked pending deletion, simulated a lost cleanup acknowledgement with the controller stopped, and verified cleanup after controller epoch 3 advanced to 4. The delayed PUT returned 409. The test containers were removed and their named volumes retained.

C2 technical acceptance was recorded on 18 November 2026. C3 adds the controller status route and live repair checks. See the [tracker](COURSE-TRACKER.md#joint-milestone-gates).
