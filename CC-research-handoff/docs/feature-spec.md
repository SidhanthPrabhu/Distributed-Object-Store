# Research extension implementation specification

All policies below are our adaptations unless explicitly labeled a reference formula. Read [the eight paper reviews](research-review.md) for provenance and transfer boundaries. Every selected feature F1-F12 is retained. Dependency ordering is not permission to silently remove later work because earlier features are already impressive.

## Shared interfaces and invariants

Design these interfaces before implementing separate optimizers:

```text
ReplicaSelector.rank(chunk_manifest, candidate_snapshots, request_context) -> ordered worker IDs
FeedbackStore.observe(attempt_id, worker_boot_id, durations, sizes, queue_sample, outcome)
BudgetManager.reserve(job_class, source, destination, bytes, deadline) -> reservation
CheckedChunkReader.read(manifest, context, policy) -> verified bytes or typed error
JobRegistry.cancel(attempt_id) -> queued_removed | running_cancel_requested | finished | unknown
RepairPlanner.plan(pending_jobs, health, capacity, budgets) -> bounded transfer intents
MaintenancePlanner.declare(replica_sets, required_count, interval, deadline, consumer) -> declaration_id
BatchAssembler.execute(manifest_snapshot, ordered_entries, context) -> framed stream
```

A request context contains request/run IDs, object generation, deadline, logical chunk ID, attempt role, cancellation state, trace context, and budget reservations. All attempts use the same integrity rules. Do not allow a batch, repair, or speculative path to bypass the common verified reader/storage logic.

The timing model records controller admission wait, worker queue wait, worker read/hash duration, full-body RPC duration, and controller validation duration separately. RTT is not pure network latency. Performance samples are keyed by worker boot and size class. Metrics are observations, not perfect instantaneous queue knowledge.

## F1. Adaptive replica selection

**Deliverable.** A selector plugin with `random`, `round_robin`, `least_outstanding`, `ewma_rtt`, `adaptive`, and `c3_reference` modes. Each takes the same candidate set: committed compatible generation, not known corrupt, worker ready/available. A source believed VALID must still be checked when read.

For the practical `adaptive` mode, start with a transparent heuristic, not an uncalibrated cubic expression presented as optimal:

```text
for each eligible replica:
    if performance sample missing/stale: use bounded discovery/fallback (F2)
    pressure = max(reported_queued_requests,
                   max(0, local_outstanding_requests - measured_worker_concurrency))
    predicted_ms = estimated_transfer_overhead_ms[size_class]
                   + ewma_read_hash_ms[size_class]
                     * (1 + pressure / max(1, measured_worker_concurrency))
choose lowest finite predicted_ms among replicas allowed by F3
break near-ties using a seeded/randomized choice, not a fixed worker ID
```

This is our proposed queue-pressure heuristic. It does not assert accurate queue reconstruction. Initially estimate transfer overhead as a non-negative EWMA of complete RPC duration minus worker residence duration; measure the appropriate size class. Avoid adding worker queue time again if using an unadjusted end-to-end RTT score. Evaluate against the simpler EWMA and least-outstanding policies; simplicity may win at four workers.

The `c3_reference` ranking formula from P1 is:

```text
q_hat = 1 + outstanding * client_weight + smoothed_reported_queue
score = smoothed_RTT - smoothed_service_time + q_hat^3 * smoothed_service_time
```

Keep consistent units and the paper's distinction between service time and aggregate service rate. Because one controller schedules these requests, start `client_weight=1`; public client count is not the number of independently routing coordinators. Guards against invalid samples, negative queue estimates, zero capacity, and numerical overflow are engineering adaptations and must be recorded. A score mode alone is not the full C3 algorithm.

**Acceptance.** Decisions are explainable in a trace; ineligible workers cannot win; process-local outstanding counters return to zero after errors/cancellation; identical seeded inputs can replay deterministic decisions. Integrate checked fallback and never trade integrity for latency.

## F2. Freshness, measurement, and rediscovery

At controller receipt of valid feedback, record monotonic sample time, worker boot ID, size class, queue observation, successful throughput sample, and response/service duration. Reset samples after boot changes. A heartbeat updates availability, not the latency sample for a real chunk.

