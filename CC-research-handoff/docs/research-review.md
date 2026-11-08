# Review of the eight primary papers

Reviewed on 8 November 2026 for the CS838 mini distributed cloud object store.

This is a full-text engineering review of the relevant methods, evaluations, and limitations, rather than an abstract-only ranking. It does not reproduce the authors' experiments, independently prove their theorems, or establish new research results. PDF page numbers below count the PDF cover when one exists. The **source findings** paragraphs describe the papers; **our adaptation** paragraphs propose new work for this particular project. Proposed policies and parameter choices are not claims made by the authors.

All eight public PDFs were downloaded and parsed successfully. See [source manifest](../research/sources.json), [download provenance and SHA-256 hashes](../research/downloads.json), and [coverage index](../research/coverage-index.json). Local PDF caches are intentionally excluded from the portable handoff; `python3 scripts/fetch_research.py` retrieves them.

## P1. C3: Cutting Tail Latency in Cloud Data Stores via Adaptive Replica Selection

- Authors: Lalith Suresh, Marco Canini, Stefan Schmid, Anja Feldmann.
- Publication: NSDI 2015.
- [Paper page](https://www.usenix.org/conference/nsdi15/technical-sessions/presentation/suresh) | [PDF](https://www.usenix.org/system/files/conference/nsdi15/nsdi15-paper-suresh.pdf).
- Anchors: design sections 3.1-3.3, PDF pp. 5-8; implementation and evaluation sections 4-6, pp. 8-13; discussion section 7, pp. 13-14. Algorithm 2's cubic exponent was also checked in the rendered PDF.

**Source findings.** C3 combines replica ranking with per-server rate control and backpressure. Responses carry queue and service information; clients track outstanding requests and smooth feedback. Its score penalizes long queues nonlinearly. Token buckets prevent routing alone from overloading replicas. Evaluation includes Cassandra on 15 EC2 nodes with three replicas and small records, plus simulations. The discussion distinguishes selecting one replica from stronger read-consistency protocols. It also reports that speculative retries can hurt under high utilization. These conditions differ substantially from a four-worker object store transferring 1 MB chunks.

**Our adaptation: implement F1 and F3.** Put a pluggable selector inside the controller's chunk reader. Offer random, round-robin, least-outstanding, response-time EWMA, and a queue-informed adaptive policy. Count actual worker operations, including hedges and maintenance, rather than counting only outer HTTP requests. Use one bounded scheduling/admission layer shared by all controller modules.

Keep a named experimental `c3_reference` ranking mode with its equation explicitly documented. For this centralized controller, use one logical scheduler client, not the number of public API callers as the paper's client multiplier. The practical default should use directly observed outstanding requests and bounded feedback estimates. Variable chunk lengths require either size buckets or byte-aware comparisons. Do not call the resulting system an exact C3 reproduction.

**Course fit.** Workload placement, load balancing, performance unpredictability, controller/worker communication, and shared resources.

**Project/resume value.** High: an explainable routing decision and a controlled baseline comparison are substantial systems work. Evidence must include whole-object latency, not only a microbenchmark of the score function.

**Acceptance and experiment.** Inject a slowdown, then remove it. Confirm routing shifts away and returns without unbounded queue growth. Test single-worker and shared-host bottlenecks separately. Compare F1 with F3 disabled/enabled; selector overhead, timeout rate, goodput, p50/p95/p99, and recovery-to-normal all matter. See experiments E1-E3.

## P2. Tars: Timeliness-aware Adaptive Replica Selection for Key-Value Stores

- Authors: Wanchun Jiang, Liyuan Fang, Haiming Xie, Xiangqian Zhou, Jianxin Wang.
- Reviewed version: arXiv:1702.08172v1, 2017. Its submission comment does not by itself establish conference acceptance.
- [Record](https://arxiv.org/abs/1702.08172) | [PDF](https://arxiv.org/pdf/1702.08172v1).
- Anchors: motivation section III, PDF pp. 3-5; design IV, pp. 5-7; evaluation V and limitations VI, pp. 7-10.

**Source findings.** Tars examines stale feedback in C3 and modifies ranking and rate-decrease decisions. It distinguishes server residence time, aggregate service rate, and request arrival rate. Its ranking changes behavior when feedback is old and allows a previously avoided server to be tried again. The paper uses a 100 ms freshness boundary and a queue threshold of five in its setup; these are not universal constants. Evaluation is simulation-based, using three replicas and 50 servers. It explicitly leaves real-system evaluation and a complete solution to stale information as further work.

**Our adaptation: implement F2.** Timestamp feedback when the controller receives it using a monotonic clock. Keep liveness heartbeats separate from data-path performance samples. Use bounded rediscovery probes for healthy-but-stale workers; a worker marked unavailable must re-register/reconcile instead. Invalidate performance samples when a worker boot ID changes. Store small-object and full-chunk measurements separately so a cheap probe cannot make a slow 1 MB transfer look fast.

Measure residence and queue time using worker-local durations; do not subtract absolute timestamps on different machines. RTT includes network transfer, queueing, read, hashing, and scheduling, so do not count the same queue delay twice. Count request completions over a window to estimate aggregate worker capacity; reciprocal service time is not aggregate capacity when operations run concurrently.

**Course fit.** Distributed observations, delayed information, performance variability, and resource allocation.

**Project/resume value.** High as a refinement of the adaptive reader, not a separate headline merely because it has a separate paper. A recovery-after-slowdown demonstration is particularly useful.

**Acceptance and experiment.** Withhold performance feedback, create an idle interval, and restore a previously slow worker. Compare identical policies with freshness and rediscovery switched off/on. Count probe cost and demonstrate that stale scores do not starve a healthy replica indefinitely. See E2.

## P3. Reducing Tail Latency via Safe and Simple Duplication

- Authors: Hafiz Mohsin Bashir, Abdullah Bin Faisal, Muhammad Asim Jamshed, Peter Vondras, Ali Musa Iftikhar, Ihsan Ayyub Qazi, Fahad R. Dogar.
- Reviewed version: arXiv:1905.13352v1, 2019. Use this exact title/version for this review; do not silently cite a differently titled later version.
- [Record](https://arxiv.org/abs/1905.13352) | [PDF](https://arxiv.org/pdf/1905.13352v1).
- Anchors: DAS section 2.2, PDF pp. 3-4; D-Stage design and proxy, sections 3.1-3.5, pp. 4-7; implementation/evaluation/discussion, sections 4-7, pp. 8-12.

**Source findings.** DAS normally creates a duplicate as a job enters, gives primaries strict priority over duplicates, and purges work after a copy finishes. D-Stages carry consistent job identity across resource layers; a proxy throttles submission into an unmodified layer to preserve queue control. The evaluation covers HDFS, network transfers, and an IDS, using public-cloud and controlled testbeds. The argument for safety relies on resource-level prioritization/preemption and removal of redundant work. The measurements also show that real prioritization has overhead. DAS is not simply delayed hedging.

**Our adaptation: implement F4 and F5.** Include delayed, budgeted hedging as the practical policy and eager low-priority duplication as an experimental mode. Both use the same job IDs, worker queues, bounded admission, and cancellation machinery. A chunk operation can have at most two attempts because there are two replicas; neither retries nor repair may recursively generate hedges.

The winner is the first fully received, length-checked, checksum-valid chunk. A faster corrupt response never wins. Cancelling queued work should remove it immediately; an already running blocking read may finish, and its cost must be measured. A worker validates cancellation before publishing a response. Close controller-side loser response bodies and release their connections and permits.

Do not describe application queue priorities as strict network/disk preemption. An optional Linux testbed can add measured network/I/O prioritization, but ordinary Docker execution must remain functional without privileged tuning. Preserve eager-mode experiments as useful work rather than removing them because delayed hedging also exists.

**Course fit.** Redundancy, transient slowness versus failure, scheduling, availability, and resource contention.

**Project/resume value.** High if the resource overhead and overload behavior are evaluated. An uncontrolled race between two HTTP requests is a much weaker implementation.

**Acceptance and experiment.** Race valid/corrupt responses, disconnect the client, and cancel before/after worker dispatch. Confirm one result, no leaked tasks or permits, bounded duplication, and accounting for unfinished loser work. Compare no duplication, naive eager duplication, delayed hedging, and prioritized eager duplication. See E3.

## P4. A Clean-Slate Look at Disk Scrubbing

- Authors: Alina Oprea and Ari Juels.
- Publication: FAST 2010.
- [Paper page](https://www.usenix.org/conference/fast-10/clean-slate-look-disk-scrubbing) | [PDF](https://www.usenix.org/event/fast10/tech/full_papers/oprea.pdf).
- Anchors: error model section 3; strategies 4.1-4.4, PDF pp. 6-8; simulation metric/evaluation section 5, pp. 8-13; limitations/conclusion pp. 13-14.

**Source findings.** The paper studies HDD latent-sector errors and the choice of scrubbing order and rate. It investigates staggered scans and accelerated checking after an observed error. Its mean latent error time (MLET) measures the fraction of operating time with an undetected error, rather than ordinary per-error detection latency. Results rely on a modeled distribution of disk faults and parameters such as bit-error behavior and workload. Physical locality, disk-head movement, and manufacturer parameters matter; the authors explicitly identify limits to transferring their model to flash devices.

**Our adaptation: implement F6.** Maintain verification timestamps and deadlines per physical replica placement. Normal checked reads can refresh only the replica actually read. The other copy still needs checking. Cold objects must receive coverage even when popular objects are continuously read.

Provide fixed-cycle sequential checking as a baseline, oldest-verification-first checking, and bounded worker-history escalation after corruption. Keep an absolute maximum verification age as a starvation guard. An error increases inspection of that worker's other chunks but does not prove adjacent chunk IDs occupy adjacent disk sectors. Round-robin worker/group coverage is an application policy, not a reproduction of physical staggered scanning. A controlled simulation with declared clustered faults can investigate that hypothesis separately.

**Course fit.** Availability, integrity, redundancy, background work, and shared-resource performance.

**Project/resume value.** High: discovering damage in an object that nobody downloads is a meaningful improvement beyond on-demand checksum checking.

**Acceptance and experiment.** Corrupt a cold replica at a known test time and verify detection without a download. Check coverage of both placements, deadline misses, and foreground impact under identical checking budgets. Report detection latency and our own replica-risk exposure metric; do not call either MLET. See E4.

## P5. Redundancy Does Not Imply Fault Tolerance

- Full title: Redundancy Does Not Imply Fault Tolerance: Analysis of Distributed Storage Reactions to Single Errors and Corruptions.
- Authors: Aishwarya Ganesan, Ramnatthan Alagappan, Andrea C. Arpaci-Dusseau, Remzi H. Arpaci-Dusseau.
- Publication: FAST 2017; reviewed the conference paper rather than the shorter magazine article.
- [Paper page](https://www.usenix.org/conference/fast17/technical-sessions/presentation/ganesan) | [PDF](https://www.usenix.org/system/files/conference/fast17/fast17-ganesan.pdf).
- Anchors: fault model/methodology sections 3.1-3.2, PDF pp. 4-6; eight-system study 4.1, pp. 6-10; cross-system observations and discussion 4.2-4.4, pp. 10-13. Later pages include references and extended results.

**Source findings.** CORDS injects single-node, single-block faults into application data: corrupted reads, read/write I/O failures, and space errors. It records local behavior and cluster-visible effects across eight historical distributed-system versions. It finds missed integrity checks, inadequate use of replicas for recovery, corruption spread, and confusion between crash and corruption recovery. It is an empirical fault study, not a new replica-placement or consensus algorithm. Its findings about those versions should not be stated as current bugs in the latest products.

**Our adaptation: implement F7 as a correctness workstream.** Build deterministic storage-boundary fault injection and a scenario runner, preserving a known original digest outside the system under test. Distinguish byte corruption, missing chunk, timeout, disk error, short write, and out-of-space. Prevent invalid data from being used for repair even if the controller previously considered that placement healthy.

Use immutable chunk identities plus expected lengths/digests anchored in controller metadata. Commit successful uploads only after both durable replica receipts. Treat incomplete uploads and corruption as different states. Keep repair writes atomic, revalidate object lifecycle before placement changes, and fence delayed jobs after deletion or controller restart. These are our project design choices, motivated by the need to test recovery end to end.

**Course fit.** Failure models, safety versus availability, fault masking, and process resilience.

**Project/resume value.** Very high. A documented fault matrix and demonstrated recovery correctness support every optimization claim. Simply adding another checksum field does not constitute this contribution.

**Acceptance and experiment.** See T1-T18 and E0/E5. Include both copies corrupted: fail clearly, retain diagnostic state, and never manufacture success. If one copy is unavailable but may return, classify temporary unavailability separately from confirmed unrecoverable bytes.

## P6. Boosting Full-Node Repair in Erasure-Coded Storage (RepairBoost)

- Authors: Shiyao Lin, Guowen Gong, Zhirong Shen, Patrick P. C. Lee, Jiwu Shu.
- Publication: USENIX ATC 2021.
- [Paper page](https://www.usenix.org/conference/atc21/presentation/lin) | [Author PDF](https://www.cse.cuhk.edu.hk/~pclee/www/pubs/atc21repairboost.pdf).
- Anchors: assumptions/design 3.1-3.4, PDF pp. 4-8; implementation section 4, p. 8; evaluation 5.1-5.4, pp. 9-12.

**Source findings.** RepairBoost represents coded repair dependencies as DAGs, maps tasks to balance sender and receiver traffic, then schedules transmissions using a maximum-flow formulation. Its extensions consider heterogeneous bandwidth and multiple failures. The evaluation uses 16 worker instances plus a coordinator, coded chunks of 64 MB by default, and separate coding/repair algorithms. The improvement concerns coordinating many repairs, not merely reducing a single transfer's latency. Its coding DAGs do not directly describe this project's two-copy replication.

**Our adaptation: implement F8.** Represent a replica repair as a dependency chain: validate source, transfer, validate and durably publish destination, commit placement, then retire any excess copy. Across many chains, allocate sender, receiver, disk, and global maintenance budgets. Choose a destination with capacity that does not already hold that logical chunk.

With one of two replicas unavailable, source choice is normally fixed. Optimize eligible destination choice and concurrent transfer ordering, not imaginary helper-node reconstruction. Compare a simple queue against a byte/capacity-aware planner. Retain an experimental matching scheduler over eligible source/destination edges; at four workers its overhead and lack of benefit are valid outcomes. Cap an epoch's work and replan when health changes instead of imposing a barrier until all transfers finish.

Urgent repairs cannot wait for speculative I/O overlap. Preserve foreground capacity and a minimum maintenance progress share. A node still holding an old offline copy may make a temporary third copy when it returns; reconciliation must verify, choose two, and delete the excess without sacrificing the last good data.

**Course fit.** Failure recovery, scheduling, placement, bandwidth sharing, and controller/worker coordination.

**Project/resume value.** High when tested during a full-worker outage with an active client workload. This is a scheduling adaptation; no erasure-code speedup claim transfers automatically.

**Acceptance and experiment.** Compare sequential, fixed-concurrency, and capacity-aware repair. Measure failure-detection delay separately from post-detection repair time, replica-risk exposure, traffic, and foreground p99. See E5.

## P7. Scaling the IO Wall with Declarative IO (DINGO)

- Authors: Sanjith Athlur, Sara McAllister, Theo Gregersen, Timothy Kim, Yiwei Chen, Sarvesh Tandon, Lucy Wang, Daniel S. Berger, Saurabh Kadekodi, Arif Merchant, Benjamin Berg, Nathan Beckmann, Rashmi Vinayak, George Amvrosiadis, Gregory R. Ganger.
- Publication: OSDI 2026.
- [Author page](https://www.pdl.cmu.edu/PDL-FTP/Storage/Declarative_IO-OSDI26_abs.shtml) | [PDF](https://www.pdl.cmu.edu/PDL-FTP/Storage/Declarative_IO-OSDI26.pdf) | [Artifact](https://github.com/Thesys-lab/declarativeIO-osdi26-artifact).
- Anchors: interface/consistency 4.1-4.4, PDF pp. 6-8; planner/dispatch/cache 5.1-5.2, pp. 9-10; evaluation/limitations sections 6-8, pp. 10-14; appendices contain additional interface details.

**Source findings.** Declarative IO exposes sets of upcoming reads, timing/order/data flexibility, and deadlines. Callbacks prompt tasks to perform ordinary reads; the callback does not deliver the data. DINGO schedules toward required progress rates and groups overlapping accesses, with explicitly managed cache space to capture reuse. It targets sealed immutable blocks, handles deletion races through existing read semantics, and permits urgent work to bypass declarations. Evidence includes an HDFS prototype and larger simulations. Its savings are maintenance-read savings, not elimination of writes or a guaranteed foreground-latency reduction.

**Our adaptation: implement F9.** Add a small internal declaration API for chunk checks and flexible placement maintenance. Use `(chunk_id, generation, source_worker_id, source_boot_id, verification_epoch)` as the reuse identity. Two replicas of one chunk are different inspection targets and must never count as one scrub read.

Schedule urgent work first, then declarations by remaining required byte rate; choose overlap as a tie-breaker within feasible deadlines. Use a bounded dispatch group and in-flight verified-buffer sharing at the worker or source-reader boundary. That buffer-sharing implementation differs from DINGO's callback/cache mechanism and must be labeled accordingly. Only a read occurring during the applicable checking interval can satisfy a current scrub declaration; an old cached success cannot indefinitely defer physical replica inspection.

If all tasks access disjoint data, there is no reuse saving to claim. Run overlapping and disjoint scenarios, with equal completed work, rather than manufacturing savings by silently skipping scrub work. Use a directly inspected worker read counter for application-level reuse; report physical disk I/O separately if measurable.

**Course fit.** Background services, scheduling flexibility, resource utilization, immutable object storage, and distributed consistency boundaries.

**Project/resume value.** High as a distinct extension once real maintenance consumers exist. It remains in the plan without depending on a claim that every workload benefits.

**Acceptance and experiment.** Shared read satisfies source verification and source repair-read work exactly once, but never the destination's verification or another replica's scrub. Delete while a declaration waits; observe cancellation, not resurrection. See E6.

## P8. GetBatch: Distributed Multi-Object Retrieval for ML Data Loading

- Authors: Alex Aizman, Abhishek Gaikwad, Piotr Zelasko (NVIDIA).
- Reviewed version: arXiv:2602.22434v1, February 2026, explicitly a preprint.
- [Record](https://arxiv.org/abs/2602.22434) | [PDF](https://arxiv.org/pdf/2602.22434v1) | [AIStore documentation](https://github.com/NVIDIA/aistore/blob/main/docs/get_batch.md).
- Anchors: methods 2.2-2.5, PDF pp. 2-5; synthetic evaluation 3 and training evaluation 4, pp. 5-7; discussion/limitations 5, pp. 8-9.

**Source findings.** GetBatch retrieves a requested ordered collection through distributed senders and a designated target that assembles a stream. It includes streaming, per-entry errors, bounded recovery, pressure-based admission, and metrics separating peer waits from local throttling. Synthetic experiments use a 16-node NVMe storage cluster and substantial client resources. Benefits vary with object size and workload; ordering introduces a serialization point. Its published API uses GET with a JSON body, and archive-member extraction is part of the original system. Neither choice is mandatory for this project.

**Our adaptation: implement F10 and retain F11.** First use the existing controller as a batch coordinator with a POST JSON request, deterministic order, bounded read-ahead, and binary-safe framed results. It invokes the same checked chunk reader as ordinary GET. Include a streaming client/parser and an explicit final completion record so truncated responses cannot masquerade as successful batches.

Retain a worker-designated assembly mode as a worthwhile later extension: the controller supplies a fixed manifest and attempt plan to one of the four workers, which retrieves verified chunks from peers. This preserves the fixed worker count. Use a bounded proxy stream initially; authenticated redirects and archive-member extraction require separate API work and are not prerequisites for useful batching.

**Course fit.** REST resources/representations, coordinator/worker roles, distributed fan-out, load balancing, and scalability.

**Project/resume value.** High as a distinct usable API. Demonstrate it with many small arbitrary files; GPUs or a training stack are unnecessary.

**Acceptance and experiment.** Compare with pooled concurrent individual GETs at matched logical work and resource limits, not only a serial client. Include a slow first entry, repeated IDs, missing entries, invalid bytes, and client disconnection. Report batch makespan, time to first complete object, memory/spool use, and sender/assembler waits. See E7-E8.

## Decisions after reviewing all eight

All eight contribute useful mechanisms or testing principles. The plan retains F1-F11 plus F12 (the observability and experiment harness that makes them assessable). Implementation ordering follows correctness and dependency constraints, not a fixed feature quota. Experimental policies stay selectable and independently measurable.

Do not introduce erasure coding merely to use a paper, change the required topology, claim physical disk reliability from synthetic Docker faults, or imply paper-reported percentage improvements were achieved here. Do preserve useful adaptations with explicit provenance and test them. Source assumptions and project invariants take precedence over an attractive benchmark number.

The next task is implementation using the [README](../README.md), [system specification](system-spec.md), [feature specification](feature-spec.md), and [validation plan](validation-plan.md). This review does not claim the application is already implemented.
