# Course project: two-person implementation plan

Recorded 8 November 2026. **You** means the repository owner's controller role; **Friend** means the teammate's worker/deployment role, even when that teammate is the person talking to Codex. These labels can be replaced with GitHub handles later. See the [tracker](docs/COURSE-TRACKER.md) for actual implementation/review status and outstanding integration gates. Use the [Codex starting prompts](docs/CODEX-START.md) to hand each role to a fresh chat.

## What we are building

Implement the course version in [PROJECT-SCOPE.md](PROJECT-SCOPE.md): one controller, four persistent workers, all required REST APIs, ordered 1 MB chunks, two-copy replication, metadata, failure detection, automatic serial repair, periodic corruption checks, safe restart/deletion, and six assignment demonstrations. Use Python/FastAPI, HTTPX, SQLite, pytest, and Docker Compose as the working stack.

The [assignment](CC-research-handoff/references/assignment.pdf) defines requirements; the [compiled checklist](docs/ASSIGNMENT-REQUIREMENTS.md) puts them in one readable place with page references. The [course contract](docs/COURSE-CONTRACT.md) fixes our initial service boundary and startup targets. The research handoff is supporting design material, not a requirement to implement all its extensions.

## Ownership

| Area | You: controller and coordination | Friend: storage workers and deployment |
|---|---|---|
| File storage | Public upload/download routes, chunking, object manifest, distribution, reconstruction | Durable chunk files, chunk PUT/GET/DELETE, local inventory and receipts |
| Metadata | Controller SQLite database, object lifecycle, metadata/list/PATCH | Worker catalog, persistent worker identity, fresh boot identity |
| Monitoring | Registration/heartbeat receiver, health decisions, inventory reconciliation | Registration/heartbeat sender, retries, local health/readiness |
| Integrity | Trusted expected checksums; checked fallback; enqueue repair | Actual file checks; periodic local scanning; damage reports; controlled byte-mutation tool |
| Recovery | One serial repair loop, destination selection, safe placement commit, rejoin/excess-copy decisions | Atomic replacement, idempotent operations, local startup recovery, tombstone enforcement |
| Deletion | Logical deletion, durable tombstone, cleanup coordination and retries | Persist tombstone, remove data, reject stale writes and report cleanup completion |
| Usability | Public API examples, architecture/data-flow explanation | CLI client, placement inspection, Compose, six-demo runner |
| Verification | Controller unit/integration tests and repair/restart race checks | Worker storage tests, deployment checks, end-to-end demo harness |

This is a service boundary, not a knowledge boundary: both people must understand upload, download, failure, corruption, and repair before the viva. Controller integration has more coordination work; the friend's ownership of deployment and demonstration tooling balances that with the worker storage implementation.

## Proposed files and who changes them

These paths define ownership for implemented foundations and later implementation targets.

| Paths | Primary owner | Coordination rule |
|---|---|---|
| `src/common/` | You | Friend reviews any change to wire models, IDs, hashing rules, or configuration used by workers |
| `src/controller/`, `tests/controller/` | You | Friend reviews API-to-worker interactions and recovery behavior |
| `src/worker/`, `tests/worker/` | Friend | You review publication, checksums, and stale-write protection |
| `src/client/`, `scripts/demo/`, `tests/integration/` | Friend | You supply controller fixtures and help diagnose cross-service failures |
| `pyproject.toml`, `uv.lock`, `Dockerfile`, `compose.yaml`, `.github/workflows/` | Friend | You declare controller dependencies/configuration before the dependency lock is changed |
| `docs/COURSE-CONTRACT.md` | You | Both review every contract change; update affected services together |
| `docs/DEMO.md` | Friend | Both run the final walkthrough |
| `docs/ARCHITECTURE.md` | You | Friend contributes worker persistence/restart details |
| `README.md`, `PROJECT-SCOPE.md`, `TEAM-PLAN.md`, `docs/COURSE-TRACKER.md` | Shared | Keep edits small; coordinate directly with the teammate to avoid simultaneous rewrites |

Use the same application image with different controller/worker entry points if practical. Do not create a framework of scheduler plugins or resume-only modules in the course implementation.

## Task cards

Each ID below can become one GitHub issue. Copy its description and acceptance criteria into the included issue template. A larger card may use several small commits; put the card ID in commit messages. The [tracker](docs/COURSE-TRACKER.md) is the progress source of truth until actual GitHub issues are linked there.

### A1 — Shared models and controller skeleton — You

