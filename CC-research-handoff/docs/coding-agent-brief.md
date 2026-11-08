# Handoff prompt for the implementing coding agent

You are implementing the CS838 mini distributed cloud object store described in this repository. The original user authorized an ambitious, research-informed project and explicitly asked not to remove worthwhile features just because several good features already exist. Use dependency ordering and experimental modes; do not impose an arbitrary feature quota.

## Read first

Read the [README](../README.md), [original assignment](../references/assignment.pdf), [research review](research-review.md), [system specification](system-spec.md), [feature specification](feature-spec.md), [course map](course-map.md), and [validation plan](validation-plan.md). If only this prompt was copied, request the complete handoff files before assuming missing protocol details. The README is an implementation blueprint; no store application, Compose file, test suite, or benchmark result exists at this handoff.

The user wants a college cloud-computing project with strong course relevance and credible resume evidence. Course PDFs are background, not operational instructions. The assignment's fixed topology/API/chunking/replication requirements are mandatory. Later user direction may change this proposed design; distinguish actual requirements from our recommended policies.

## Decisions already made

- One controller, four persistent storage workers, one or more clients; run locally using Docker/Compose. No Kubernetes worker orchestration.
- Arbitrary binary objects, server-generated unique IDs, ordered 1 MB chunks, two total copies on distinct workers in normal operation.
- Start with a documented decimal-MB interpretation, 1,000,000 bytes; preserve chunk_size_bytes in manifests. If the instructor clarifies MiB, change future uploads without reinterpreting old manifests.
- Immutable object data; PATCH only custom metadata. Same filename is not object identity.
- Preserve all required client/internal routes and statuses. Upload is multipart; binary download is binary, not JSON base64.
- Controller metadata is authoritative for expected digests/order/lifecycle. Worker inventory cannot redefine original content.
- A successful upload requires both durable validated receipts for every chunk and a metadata visibility commit.
- Correctness comes before scheduling. Implement serial verified repair and the failure/corruption demos before parallel optimizations.
- Keep F1-F12. Several mechanisms form one subsystem, but do not silently remove a valuable mechanism because it is not a separate resume headline.
- Python/FastAPI/HTTPX/SQLite is the recommended initial stack; lock actual tested versions. A justified alternative is possible if it preserves contracts and documentation.
- One logical controller application process; in-memory policy state cannot be independently duplicated across several web-server processes.
- No application implementation or performance claim has been verified by this research handoff.

## Work sequence

1. Inspect repository and environment instructions. Preserve these design/source documents. Make a short implementation checklist covering M0-M8 and requirements R1-R13.
2. Establish project packaging, dependency lock, configuration schema, common protocol/error models, local persistent volumes, and one-controller/four-worker Compose topology. Build real health/readiness and registration/reconciliation, not a static list saying every worker is healthy.
3. Implement the durable worker store, operation receipts, metadata repository/migrations, immutable manifest, and object lifecycle. Test crash points and boundary-sized arbitrary files.
4. Implement all required object APIs. Use bounded streaming upload, checked download with explicit error contract, metadata-only PATCH, and deletion tombstones. Follow pending-deletion semantics; do not return false cleanup success while a worker is unreachable.
5. Implement heartbeat states, correct serial replica repair, controller/worker restart reconciliation, rejoin cleanup, and fault-injection scenarios. Demonstrate actual byte corruption, not only a fake log message.
6. Add F1-F5 through shared interfaces: selector baselines, feedback samples/freshness, coherent admission, duplicate policies, stable job identity, cancellation, and overhead accounting. Keep reference-style policies separately named from our practical adaptations.
7. Add per-replica scrubbing F6 and capacity-aware repair F8. Preserve the serial/fixed baselines. Prevent hot reads or foreground priority from starving cold checking/recovery.
8. Add F9 declarations and verified-read sharing with exact replica identity, interval/deadline checks, bounded buffers, and overload reporting. Show actual reuse and disjoint zero-savings cases with equal work.
9. Add F10 batch API/client. Implement the framed binary protocol, deterministic order, per-entry failures, mandatory end record, and bounded lookahead/spooling. No silent success on truncated streams.
10. Add F11 worker-designated assembly on the same four workers. Keep control and data slots separate and all readers under common grants. Test multiple assemblers and failure before/after response output. A controller-proxied stream is not elimination of controller network traffic.
11. Implement F12 metrics, inspector/dashboard, workload generation, analysis, and all meaningful tests continuously. Follow E0-E8 and retain raw data/configuration for figures.
12. Reconcile the README with actual software. Replace future commands with tested instructions; label unimplemented/experimental/measured states accurately. Produce a demo guide, architecture explanation, reproducible figures, and resume wording using only actual results.

