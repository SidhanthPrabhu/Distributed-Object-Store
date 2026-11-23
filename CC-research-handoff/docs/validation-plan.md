# Correctness, experiments, and demonstration plan

No application tests or benchmarks have run yet. This document is the implementation acceptance plan. The research handoff's own checks validate sources, document links, and consistency only.

## 1. Validation principles

Start from an external byte oracle: the test client creates a known file, calculates its digest independently, uploads it, and verifies the complete downloaded bytes. Assertions about storage must inspect both controller metadata and worker data/receipts. A status code or a matching pair of corrupt replicas is not a sufficient oracle.

Test the storage abstraction's error paths and real container stop/start. Include controlled actual chunk-file mutation, process crashes at publication boundaries, and request races. Do not damage files outside the project's named test volumes or fill the host disk to simulate ENOSPC.

Unit tests are useful for frame parsing, ranking bounds, budget accounting, and lifecycle decisions. Integration tests must establish the system's behavior across real processes and durable files. Stateful/generated operation traces can exercise upload/download/delete/worker-restart combinations against an independent model. Avoid tests that merely restate private implementation steps.

## 2. Correctness matrix

| ID | Scenario | Required observation |
|---|---|---|
| T1 | Upload/download arbitrary bytes at 0, 1, C-1, C, C+1, 2C, 2C+1 and larger sizes | Exact external digest/length; contiguous chunk order; final size correct; zero-byte object has zero chunks |
| T2 | Repeated name/content uploads and concurrent uploads | Distinct IDs; no name/path collision; independent metadata; two different workers per chunk |
| T3 | List, metadata, PATCH, missing IDs, invalid JSON/multipart | Required fields/statuses; PATCH changes only custom metadata/revision/time; unchanged chunk writes and digests |
| T4 | Upload fails after some receipts, lost acknowledgement, controller restart | No partial ACTIVE object; idempotent receipt recovery; tracked orphan cleanup; retries do not duplicate committed objects accidentally |
| T5 | Worker stop/start with a committed object | Detection recorded; surviving valid copy serves data; replication restored when feasible; identity survives restart |
| T6 | Byte flip, zeroed region, truncation, missing file, read EIO | Corruption/error detected; bad bytes never returned; fallback uses original expected digest; repairs restore exact bytes |
| T7 | Write EIO, short write, injected ENOSPC, crash before/after rename/catalog commit | No false durable receipt; incomplete staged data not served; restart reconciliation is bounded and correct |
| T8 | Both copies invalid; both temporarily offline; fewer than two writable workers | No fabricated recovery; explicit unavailable/error state; temporary outage distinguished from proven destroyed copies |
| T9 | Fast corrupt attempt races a slower valid one; two valid attempts finish together | First fully validated result wins once; corrupt response loses; no duplicate client bytes |
| T10 | Client disconnect, deadline, cancel before enqueue, cancel while queued/running | No orphan supervisor tasks/permits; worker reports actual cancel outcome; unavoidable loser work remains accounted |
| T11 | Delete races a repair, upload receipt, or declaration callback | Terminal tombstone prevents resurrection; stale metadata commits fail; all configured workers fenced before 204 |
| T12 | DELETE while a worker is offline, then retry/restart | Object hidden; 503 deletion_pending until durable cleanup; eventual 204 for pending operation, subsequent ordinary DELETE 404 |
| T13 | Controller/worker restart mid-repair; old epoch commands; duplicate worker identity | Stale jobs cannot publish current placements; registration conflict detected; no corrupt manifest reconstruction |
| T14 | Cold corruption with ongoing hot-object reads and different replica histories | Both placements receive independent coverage; cold corruption found proactively; deadline misses are visible |
| T15 | Rejoin after repair created a replacement copy | Temporary extra recognized; two valid placements retained; only excess placement retired; no terminal tombstone installed for unrelated chunks |
| T16 | Shared maintenance read, late consumer, different replica, forced fresh check, delete while waiting | Share only eligible contemporaneous identical placement read; no false cross-replica verification; deletion cancels work |
| T17 | Batch arbitrary binary bytes, repeated IDs, slow first object, missing entry, fatal failure, truncated stream | Order preserved; meaningful per-entry errors; digest checks; explicit end required; bounded RSS/spool and no deadlock |
| T18 | Parallel worker assemblies, assembler crash, health/control traffic under saturation | No cyclic waits; same output semantics; truncated/restarted streams cannot pass; queues/control operations remain bounded |

