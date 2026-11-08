# Course submission and portfolio scope

Scope recommendation prepared 8 November 2026 after reading the complete research handoff and the original three-page assignment; updated to record the subsequent feature selection and two-person split. This document defines the target scope; it does not report passing software or benchmarks. See [TEAM-PLAN.md](TEAM-PLAN.md) for ownership and [the tracker](docs/COURSE-TRACKER.md) for actual progress.

## Decision

Build the course submission as a complete mini distributed object store: all required APIs, chunk distribution, two-copy replication, persistent storage, failure detection, automatic repair, and checksum-based corruption recovery. Use simple deterministic policies and a small command-line demonstration.

After freezing the course submission, extend the same working system with measured adaptive replica selection and a more extensive fault/benchmark suite, followed by the selected later increments: smarter checking/repair, controlled duplicate reads, and controller batch retrieval. These are planned portfolio work, not course prerequisites. Declarative maintenance/read sharing (F9), worker batch assembly (F11), and additional paper-reference variants remain optional.

The handoff repeatedly says to keep F1–F12 and complete M0–M8. That describes the friend's ambitious proposed design. The current request calls for a course/portfolio distinction, so those statements are not the course completion gate. Preserve the handoff as reference; use the scope here for implementation planning. Research papers cited there are inspiration, not additional assignment requirements. Their original PDFs are not bundled, and this assessment does not independently re-review them or verify their published results.

The supplied assignment defines requirements and demonstrations, but contains no separate marking rubric, deadline, team rules, or policy about development tools. Coverage below addresses the supplied criteria; it cannot predict a grade or an examiner's reaction.

## Course version: implement these

**Assignment source:** every reference below points to the bundled [original assignment PDF](CC-research-handoff/references/assignment.pdf), using one-based PDF page numbers and its printed section headings. Read the [compiled assignment checklist](docs/ASSIGNMENT-REQUIREMENTS.md) for all required APIs, error codes, metadata fields, deployment rules, and demonstrations together. Our chosen implementation policies are identified separately there.