Configure a freshness horizon and small rediscovery budget. For a healthy worker whose old poor score causes avoidance, allow a bounded real read/probe after a cooldown, including when it has had no traffic for a long time. Do not repeatedly probe a worker classified UNAVAILABLE. Probe outcomes feed the matching size class only. A health endpoint's latency cannot substitute for a chunk's disk/hash/transfer cost.

Worker capacity measurements count completed work over a time interval with concurrency recorded. Report requests/s and bytes/s separately. Do not infer total throughput by taking the reciprocal of one task's service time while four reads execute concurrently. Use local durations, not subtraction of wall-clock timestamps across hosts.

Keep `freshness=off/on` and `rediscovery=off/on` independent for ablation. An experimental `tars_reference` ranking/rate variant may use P2's distinctions, but must document any modification to its equations, original thresholds, or queue model. Do not equate the practical heuristic with an exact Tars reproduction.

**Acceptance.** A recovered worker eventually receives controlled useful work; stale information is visible; probing remains bounded; there are no infinity/NaN scores or a zero-rate deadlock after startup.

## F3. Admission, backpressure, and capacity control

Use one coherent budget hierarchy: global controller work, per-worker data operations, per-worker/peer bytes, per-outer-request fan-out, class allocation, and spool/cache memory. Worker-local queues provide a second bounded enforcement point; they do not justify unlimited controller submissions.

Suggested first experimental values are four active data operations per worker, a queue of 128, and a global outstanding cap of 32. They are initial tuning inputs, not verified defaults. Separate inexpensive control-plane capacity so a saturated data queue does not block heartbeats/cancellation.

Schedule across outer requests fairly. A batch containing 128 objects must not get 128 times the shares of an ordinary GET merely because it has more children. Within shares, prefer primary foreground work; hedges use only admitted spare capacity. Reserve an explicit maintenance minimum when pending, with configurable escalation for endangered data. Strict primary priority forever would starve checking/repair, so our combined scheduler differs from ideal DAS priority.

When all candidates are saturated, wait in a bounded queue with the original deadline. If admission cannot happen within the budget, return a typed overload error mapped to 503 on required APIs. The batch extension can use 429 with Retry-After for pre-admission rate pressure if documented, but memory/storage inability remains a meaningful resource error. Never run unbounded `gather()` over every object/chunk.

Retain rate modes `fixed`, `aimd_project`, `c3_reference`, and `tars_reference` for research. The practical AIMD policy uses congestion/queue feedback with bounds, conservative increases, multiplicative decreases, and cooldowns. Reference modes use P1/P2's published logic, with units and changes documented. Only **one** capacity-control mode controls a worker at a time. Do not stack CUBIC, AIMD, and an independent repair feedback loop blindly.

All modes require an initial nonzero dispatch allowance and bounded rediscovery to avoid a limiter that receives no completions because it permits no requests. Feedback after errors must not be misclassified as successful service capacity.

**Acceptance.** Every permit/byte reservation is released exactly once. Count a remote operation whose cancellation is unacknowledged as potentially running until completion, cancellation acknowledgement, an explicit worker job expiry, or worker unavailability. Do not free its physical capacity merely because a local coroutine was cancelled. Log reservation ownership to diagnose leaks.

## F4. Controlled duplicate reads

Implement four modes: `off`, `naive_eager` (benchmark only), `delayed_budgeted`, and `eager_prioritized` (experimental DAS-inspired mode). Eager mode is retained because it tests a distinct mechanism from a timeout threshold.

Practical delayed policy:

```text
start primary attempt chosen by selector, under shared budgets
arm a hedge timer only after primary dispatch
if primary succeeds validation: finish and cancel timer
if primary fails first: attempt the remaining eligible replica as necessary fallback
if timer fires while primary unfinished:
    if remaining deadline, alternate health, spare capacity, and hedge budget allow:
        start one lower-priority alternate attempt
accept the first fully received, length- and checksum-valid chunk
notify/cancel any loser; record bytes and work already consumed
if no valid attempt remains: return typed unavailable/integrity failure
```

Use at most one body attempt per physical replica and at most two per logical read. Disable invisible HTTP transport retries on this path. A necessary sequential fallback is not discretionary extra concurrency, but still consumes worker budgets. No hedging for writes, deletes, metadata mutation, repairs, or already speculative attempts.

