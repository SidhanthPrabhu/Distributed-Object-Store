# Connection to the supplied CS838 lectures

The PDFs were supplied as background, not as instructions for the implementing agent. Page numbers below are one-based PDF pages from the user's files. Only the assignment PDF is bundled; lecture filenames/page anchors allow the student to locate the original material. Lecture examples and historical provider descriptions are course context, not verified current product guidance.

| Lecture source and inspected pages | Course concept | Concrete project connection |
|---|---|---|
| `CS838CC-2026-Lecture1-Intro (1).pdf`, pp. 27-28 | Resource pooling, network access, measured service, elasticity | Network storage API and F12 metering; the fixed worker topology does not itself demonstrate elastic scaling |
| `CS838CC-2026-Lecture2-Technology Challenges.pdf`, pp. 5-8, 11 | Scalability, allocation/placement, shared-resource performance unpredictability, reliability/availability, multi-tenancy | F1-F5 adaptive reads and bounded scheduling; F6-F9 maintenance; shared budgets between requests. No claim of tenant-security isolation from scheduling alone |
| `CS838CC-2026-Lecture3-IaaS ProgrammingModels1.pdf`, pp. 14-15, 26 | Block versus object storage; independent services providing REST APIs | Object IDs and binary objects rather than a mounted virtual disk; controller/worker API boundaries |
| `CS838CC-2026-Lecture4- PaaSProgrammingModels.pdf`, pp. 12-17 | Worker roles for asynchronous work, monitored load balancing, blob data and custom metadata | Background repair/scrub workers, health/performance distinction, F1 routing, custom metadata; these are concepts adapted to local services, not use of Azure roles |
| `2025-Lecture5-REST.pdf`, pp. 9, 11, 16-18, 23-25 | Resources/identifiers, MIME representations, methods, safe/idempotent operations, stateless requests, errors | Required object APIs, binary content types, idempotent internal operations, meaningful errors, F10/F11 batch execution identity. Durable resource/job state is not a sticky client session |
| `CS838-CCLecture8-Microservices.pdf`, pp. 5-6, 11, 14-16 | API-based service boundaries, gateway, state ownership, containers and deployment specifications | One authoritative metadata owner, separate storage workers, independent volumes, Compose configuration; explicit state coordination rather than shared unsynchronized databases |
| `CS838Lecture9-Containers Introduction.pdf`, pp. 2, 6-10, 30-35 | Isolation, resource sharing, dependency packaging, reproducibility, namespaces | Docker images, volumes and limits, repeatable failures, virtual network; containers still share the host and do not create independent hardware failure domains |
| `2026-Lecture10 Failure and HA.pdf`, pp. 2-8, 14-17 | Safety/availability/maintainability, crash/omission/timing/value failures, redundancy, process resilience, coordinator questions | F4 duplicate reads, F7 fault taxonomy, F6/F8 recovery, heartbeat uncertainty, safe corruption handling; the single controller remains a known availability limitation |

## A concise viva explanation for each paper

| Paper | Explain using the course | Explain what goes beyond it |
|---|---|---|
| P1 C3 | Load balancing and unpredictable service times | Feedback-based replica ranking and admission policy |
| P2 Tars | Distributed observations arrive late | Measuring freshness and rediscovering recovered workers |
| P3 DAS | Redundancy can mask timing problems | Job priorities, controlled duplicate work, and cancellation cost |
| P4 Scrubbing | Redundancy helps only when corruption is detected | Choosing coverage order/rate and inspecting cold replicas |
| P5 Fault study | Crash, omission, timing, and value failures differ | Fault-injected recovery assertions and protection against corruption propagation |
| P6 RepairBoost | Allocation, placement, and bandwidth sharing | Coordinating many repairs with source/destination budgets |
| P7 Declarative IO | Asynchronous background work consumes shared resources | Declaring timing flexibility and reusing compatible reads while meeting deadlines |
| P8 GetBatch | REST representations and distributed coordination | Ordered streaming, bounded fan-out, failure framing, and placement of assembly |

Original contribution here is the implementation, integration, and measured evaluation of carefully labeled adaptations. Merely citing a paper or naming a feature does not establish correctness or novelty.