Add targeted tests whenever a discovered race warrants them. Do not broaden the system to support mutable object overwrites simply to exercise a paper's unrelated consistency protocol.

## 3. Policy baselines and ablations

Every comparison uses the same chunk sizes, dataset, integrity checks, durable-write settings, connection reuse, topology, and quotas unless the deliberately varied factor is named. Baselines must be competent implementations.

| Family | Modes to compare | Isolate |
|---|---|---|
| Routing | random, round-robin, least-outstanding, EWMA RTT, adaptive, C3-style reference | Benefit/overhead of queue-informed choice |
| Freshness | same selector, freshness off/on, rediscovery off/on | Recovery from old measurements, probe cost |
| Admission | fixed bounded admission, project AIMD, C3/Tars-style experimental rate modes | Queue containment, throughput, controller stability |
| Duplication | none, naive eager, delayed budgeted, eager prioritized | Extra work versus tail reduction |
| Purging | same duplicate policy with queued removal and cooperative cancellation off/on | Necessity and limits of duplicate cleanup |
| Scrub | fixed-cycle, oldest-verification-first, history-adaptive | Detection/coverage at equal byte budgets |
| Repair | serial, fixed concurrency, capacity greedy, matching | Recovery coordination benefit independent of integrity protocol |
| Maintenance | independent reads, declarations without reuse, declarations with reuse | Savings from timing/reuse, separate from mere scheduling |
| Batch | serial GET, pooled concurrent GET, controller batch, worker batch | Coordination/streaming/assembly placement |

Run individual ablations before the combined configuration. Do not attribute a combined result exclusively to one paper when several policies changed.

## 4. Experiment matrix

### E0. Baseline conformance and durability

Run T1-T8 and the six assignment demos with simple policies. Record the object/chunk/replica state before and after restart and corruption. Verify both worker receipt persistence and controller metadata persistence. No optimization milestone is accepted without this gate.

### E1. Read routing under worker variation

Create a fixed seeded mix of full chunks, short final chunks, and small objects. Test uniform and skewed popularity. Hold offered logical request rate constant while one worker becomes slow and later recovers; repeat with the slow worker changing over time. Separate synthetic response delay from real competing disk/CPU workload. Artificial delay models a timing disturbance, not physical media failure.

Compare routing modes under healthy, one-slow, rapidly changing, and shared-host-saturated conditions. Measure object p50/p95/p99, success/deadline rates, goodput, worker distribution, queue wait, hash cost, and controller CPU/event-loop lag. Four workers may offer limited diversity; record that limitation.

### E2. Stale feedback and rediscovery

Train an unfavorable sample for one worker, stop accesses long enough to age it, restore the worker, then issue a controlled new load. Separately delay or omit performance feedback while continuing heartbeats. Measure sample age, time until useful traffic returns, routing balance, probe count/bytes, and end-to-end latency. No disabled mode may silently use a different connection pool or timeout.

### E3. Duplicate-aware reads and overload

Run low, moderate, and near-capacity offered loads, with independent worker slowdowns and with a common bottleneck. Compare duplication modes and purging ablations. Include a fast bad replica to exercise validation before winner selection.

Measure logical completion times, primary/hedge attempted and completed work, extra transferred bytes, cancellation latency/outcomes, queue growth, rejected requests, and unfinished remote work at shutdown. Show cases where duplication helps and where budgets suppress it. Application-level priority does not imply lossless network/disk preemption; report observed interference.

### E4. Scrub policy and coverage

Create hot/cold objects and inject faults at seeded times in explicitly recorded placements. With equal checking bandwidth and required coverage, compare policies. Uniform faults and deliberately clustered worker-local faults are different workload assumptions; label them.