Create `src/common/` wire/configuration models and the controller application entry point. Implement the documented error shape, ID checks, health endpoints, and HTTPX client lifecycle. Agree with Friend on the initial contract before either service depends on it.

**Done when:** both applications can import the same models; invalid request/error behavior is consistent; service startup/shutdown closes owned resources. **Dependency:** none. **Reviewer:** Friend.

### B1 — Project packaging and local deployment — Friend

Create packaging, the tested dependency lock, Dockerfile, and Compose configuration for controller plus worker1–worker4. Give every service its own persistent volume, with internal service discovery and sensible healthchecks. Keep literal container names `worker1` through `worker4` for the required commands. Add a test command to CI once tests exist.

**Done when:** a clean checkout builds and starts five services; worker storage identities and controller metadata survive ordinary restart; readiness reflects registration/storage state. Containers may start before all are ready, with registration retries. **Dependency:** coordinate entry points with A1; packaging work can start immediately. **Reviewer:** You.

### A2 — Durable metadata and worker registry — You

Implement controller SQLite manifests, object/chunk/replica records, pending operations, tombstones, and persistent operation sequencing. Receive registration and heartbeats, validate identities/addresses, obtain worker inventories, and reconcile before marking a returning worker eligible.

**Done when:** four distinct persistent workers register; expired heartbeats remove workers from placement/read candidates; restart retains manifests and pending work; inventory never overwrites trusted expected hashes. **Dependency:** A1; integrate with B2/B3. **Reviewer:** Friend.

### B2 — Durable worker chunk store — Friend

Implement the worker catalog and required chunk PUT/GET/DELETE/inventory routes. Stage writes, check length/hash, flush and atomically publish, persist receipts, detect damaged data, handle retries idempotently, and recover interrupted local publication. Use generated IDs for paths.

**Done when:** correct binary chunks survive restart; corrupt/truncated PUTs are rejected; damaged GETs return an error; repeated operations cannot create conflicting content; interrupted writes are reconciled. **Dependency:** A1 for shared models; can test storage functions independently. **Reviewer:** You.

### B3 — Worker registration, heartbeat, and cleanup — Friend

Implement persistent worker ID, per-start boot ID, register/retry/heartbeat loops, readiness, local inventory reconciliation, and the internal object tombstone/cleanup route. Persist tombstones before acknowledging them and prevent late PUTs from reviving deleted data.

**Done when:** restarted workers rejoin with their old storage identity and a new boot ID; disconnected registration retries are bounded; cleanup survives restart and fences old writes. **Dependency:** B2; integrate with A2. **Reviewer:** You.

### A3 — Upload and checked download — You

Implement multipart upload, UUID object/chunk IDs, decimal 1 MB chunking, rotating placement across healthy worker pairs, two durable copies before visibility, immutable manifest, checked fallback, and quota-limited complete-object spooling before download success. Track failed-upload cleanup.

**Done when:** arbitrary files and 0/1/C−1/C/C+1/2C/2C+1-byte cases round-trip; incomplete uploads are absent from normal reads/list; every committed chunk has two distinct locations. **Dependency:** A2/B2 and working registration. **Reviewer:** Friend.

### A4 — Metadata, list, delete, and public errors — You

Implement remaining public routes, all assignment metadata fields, custom metadata merge, meaningful JSON errors, logical deletion and all-worker cleanup tracking. Preserve content and creation time during PATCH.

**Done when:** API checks cover all six public routes/statuses; PATCH causes no chunk writes; offline-worker deletion stays pending; repeated pending cleanup eventually completes without resurrection. **Dependency:** A3/B3. **Reviewer:** Friend.

### B4 — Periodic integrity checking and corruption tool — Friend

Add a bounded fixed-order local scan of committed chunks against trusted expected values supplied during PUT/reconciliation. Persist scan progress so restarts cannot repeatedly skip the same cold tail. Verify both placements independently. Report scan failures in heartbeat reports, retaining/repeating unacknowledged reports. Build a local-test-only tool that mutates an actual chosen chunk inside a worker volume.

**Done when:** an unread corrupt chunk is detected and reported; all local placements get turns; reports survive acknowledgement loss/restart; neither scanning nor the tool changes expected hashes or unrelated files. **Dependency:** B2/B3 and contract agreement with A2/A5. **Reviewer:** You.

### A5 — Serial repair and safe rejoin — You

Coalesce heartbeat, read-failure, and inventory reports into a serial repair queue. Use a valid source, choose a different healthy destination, verify replacement bytes, and commit only if the object and repair intent are still current. For the course version the controller may relay one bounded chunk at a time. Reconcile extra copies after rejoin and recover pending repair/deletion work after restart.