Explore a recent primary p95 threshold after sufficient samples, with a documented fallback threshold during warmup. Condition samples on size class and include only appropriate completed primary attempts; mark censored/cancelled samples rather than treating them as fast completions. Do not hard-code an original paper's threshold as universally correct.

Initial hedge controls to explore: no more than 5% extra attempts over a rolling window of logical primary dispatches, and at most one active hedge per worker. Bootstrap allowances and window rounding must be bounded. Compare the same policy with/without queue prioritization and purging; the result must show their contribution rather than attributing all gain to one acronym.

**Acceptance.** Fast corrupt data loses; two simultaneous valid completions publish once; a deadline never restarts on retries; an unavailable alternate suppresses duplication; caller disconnect stops useless work. Public completion need not wait for a slow loser cancellation acknowledgement, but a bounded supervisor owns cleanup and capacity accounting afterward.

## F5. Duplicate-aware jobs, priority, and purging

Every logical chunk read has a group ID; each physical attempt has a unique ID, role (`primary`/`hedge`), immutable content identity, cancellation token, enqueue/dispatch timestamps, and absolute request deadline represented safely for each process. Workers never generate additional duplicates.

Keep queued work in structures that support removal by attempt ID. A cancel arriving before enqueue must leave a short-lived bounded cancel record so a later delayed enqueue cannot start that job. Cancel-before-start and cancel-after-finish are idempotent outcomes. A running blocking disk read may be non-preemptible: stop subsequent transfer/hash work when possible, account for completed work, and report the limitation.

The worker checks cancellation before disk dispatch and before response publication. Controller closes loser responses/connections, explicitly cancels remote jobs, and polls bounded job status if needed. Worker job TTLs use receive-local monotonic durations plus sequence/epoch checks; do not assume synchronized clocks provide fencing.

If an in-flight verified read has multiple consumers (F9), cancellation decrements a reference count; only the last consumer can request cancellation of the underlying physical work. A cancel for one hedged GET cannot cancel an urgent repair that joined the same physical source read.

Optional Linux experiments can investigate network/I/O priority enforcement. Do not assume historical CFQ behavior is present on the current kernel/device or request privileged settings as a hidden default. Application queueing and cooperative cancellation remain portable. No end-to-end "duplicates never hurt" guarantee is claimed without appropriate enforcement and measurement at every bottleneck.

## F6. Proactive per-replica scrubbing

Each placement tracks last successful **fresh physical application read/verification**, coverage interval, verification epoch, error observations, and deadline. Here "physical application read" means a read through the worker's file abstraction; it may still hit the OS page cache and is not necessarily a media access.

Modes: `fixed_cycle` baseline, `oldest_verified_first`, and `history_adaptive`. All modes must cover every required replica; random sampling alone cannot satisfy complete coverage. A successful read from worker1 cannot renew worker2's verification timestamp.

History-adaptive selection first honors imminent coverage deadlines, then favors old/never-verified placements, then gives a bounded temporary boost to a worker after corruption. This is our policy, not a calibrated probability of HDD failure. Avoid starving unaffected workers by reserving some scan progress for them. Do not model byte-adjacent UUID filenames as adjacent disk sectors.

Illustrative lab parameters: normal scrub target every 300 s, hard maximum verification age 900 s, configurable minimum/maximum scan bandwidth, and an escalation interval after an observed error. Feasibility depends on data size and competing work. Compute required checking bytes per deadline; expose an infeasible-coverage alert instead of pretending a fixed budget guarantees every deadline. Declarations may be rescheduled but not marked complete without real eligible work.

Verification obtains trusted expected values, reads a current placement, checks length/hash, and reports a result tied to its generation/boot/verification epoch. Failure marks that replica suspect/corrupt, enqueues repair, and invalidates stale reuse entries. Do not erase the object's expected hash.

**Acceptance.** Cold corruption is detected without GET; old cold placements get a turn under hot reads; post-restart declarations cover remaining work; raw checking bytes and detected faults are inspectable.

## F7. Integrity, lifecycle correctness, and fault experiments

This feature is inseparable from the baseline, but its systematic validation is a substantial deliverable. Use a deterministic fault interface at the worker storage boundary:

```text
FaultSpec(kind, worker_id, chunk_id, operation_phase, occurrence, duration, seed)
kind = corrupt_bytes | truncate | missing | read_eio | write_eio
       | enospc | short_write | delay | connection_drop | crash_at_checkpoint
```

The test runner records an external expected digest and intended fault time; data/control process errors remain distinguishable. Never implement simulated disk-full by filling the user's entire host disk. Use controlled injected errors or a bounded test filesystem. Byte mutation of an actual selected chunk file must be part of the real demonstration.

Correctness assertions include no success for incomplete uploads, no unchecked bytes, no repair from a corrupt source, no false repair receipt, no stale-job resurrection, recoverable operation receipts after timeout, and safe fail-closed behavior if both copies are invalid. See [system spec](system-spec.md) for the publication protocol and [validation plan](validation-plan.md) for scenarios.

Fault probes and control APIs are enabled only in the local demo/test profile. A reproducible known fault is useful scientific evidence; it is not a measured real-world disk-failure distribution.

## F8. Replica repair scheduling and placement maintenance

First keep a serial correct repair baseline. Add `fixed_concurrency`, `capacity_greedy`, and `matching_experimental` planners using the same repair execution/commit protocol.

For capacity-greedy planning:

```text
order jobs by urgency/deadline and bounded age-based fairness
for each eligible job in the epoch:
    identify a current valid source; if none, defer with explicit reason
    enumerate healthy distinct destinations with reserved free capacity
    estimate finish pressure from assigned bytes / observed residual capacity
    choose the destination minimizing added receiver bottleneck pressure
    atomically reserve source-send, destination-receive, disk and byte budgets
    dispatch a versioned idempotent transfer; update estimated assigned work
replan after bounded completions or changed health; never wait for a global barrier
```

With a lost one-of-two replica, there is usually one source. When there are multiple valid sources during temporary extra-copy situations, choose only compatible verified data. Source upload and destination download are separate constraints; disk contention can couple them. Do not assume loopback containers provide independent full-duplex hardware links.

Retain a matching experiment: construct eligible source-to-destination edges for pending replicas and select a set respecting available sender/receiver slots. At unit capacities this is a small bipartite matching; weighted/byte-aware variants are our adaptation, not RepairBoost's coded DAG algorithm. Compare scheduler cost and completion results against the greedy planner. Small clusters may not benefit.

All repair requests still verify source and destination, durably publish, and conditionally commit. Unavailable/corrupt sources cannot be scheduled just because their link is fast. Add exponential backoff with jitter for transient failures and retain terminal diagnostics rather than retry forever.

Maintenance receives a nonzero minimum share. Ordinary background checking can yield to an urgent one-copy repair, but cannot disappear indefinitely. Optional capacity rebalancing may use copy-validate-commit-retire with a temporary third replica; do not migrate solely to invent overlap for F9.

## F9. Maintenance declarations and safe I/O reuse

Declare sets of replica reads with a coverage interval, completion deadline, and consumer action. For scrub declarations every listed placement must be checked; `required_count` is all placements. Data-choice flexibility is legitimate for a capacity-balancing consumer but not permission to scrub only the convenient copy.

Store declaration progress durably. Map wall-clock deadlines to process-local monotonic deadlines. Within each scheduling quantum, calculate `remaining_required_bytes / remaining_time`; schedule needed progress under the actual maintenance budget, favoring reuse only when it does not endanger deadlines. Mark overload when required progress cannot fit; report missed deadlines. Do not trigger an unbounded wave of imperative fallback reads when several deadlines expire.

For dispatch, find contemporaneously needed reads with the exact key:

```text
(chunk_id, immutable_generation, source_worker_id, source_boot_id, verification_epoch)
```

Read and validate once, then fan out the bounded immutable buffer to eligible consumers. This buffer-sharing implementation is our adaptation; DINGO's original callback prompts tasks to issue reads and relies on cache reuse. Reserve enough buffer space before joining consumers; evict when references are released and keep a hard byte cap. Maintain read-origin timestamps and expiry so old cached bytes never substitute indefinitely for a new disk check.