| Deliverable | Implementation choice | Evidence for acceptance | Assignment source |
|---|---|---|---|
| Required deployment | One controller, exactly four workers, and a CLI/script client; local Docker Compose; separate persistent worker volumes; no Kubernetes worker orchestration | A clean build/start produces the topology; restarting containers retains stored data | [Page 1: section 2, System Configuration](CC-research-handoff/references/assignment.pdf#page=1); [page 3: section 9, Deployment](CC-research-handoff/references/assignment.pdf#page=3) |
| Binary object storage | Multipart upload, unique object IDs, binary download with stored content type; filenames are display metadata | Text, image, video/audio, archive, and random bytes round-trip with an externally calculated SHA-256 | [Page 1: sections 1 and 3, Objective / Object and Metadata](CC-research-handoff/references/assignment.pdf#page=1); [page 2: sections 5.1–5.2, Upload / Download](CC-research-handoff/references/assignment.pdf#page=2) |
| Ordered chunking | Unique chunk IDs; 1 MB chunks; smaller final chunk; record order and lengths in the manifest | Inspect a 2,500,000-byte file as 1,000,000 + 1,000,000 + 500,000 bytes; test boundary sizes | [Page 1: section 4, Chunking and Replication](CC-research-handoff/references/assignment.pdf#page=1) |
| Distribution and replication | Rotate worker pairs when placing chunks, skipping unavailable workers; two total copies on distinct workers; check capacity before writing | Manifest and inventories show two actual stored copies and distribution over four workers | [Page 1: section 4, Chunking and Replication](CC-research-handoff/references/assignment.pdf#page=1) |
| Complete metadata | Name, size, content type, creation/modification times, checksum, chunk count, chunk IDs/order/lengths/locations; separate custom key-value map | Metadata endpoint exposes all fields without returning file data | [Page 1: section 3, Object and Metadata](CC-research-handoff/references/assignment.pdf#page=1); [page 2: section 5.3, Get Object Metadata](CC-research-handoff/references/assignment.pdf#page=2) |
| Metadata update | Merge custom metadata in a controller database transaction; update modification time | PATCH changes metadata while bytes, checksums, IDs, and worker chunk-write counts stay unchanged | [Page 2: section 5.4, Update Object Metadata](CC-research-handoff/references/assignment.pdf#page=2) |
| Object lifecycle APIs | Upload, download, metadata GET/PATCH, delete, and list; required success codes and meaningful JSON errors | Exercise every route, unknown IDs, malformed input, unavailable storage, and deletion | [Pages 1–2: section 5, Client REST API](CC-research-handoff/references/assignment.pdf#page=1); [page 2: section 5.7, Error Handling](CC-research-handoff/references/assignment.pdf#page=2) |
| Worker protocol | Registration, heartbeat, chunk PUT/GET/DELETE, and inventory routes exactly as assigned | Workers register with persistent identities; controller can inspect inventory and track availability | [Pages 2–3: section 6, Internal Worker APIs](CC-research-handoff/references/assignment.pdf#page=2) |
| Worker failure handling | Heartbeat timeout, checked fallback to the other copy, one repair job at a time, automatic restoration when a healthy destination exists | Stop worker2, keep reading recoverable objects, show repair to a distinct worker, restart worker2 and reconcile excess copies | [Page 1: section 4, replication restoration](CC-research-handoff/references/assignment.pdf#page=1); [page 3: section 7, Worker Failures](CC-research-handoff/references/assignment.pdf#page=3) |
| Corruption detection and recovery | SHA-256 and length checks on reads and repair sources; use a valid copy and replace damaged data | Mutate an actual stored chunk; detect it, return original bytes, and restore two valid copies | [Page 3: section 8, Data Integrity](CC-research-handoff/references/assignment.pdf#page=3) |
| Simple periodic checking | Each worker runs a fixed-order local background scan with a configurable pause and bounded work; durable fault reports go to the controller through heartbeats | Detect a corrupt cold object without a client download; show each replica gets checked | Our selected addition supporting [page 3: section 8, Data Integrity](CC-research-handoff/references/assignment.pdf#page=3); periodic scanning is not explicitly required |
| Instructions and demonstration | Build/start/use guide, reproducible CLI demo, readable metadata/health/repair output | All six required demonstrations run from documented steps | [Page 3: sections 9–10, Deployment / Required Demonstration](CC-research-handoff/references/assignment.pdf#page=3) |

Document the initial MB interpretation as 1,000,000 bytes. The assignment does not resolve MB versus MiB. If clarified by the instructor, change the configured size for new uploads and retain the size in existing manifests.

### Exact API checklist

Client routes:

- `POST /api/v1/objects` → 201; multipart `file`, optional JSON custom `metadata`.
- `GET /api/v1/objects/{object_id}` → 200; complete original binary data.
- `GET /api/v1/objects/{object_id}/metadata` → 200.
- `PATCH /api/v1/objects/{object_id}/metadata` → 200.
- `DELETE /api/v1/objects/{object_id}` → 204.
- `GET /api/v1/objects` → 200; at least ID, name, size, and content type.

Internal routes:

- Controller: `POST /internal/workers/register`, `POST /internal/workers/heartbeat`.
- Worker: `PUT`, `GET`, `DELETE /internal/chunks/{chunk_id}`.
- Worker: `GET /internal/workers/{worker_id}/chunks`.

Use the assignment's 400/404/409/500/503 errors where applicable, with meaningful JSON. A download must be binary, not base64 inside JSON. Normalize framework validation failures to the required invalid-request contract.

### Supporting safeguards belong in the course version

These are implementation choices that make the required behavior reliable. They do not need separate research branding in the report:

- Persist controller manifests and lifecycle state in SQLite on its own volume. Keep expected checksums authoritative there; worker inventory cannot redefine good content.
- Keep uploaded data immutable. Re-uploading the same filename creates a new object ID; PATCH changes custom metadata only.
- Write each worker chunk to a temporary file, verify it, flush it, and atomically publish it. A successful upload becomes visible only when every chunk has two validated durable copies.
- Track interrupted uploads and remove their orphaned chunks. Retain enough operation identity to reconcile a write whose reply was lost.
- Use short metadata transactions and bounded buffers, concurrent requests, retries, and network timeouts. Sequential chunk processing is acceptable initially; loading an arbitrary entire file into RAM is not.
- Verify a complete download into a quota-limited temporary file before sending success. This simple policy permits a meaningful JSON error if a later chunk is unavailable. Preserve it in fair performance comparisons.
- Record deletion durably and prevent a queued/in-progress repair from recreating deleted data. Recheck object state before repair publication and metadata commit. Use an operation revision where delayed work needs fencing.
- If deletion cannot reach an offline worker, hide the object and track cleanup. Follow the handoff's documented `503 deletion_pending` policy until all workers acknowledge cleanup, then return 204. This is a chosen edge-case policy, not an explicit assignment status rule.
- On restart/rejoin, reconcile manifests, worker inventories, pending cleanup, and repair work. Verify safe copies before removing extras; never remove the last valid copy to enforce a count.
- If neither copy is readable, return a clear error and retain diagnostics. Two-copy replication cannot tolerate every possible pair of worker failures.

A small SQLite schema and one serial repair loop are sufficient starting points. For course repair, the controller may relay one bounded chunk at a time using existing worker GET/PUT routes; direct worker-to-worker transfers are a later optimization. The handoff's declaration tables, generalized grant engine, speculative job registry, and worker assembly protocol are unnecessary for the course version. Simplify policy and structure without discarding the safeguards above.

### Stack and presentation

Use the handoff's proposed Python/FastAPI, HTTPX, SQLite, worker filesystems, pytest, and Docker Compose unless the team has a stronger familiar stack. Record tested dependency versions when implementation starts. One application process per service is enough.

Make upload/download and placement inspection work through a small CLI or scripts. The existing metadata API can show chunk placement; add only a small health/repair inspection view if needed. A frontend is not an assignment requirement. Do not make a polished dashboard part of course completion.

## Portfolio version: recommended first increment

The course system is already useful portfolio material if it runs reproducibly and demonstrates real failures. The first later increment should add an explainable, measurable performance contribution:

1. **Adaptive replica selection, F1.** Keep round-robin and a competent simple baseline such as least-outstanding or EWMA response time. Add one transparent queue/latency-informed selector. Route only to eligible replicas and validate all returned bytes. Paper-reference formulas are optional experiments.
2. **Feedback freshness and rediscovery, F2.** Timestamp measurements, reset them after worker restart, and allow bounded exploration of a recovered but previously slow worker. Heartbeat health is separate from actual read performance.
3. **Extend the simple limits in F3.** Account for foreground and maintenance work together, prevent unlimited queues, and give repair/checking some progress under load. A full adaptive rate controller is not necessary for the initial portfolio increment.
4. **Stronger correctness evidence, F7.** Add deterministic missing/truncated chunks, disk read/write errors, interrupted publication, deletion during repair, and controller/worker restart scenarios. Use controlled injection, not destructive host-wide faults.
5. **Reproducible evaluation, F12.** Save source version, dataset seed, configuration, hardware/container limits, raw request outcomes, and analysis. Compare healthy, slow-worker, recovered-worker, and saturated shared-host cases under equal offered load. Report latency, goodput, errors, and resource cost. Measure whole-object completion rather than only the selector function.

Complete and measure this increment before beginning the selected later increments. A transparent result showing where adaptation helps and where it does not is sufficient; no particular speedup is promised.

### Selected later increments, in implementation order

| Extension | Why consider it | Prerequisite and completion gate |
|---|---|---|
| Better checking and repair, F6/F8 | Compare fixed scanning with oldest-verified-first; improve recovery during foreground reads using bounded parallel repairs and capacity-aware destination choice | Correct serial repair first. Compare greedy and simple parallel repair at equal concurrency, checking budgets, and durability; measure recovery and foreground impact |
| Controlled duplicate reads, F4/F5 | Reduce latency from an occasional slow replica | Shared limits first; delayed budgeted hedges, attempt identity, cleanup, and loser-work accounting. Show benefit and extra work; a fast corrupt response must never win |
| Controller batch retrieval, F10 | A distinct client capability for many small objects | Bounded ordered binary stream, explicit per-entry failures and terminal completion, tested parser. Compare against pooled concurrent individual GETs |

### Outside the committed portfolio scope

| Optional extension | Why consider it | Prerequisite and completion gate |
|---|---|---|
| Worker batch assembly, F11 | Explore assembly placement and controller load | F10 first. Demonstrate identical output semantics and no deadlock; measure controller proxy traffic honestly |
| Declarative maintenance and read reuse, F9 | Study whether overlapping maintenance tasks can share a verified source read | Existing real scrub/repair consumers and measurable overlap. Equal completed work; no conflation of different replicas; disjoint workloads must show zero reuse |

F9 is the last priority: it adds deadline scheduling, persistent task progress, shared-buffer ownership, and subtle verification rules. At four workers its additional value is uncertain. Keep it as a research option until the simpler portfolio increment is measured. Matching-based repair and C3/Tars/DAS reference modes also remain optional experiments.

## Mapping every handoff feature

| Handoff feature | Course submission | Selected portfolio work / optional research |
|---|---|---|
| F1 Adaptive selection | Simple round-robin selection with valid-copy fallback | Adaptive selector and fair baseline comparisons first |
| F2 Freshness/rediscovery | Required heartbeat/rejoin behavior, without performance scoring | Performance-feedback freshness and bounded rediscovery first |
| F3 Backpressure | Fixed request/buffer/time limits | Shared work accounting first; adaptive rate algorithms optional |
| F4 Duplicate reads | Sequential necessary fallback only | Selected later: delayed, budgeted hedging |
| F5 Duplicate queues/cancellation | Ordinary request cleanup; no speculative job subsystem | Add with F4, including cancellation-cost accounting |
| F6 Scrubbing | Small fixed-order periodic checker | Selected later: oldest-verified-first policy; history-aware variants optional |
| F7 Recovery correctness | Core upload/read/delete/repair safety and essential fault tests | Broader deterministic fault/race suite first |
| F8 Repair scheduling | One correct serial automatic repair loop | Selected later: bounded parallel/capacity-aware planning |
| F9 Maintenance declarations/reuse | Deferred | Optional last-priority research work |
| F10 Batch retrieval | Deferred | Selected later: controller batch API/client |
| F11 Worker assembly | Deferred | Optional after F10 |
| F12 Visibility/evaluation | Logs, placement/health/repair inspection, tests, six demos | Reproducible benchmarks and analysis first; dashboard optional |

## Implementation order and course completion gate

1. **Working storage path:** Compose, durable identities/volumes, registration, durable chunk PUT/GET, manifests, upload/download, chunk placement.
2. **Complete API:** Metadata, PATCH, list, deletion, error contracts, empty/boundary files, interrupted-upload cleanup.
3. **Failure handling:** Heartbeats, valid-copy fallback, serial automatic repair, rejoin, checksum corruption recovery, simple checker, restart/deletion safeguards.
4. **Submission package:** Inspection script, automated conformance checks, six-demo walkthrough, architecture explanation, actual build/start/use instructions.
5. **Freeze the course version:** Record a tested commit/tag before later extensions. This gives the submission an independently reproducible scope.
6. **Portfolio work:** Implement and measure the first increment above, then the selected checking/repair, duplicate-read, and controller-batch increments. F9/F11 and further reference variants remain optional research.

Course acceptance includes all assignment behaviors, not all handoff experiments. Reuse T1–T8, T11–T13, and T15 from the handoff for applicable lifecycle/fault checks; use T14's basic per-replica cold-corruption check without its advanced deadline machinery. T9/T10's speculative-attempt cases, T16–T18, and E1–E8 belong with the extensions that introduce them. Ordinary disconnect cleanup and bounded operation remain course safeguards.

Run the six demonstrations in assignment order: byte-identical upload/download; >1 MB chunking/distribution; two-copy locations; custom metadata update without data writes; worker2 stop/read/repair/start/reconcile; actual corruption/detection/fallback/repair. Include a negative check where both copies are unavailable or invalid and the system returns an honest failure.

## Viva preparation

The scope should be small enough that every team member can trace these paths in the code and reproduce the demo:

- How a 2.5 MB object becomes three chunks, and how their two copies are placed.
- How the manifest restores original order and why clients never need worker locations.
- How worker unavailability is inferred, and why a timeout does not prove data is destroyed.
- How two copies differ from protection against every two-worker outage.
- How expected checksums detect corruption, how a valid repair source is chosen, and why two bad copies cannot repair themselves.
- Why metadata can change without re-upload, and why incomplete uploads remain invisible.
- How restart, pending deletion, and an old repair interact without resurrecting an object.
- Why controller metadata is persistent, while the single controller remains an availability limitation.
- Which properties were required, which are implementation decisions, what was tested, and which limitations remain.

Use the six required demonstrations and a plain architecture diagram as the course presentation. Discuss only mechanisms actually implemented and understood. Label paper-inspired adaptations and actual measurements accurately when presenting later portfolio work.