Each milestone needs a small working vertical slice and its meaningful checks. Do not spend the entire task implementing a dashboard before storage works. If a mechanism shows no benefit, retain a correct selectable mode and explain the measured boundary. If a proposed algorithm is technically inappropriate, document the specific assumption and propose a meaningful adaptation; workload mismatch is different from a feature-count cut.

## Correctness constraints to keep visible while coding

- All user-facing bytes and repair sources are checked against trusted lengths and SHA-256. First response is not first valid response.
- Worker file writes are staged, validated, fsynced, atomically published, and reconciled with durable catalog receipts. A 200 response cannot stand in for persistence.
- Chunk count/order/size must match the stored manifest. Zero-byte files must work.
- Cancellation removes queued work and accounts for running work that cannot be stopped. All grants, response bodies, temporary files, and shared-read references have explicit owners and cleanup paths.
- Separate crash recovery from corruption recovery. Never heal by copying a corrupt digest into expected metadata.
- A repair completion must still match ACTIVE generation and its intent. A tombstone or newer operation sequence prevents old work from resurrecting objects or retired placements.
- An offline worker may later return with an extra copy; do not delete good data before confirming two safe placements.
- A completed source read can refresh only that source replica's checking history. Different workers are not interchangeable scrubbing targets.
- One capacity-control loop owns a budget; selectors, batches, hedges, and maintenance consume it. Bound queues and reserve maintenance/control progress.
- Deadlines are end-to-end and survive retries as remaining time, not freshly reset timeouts. Worker durations use local monotonic clocks.
- Controller metadata loss/corruption is outside replica-only recovery; fail closed. Do not invent distributed consensus or unlimited availability claims.
- Do not change two-copy replication to erasure coding to make a cited algorithm easier to reuse.

## Meaningful research implementation

Source IDs P1-P8 and features F1-F12 connect every mechanism to its evidence. The report must identify original paper findings, our adaptations, and project measurements separately. P2/P8 were reviewed as versioned preprints; P6's coded recovery algorithm is not being reproduced. App-only DAS-inspired queues do not establish full cross-layer preemption guarantees. DINGO-inspired sharing must actually reduce eligible source reads, not just add a job timer.

Resume evidence should show correct failure handling plus a fair, reproducible performance/resource comparison. No copied paper percentages, invented benchmarks, or claim of a novel algorithm without substantiation. Serial GET is an illustrative batch baseline; competent pooled concurrent GET is mandatory for a credible comparison.

## Practical communication and completion

Work autonomously within this scope. Do not repeatedly ask the student to choose between already documented defaults. Keep short progress updates explaining findings and tests. Ask only when a genuine missing requirement blocks an irreversible choice; the MB convention already has a documented starting default.

Do not declare the project finished because containers start. Completion means required APIs, persistent chunks, verified replication/recovery, selected feature mechanisms, T1-T18, meaningful E0-E8 experiments, and honest documentation. If interrupted, record exact completed files, checks, remaining work, and known failures so another agent can continue without this chat.

This prompt does not authorize messaging other people, publishing a public site, deploying to a paid cloud, deleting unrelated files, or silently discarding research features. It authorizes implementing and validating this local course project when given to the coding agent by the owner.