Example valid reuse: a fresh source read during replica repair also fulfills a due scrub of **that source placement**. It does not verify the newly written destination or a second existing replica. Destination validation remains separate. If a consumer's declared interval begins after the physical read, that read does not satisfy it. A forced fresh-scrub epoch deliberately prevents joining an older cache entry.

Urgent repair executes without waiting to accumulate reuse opportunities. Normal scans/rebalancing can expose timing and ordering flexibility. Deleted generations cancel pending declarations. Recheck lifecycle before callbacks and before commits; metadata edits alone do not change immutable data identity, but object deletion does.

**Acceptance.** An overlapping workload saves actual worker-file reads at equal completed checking/repair work. Disjoint workloads show zero reuse. Track bytes requested, bytes actually read, shared bytes, suppressed deleted work, active buffer bytes, deadline misses, and scheduler overhead separately. OS page-cache effects are not application read savings.

## F10. Ordered batch retrieval API and client

Use `POST /api/v1/batches/retrieve`, JSON request, rather than copying a GET-body API. [RFC 9110 section 9.3.1](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.3.1) gives GET content no generally defined semantics; intermediaries and clients may reject it. This is our interoperability choice, not an assignment change.

```json
{
  "object_ids": ["uuid-A", "uuid-B", "uuid-A"],
  "continue_on_error": true,
  "assembly": "controller"
}
```

Duplicates preserve distinct request positions. Use fixed input order, generation snapshots, and explicit errors. Sampling/shuffling belongs to the client. The optional batch API supplements, never replaces, ordinary GET.

Define a binary-safe stream `application/vnd.cc.batch-v1`. Initial framing:

```text
magic: ASCII CCB1 followed by newline
repeat:
    4-byte unsigned big-endian JSON-header length (1..65536)
    that many UTF-8 JSON-header bytes
    exactly header.data_length payload bytes
```

Header kinds:

- `object`: index, object_id, generation, size_bytes, content_type, sha256, data_length; data is a fully verified object.
- `error`: index, object_id, stable error code/message, data_length=0; preserves position in continue-on-error mode.
- `fatal`: fatal stream error, data_length=0; client fails the batch, even if some prior objects arrived.
- `end`: requested_count, delivered_count, failed_count, complete=true, ok=(failed_count==0), data_length=0. It is required exactly once for a normally completed stream. No content follows it.

The client validates lengths, index order, expected count, per-object digests, terminal counts, and the end record. EOF without end is failure, not success. Do not embed arbitrary binary data inside JSON or scan payloads for delimiter strings. Do not allocate data_length bytes eagerly in the client; stream it under limits.

Before beginning HTTP 200, validate the request, establish resource reservations, and preflight metadata for strict mode. Once streaming starts, use error/fatal frames, never attempt to switch the HTTP status. `continue_on_error=false` aborts on the first retrieval error; it does not promise transaction-like rollback of objects already delivered. In continue mode, explicit unavailable/missing entries are errors, not zero-byte successful objects.

Use bounded per-object spooling and whole-object validation as in normal GET. Read ahead only a few entries, reserve disk/memory, and guarantee admission capacity for the next required output entry so a slow head cannot deadlock behind later spools. Record head-of-line blocking. Stream headers and payload in bounded writes, release each spool promptly, and propagate disconnect/cancellation through all child jobs.

Initial resource suggestions: max 128 entries/request and lookahead 4, configurable total spool quota, and a per-request byte reservation limit. These are documented limits, not unbounded guarantees. Batch pressure must not consume all ordinary GET shares.

Archive-member extraction is an additional possible enhancement, not needed to demonstrate this paper's main transferable retrieval idea. It requires archive format bounds, path validation, and a separate comparison; do not quietly claim it was implemented with simple whole-object batching.

## F11. Worker-designated assembly

Retain `assembly=worker` alongside controller assembly. The controller validates the batch, pins manifests, and chooses one of the existing four healthy workers using predicted assembly load and placement locality, not just object count. A chunked object can span all workers; calculate locality in bytes.

The controller issues a bounded assembly manifest containing immutable IDs, lengths, digests, request positions, and short-lived execution identity. Large manifests must be paged or rejected/fall back explicitly, never sent as unlimited JSON. The assembler obtains bounded chunk-read grants from the controller's shared scheduler in a small window, then reads peers and assembles the same framed response. It does not independently bypass the global budget manager or invent a new selector client multiplier.