Measure per-fault detection delay, per-replica coverage age, maximum age, bytes read, deadline misses, and foreground latency. A policy that detects one fault sooner by never checking other placements fails coverage, even if its mean detection time looks good. Do not use HDD MLET terminology for a different metric.

### E5. Repair under foreground load

Stop one worker after a large committed upload set; continue client reads. Hold data placement and workload seed constant across repair modes. Repeat with heterogeneous **configured** transfer limits and with equal limits. Include a second failure that is survivable for some chunks and not for others; report actual per-chunk availability rather than declaring a fixed number of worker failures universally tolerable.

Measure (a) fault time to detection, (b) detection to first restored replica, (c) detection to all repairable chunks restored, (d) end-to-end outage/recovery time, (e) replication-deficit exposure, (f) sender/receiver bytes and imbalance, (g) foreground p99 and goodput, and (h) planning CPU time. Pure replication still requires writing a full replacement copy; do not claim coding-style network-byte reduction from scheduling alone.

### E6. Declarative maintenance overlap

Use real consumers: due scrubs and source reads for repair or justified placement rebalancing. Compare independent execution, coordinated scheduling with sharing disabled, and coordinated verified-read sharing. Hold completed work, deadlines, cache policy, and source data constant.

Use three cases: meaningful overlap, deliberately disjoint reads, and deadlines too tight to wait. Report application file-read counts/bytes, consumer bytes, saved reads, cache/spool space, deadline misses, and runtime overhead. Physical disk reads measured with OS instrumentation are a different metric and may be affected by page cache. Deleted work elision is reported separately from shared-read savings. Never count source and destination as one verification.

### E7. Controller batch retrieval

Use ordered lists of arbitrary small objects, e.g. 10 KiB, 100 KiB, and near-1 MB, plus mixed sizes. Vary batch size (such as 8, 32, 64, 128), lookahead, and a slow leading object. These sizes are experimental inputs, not the object store's chunk-size definition.

Compare serial GET (illustrative only), persistent pooled concurrent GET, and batch retrieval. Match the set of requested objects and use two clearly labeled controls: equal logical-object concurrency, and equal outer-client-worker count. The latter allows batches more internal concurrency and therefore cannot isolate batching overhead by itself.

Measure completion of the entire requested batch, first complete object, per-entry completion from the common batch submission time, peak RSS/spool, request count, useful bytes, and unsuccessful entries. Do not divide batch duration by entry count and label it independent object latency. Include strict and continue-on-error semantics and parser validation.

### E8. Assembly placement and combined behavior

Compare controller and worker assembly using the same manifests, reader policies, quotas, and batches. Measure placement-local bytes, peer bytes, controller proxy bytes, assembler wait, and CPU/memory. Multiple simultaneous assemblies must pass T18 before performance claims.

Finally combine the selected adaptive read, maintenance, and batch mechanisms. Measure both average user experience and fairness between ordinary GETs and large batches, plus repair/verification progress. A combined configuration can underperform because feedback loops or shared resources interact; report it and tune transparently rather than hiding the result.

## 5. Workload and statistical discipline