**Done when:** worker2 outage triggers automatic re-replication; corruption triggers valid-copy recovery; no source yields an honest unavailable state; deletion during repair cannot resurrect data; rejoin safely returns to two normal placements. **Dependency:** A3/A4/B3; B4 adds proactive triggers. **Reviewer:** Friend.

### B5 — Client, inspector, and integration harness — Friend

Create CLI/script commands for upload/download/list/metadata/PATCH/delete and a read-only placement/health/repair view. Use the public metadata and agreed inspection routes. Add end-to-end checks using independently generated input files and hashes. Do not build a web frontend.

**Done when:** a person can exercise every public route and see actual placements and repair state; integration checks detect mismatched bytes and missing replicas. Use polling with explicit deadlines rather than arbitrary long sleeps. **Dependency:** can build against the contract first; integrate after A3/A4/A5. **Reviewer:** You.

### A6 — Controller fault/race checks and architecture guide — You

Verify incomplete uploads, unavailable/invalid copies, controller restart, deletion during repair, pending deletion, and returning extra copies. Write `docs/ARCHITECTURE.md` explaining manifests, states, placement, checked reads, repair, deletion, and known limits.

**Done when:** meaningful checks pass against real worker storage where relevant; no false success or stale repair publication occurs; diagrams match implemented behavior. **Dependency:** A5/B5. **Reviewer:** Friend.

### B6 — Six-demo walkthrough and clean checkout verification — Friend

Create `docs/DEMO.md` and scripts for all six assignment demos. Include real stop/start and actual byte corruption. Update README with commands actually executed, with setup, expected observations, and recovery steps. Retain small human-readable evidence, avoiding uploaded file bodies or large generated fixtures.

**Done when:** both people independently run the walkthrough from a clean checkout; all six demos pass; instructions distinguish required behavior, recommended scanning, and known limits. **Dependency:** A6/B4/B5. **Reviewer:** You.

## Integration milestones

No due dates have been invented; add dates together in the tracker.

| Milestone | Your deliverable | Friend's deliverable | Joint gate |
|---|---|---|---|
| C0: shared foundation | A1 and initial A2 models | B1 and initial B2 storage | Services build; contract reviewed; one durable chunk PUT/GET works |
| C1: complete object path | A2/A3 | B2/B3 | Four workers register; >1 MB upload/download and two-copy placement work |
| C2: complete required API | A4 | B5 initial client/inspection | Metadata, PATCH, list, delete, and errors work end to end |
| C3: failure and integrity | A5 | B4 plus B5 integration checks | Stop/read/repair/start and cold-corruption/repair pass; no deleted data returns |
| C4: submission freeze | A6 | B6 | Full checks pass; both run all six demos and explain the system; tag the tested course release |

Early fakes are acceptable for local development, but a milestone passes only with real services and persistent worker files. Neither person should wait until C4 to integrate their work.

## GitHub coordination

1. Both people work directly on `main` and push to `origin/main`; task branches, pull requests, and PR approval are not required. See [the publication guide](docs/GITHUB-START.md).
2. Create an issue per A1–A6/B1–B6 with the issue template. Link its ID in the tracker; add an assignee and milestone if useful. GitHub handles and due dates remain unassigned until known.
3. With a clean checkout, pull `main` before starting. Commit one card or small slice at a time. Before pushing, rebase local commits onto the current `origin/main`, resolve conflicts, and rerun affected checks. Never force-push over the other person's commits.
4. Keep controller/worker changes in their owner paths. For shared files, the primary owner makes the edit after both people agree. Cross-service contract changes include updated examples and tests in the same coordinated change.
5. Commit messages and the tracker state the card ID, changed behavior, checks actually run, and remaining limitations. Share the pushed commit with the other person. Existing PR templates are optional; no PR is needed. Never mark an unrun check as passed.
6. After pushing, the other person pulls `main`. Update the tracker with evidence before starting another dependent card. A useful task update is: completed behavior, current blocker, and the next interface needed. Pushing does not imply teammate review or milestone acceptance; review can happen directly without a PR.
7. Both people review recovery-sensitive code and run each other's demos. Freeze a tested `course-v1` tag only at C4. Continue portfolio work afterward without changing the frozen submission.

Do not commit environment secrets, live databases, local volume data, or generated large test files. Do not delete persistent volumes during failure/restart demos. The root `.gitignore` provides initial exclusions; keep it aligned with actual storage paths.