Start with a controller proxying the assembler's stream so external clients keep the same endpoint. Proxying still uses controller network capacity and must not be advertised as eliminating that bottleneck. A direct redirect/capability-token path is a separately evaluated improvement if implemented.

Assembly bookkeeping/stream slots are different from data read/disk permits. Never hold all local disk permits while waiting for peer reads: four workers assembling one another's batches can otherwise deadlock. The head entry always has reserved forward-progress capacity. Local and peer chunks obey the same length/digest checks and per-replica verification rules.

If the assembler fails before output begins, the controller can retry once on another worker under the same deadline. If bytes have been emitted, abort and let the client retry the whole batch; do not transparently concatenate a restarted stream. Resume-at-entry boundaries can be designed later but is not implied. Clean up grants, spools, and shared-read references after a coordinator failure or client disconnect.

**Acceptance.** Controller and worker assembly produce identical ordered entries/error semantics. Parallel assemblies do not deadlock. Failure yields explicit truncation/abort rather than silent duplicate entries. Measure peer bytes, controller bytes, assembly CPU/memory, and waiting for grants/peers separately. See E8.

## F12. Observability and experiment tooling

Expose health, Prometheus-compatible metrics, structured events, and a read-only inspector. Dashboard/CLI should show object lifecycle/generation, chunk sizes and placement, last verification per replica, worker boot/health, replication deficits, active repair/declaration progress, queue occupancy, hedge outcomes, and selected policies.

Metrics dimensions must stay bounded: worker ID, job class, policy, outcome. Put Object IDs, chunk IDs, request IDs, and attempt IDs in sampled traces/events, not unbounded Prometheus labels. Never log file bodies. Record policy decisions with their sample ages so routing behavior can be explained.

Minimum counters/histograms:

```text
objects_committed, object_bytes, api_requests/errors, end_to_end_latency
worker_queue_wait, worker_read_hash_duration, worker_rpc_duration
outstanding/queued work by class, reserved bytes, queue rejections
hedges_dispatched/suppressed/won, loser_bytes, cancelled_queued/running/unknown
replica_verify_success/failure, verification_age, scrub_deadline_misses
repair_bytes, detection_delay, post_detection_repair_time, deficit_exposure
maintenance_requested/read/shared_bytes, active_reuse_buffers, declaration_misses
batch_makespan, time_to_first_object, peer_wait, throttle_wait, reorder_wait
spool_bytes, RSS, CPU, event-loop lag, open connections/files, controller DB wait
```

Record raw samples or compatible histogram data for analysis, with definitions from the validation plan. A UI chart is not evidence unless its underlying data and configuration are retained. Infrastructure metrics should reveal the controller/shared host as a potential bottleneck.

## Configuration and interaction rules

Use a checked configuration file with a schema, log the resolved values at startup, and save it with every experiment. An invalid policy combination must fail startup with a useful message. Suggested switches:

```yaml
chunk_size_bytes: 1000000
replication_factor: 2
selector: adaptive
freshness: true
rediscovery: true
admission_mode: fixed
hedge_mode: off
worker_queue_policy: project_fair
scrub_policy: fixed_cycle
repair_policy: serial
maintenance_reuse: false
batch_assembly_default: controller
download_mode: verified_spool
fault_injection_enabled: false
```

This example defines an initial baseline configuration, not the eventual optimized result. Keep topology and integrity/durability constant across comparisons. Constraints that prevent invalid experiments:

1. F4 depends on F5 accounting and F3 limits; no nested duplication or hidden retries.
2. F6 covers both placements; F9 cannot coalesce different replicas into one inspection.
3. F8 and F9 share a budget; urgent recovery bypasses opportunistic waiting, not integrity or fencing.
4. F10/F11 invoke the same checked reader and fairness policy as ordinary requests.
5. One active capacity-control loop owns each worker budget; other policies consume its grants.
6. Tombstones/generations apply to every background task, cached read, and late acknowledgement.
7. Feature-off modes retain the same cryptographic checks, durability, pooling, and resource limits. Do not make a baseline artificially weak.
8. Every selected feature has an experiment or correctness gate before being called complete.