1. Generate deterministic datasets and request sequences using saved seeds. Record byte lengths and original SHA-256 values outside the object store. Use incompressible random bytes when testing raw transfer; label structured/compressible datasets separately.
2. Estimate sustainable baseline throughput, then choose several fixed offered loads for all policy comparisons. An improvement may change utilization; do not retune each policy's offered rate and pretend the conditions were identical.
3. Use an open-loop arrival schedule for primary latency comparisons. Timestamp intended arrival, actual submission, admission, and completion so client-side queueing and scheduling lateness are included. A closed-loop client can hide overload; label closed-loop throughput tests separately.
4. Cap client work safely and record every scheduled request not submitted, rejected, timed out, or unfinished. Do not drop pending requests at the end of a measurement window and exclude them from the denominator. Drain within a defined limit, then record remaining outcomes.
5. Warm up explicitly, then run stable measurement windows. Vary policy order and repeat with at least five seeds/runs where practical. For p99 claims seek enough completions (e.g. 10,000 or more per run) and report actual sample/tail counts; a smoke test is not a statistically strong benchmark.
6. Report run-level variation/confidence intervals and raw counts. Correlated latency samples should not be treated as independent observations merely to produce narrow confidence intervals. Merge compatible histograms or raw observations; never average client p99 values.
7. Include failures and deadlines alongside successful-latency percentiles. A fast error is not a successful low-latency download. Report both useful bytes/s and request completions/s; duplicate bytes and failed partial transfers are not goodput.
8. State application-cache and OS-cache conditions. If caches cannot be controlled without privileged host changes, report warm/unspecified OS-cache behavior rather than claim a cold-disk experiment. Do not drop host caches as a hidden prerequisite.
9. Record host hardware, Docker VM resources, CPU/memory limits, filesystem/volumes, OS/kernel, network/shaping settings, Python/SQLite/dependency versions, source commit, policies, and timeouts. A one-host experiment is not a geo-distributed availability result.

## 6. Metric definitions

- **Object latency:** intended arrival/submission (both recorded) to complete verified reception by the client, including admission and reconstruction. Whole-object spooling policy must match across modes.
- **Chunk attempt RTT:** controller/assembler dispatch to complete body reception; separate worker queue and read/hash durations.
- **Batch makespan:** common batch submission to all requested entries and a validated terminal frame. Continue-on-error completion includes failed_count; it is not all-object success.
- **Time to first object:** common batch submission to first complete validated object, not merely a response header.
- **Detection delay:** known injected-fault time to system detection. Fault-injection knowledge is experimental ground truth, not a production capability.
- **Repair completion:** a replacement is length/hash checked, durably published, and committed in controller metadata; sending the last network byte alone is insufficient.
- **Replication-deficit exposure:** integrate `sum_chunks max(0, 2 - min(2, available_valid_copies))` over time, in copy-seconds. Report zero-copy unavailability separately. In experiments distinguish controller-observed state from fault-runner ground truth; do not assume a recently verified replica can never corrupt later.
- **Coverage age:** elapsed time since a fresh eligible verification of that exact placement, not the other copy or an old cache hit.
- **Reuse saving:** equal required consumer work with fewer actual worker-file read bytes; cache hits and physical-device read savings must be labeled separately.
- **Duplicate overhead:** extra attempts/bytes and actual loser work relative to useful logical work, including uncancellable work.

## 7. Artifact contract for implemented experiments

Create one run directory per immutable configuration:

```text
results/<experiment>/<run_id>/
  config.json
  environment.json
  dataset-manifest.json
  requests.jsonl
  faults.jsonl
  events.jsonl
  metrics.json
  summary.json
  plots/
```

Document units and schemas; retain the script that generates every figure. No results currently exist in this handoff. The final report must separate original paper findings, our hypotheses, and measured project results.

## 8. Demonstration acceptance checklist

- [ ] Clean machine can build/start the project from documented commands.
- [ ] One controller and four persistent workers register and reconcile.
- [ ] Upload/download external digest matches for arbitrary files and >1 MB objects.
- [ ] Inspector shows chunk order, lengths, and two distinct worker locations.
- [ ] Metadata can be retrieved/patched without chunk re-upload.
- [ ] Stop worker2; valid reads and visible repair progress continue when possible.
- [ ] Restart worker2; inventory reconciliation safely returns to two normal placements.
- [ ] Mutate a real worker chunk; cold-data scrub detects it, fallback returns correct bytes, and repair restores it.
- [ ] Corrupt-fast versus valid-slow race cannot return bad data.
- [ ] Delete during repair/declaration work cannot resurrect an object.
- [ ] Batch parser detects missing entries, invalid digests, and truncated streams.
- [ ] Reproducible policy comparison includes overhead, errors, and hardware limits.
- [ ] README accurately distinguishes implemented, experimental, measured, and unimplemented work.

Completion requires these observable behaviors, not a screenshot of containers running or an attractive dashboard alone.
