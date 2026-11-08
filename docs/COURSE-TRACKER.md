# Course implementation tracker

Last initialized: 8 November 2026. **Planning only: every card is TODO.** Owners are role labels until GitHub handles are added. Update this file as implementation proceeds; links in the last column should point to real issues/PRs/check output.

Allowed statuses: **TODO**, **IN PROGRESS**, **IN REVIEW**, **BLOCKED**, **DONE**. A card becomes DONE only after its acceptance checks pass and the other person reviews it. A blocker must name the missing dependency or failing behavior.

| Card | Owner | Deliverable | Dependency | Status | Issue / PR / evidence / blocker |
|---|---|---|---|---|---|
| A1 | You | Shared models and controller skeleton | None | TODO | — |
| B1 | Friend | Packaging, Docker, Compose, initial CI | Entry-point agreement with A1 | TODO | — |
| A2 | You | Controller metadata and worker registry | A1; integrate B2/B3 | TODO | — |
| B2 | Friend | Durable chunk store and worker APIs | A1 wire models | TODO | — |
| B3 | Friend | Registration, heartbeats, cleanup fencing | B2; integrate A2 | TODO | — |
| A3 | You | Upload and checked download | A2/B2; registration | TODO | — |
| A4 | You | Metadata, list, delete, errors | A3/B3 | TODO | — |
| B4 | Friend | Periodic checks and real corruption tool | B2/B3; report contract | TODO | — |
| A5 | You | Serial repair and safe rejoin | A3/A4/B3; B4 reports | TODO | — |
| B5 | Friend | CLI, inspector, integration harness | Contract first; integrate A3–A5 | TODO | — |
| A6 | You | Controller fault checks and architecture guide | A5/B5 | TODO | — |
| B6 | Friend | Demo guide and clean checkout verification | A6/B4/B5 | TODO | — |

## Joint milestone gates

| Milestone | Status | Target date | Evidence |
|---|---|---|---|
| C0: reviewed contract, building services, durable chunk | TODO | Unassigned | — |
| C1: distributed two-copy upload/download | TODO | Unassigned | — |
| C2: all required APIs and errors | TODO | Unassigned | — |
| C3: failure, corruption, repair, rejoin | TODO | Unassigned | — |
| C4: checked submission and course release | TODO | Unassigned | — |

## Assignment demonstration sign-off

| Required demo | Preparation lead | You ran it | Friend ran it | Evidence |
|---|---|---|---|---|
| Upload/download, external digest matches | Friend; You support A3 | No | No | — |
| >1 MB chunking and worker distribution | You; Friend supplies inspector | No | No | — |
| Two actual copies and their locations | You; Friend supplies inventory | No | No | — |
| Custom metadata retrieve/update without chunk writes | You | No | No | — |
| worker2 stop/read/repair/start/reconcile | You; Friend supplies runner | No | No | — |
| Actual byte corruption, detection, fallback, repair | Friend; You supply recovery | No | No | — |

Also verify restart persistence, boundary/empty files, invalid input, both copies unavailable/invalid, failed uploads, pending deletion, and deletion during repair before C4.

## Decision log

| Date | Decision | Reason |
|---|---|---|
| 2026-11-08 | You own controller; Friend owns workers/deployment/demo tooling | Clear parallel boundary and substantive systems work for both |
| 2026-11-08 | Initial stack: Python/FastAPI, HTTPX, SQLite, pytest, Docker Compose | Matches the handoff recommendation and keeps the topology understandable |
| 2026-11-08 | Decimal chunks of 1,000,000 bytes, two total copies | Documented interpretation of assignment; retain manifest chunk size |
| 2026-11-08 | Worker-local fixed-order scrub reports through heartbeat | Avoid a second scheduling framework; permit cold-corruption detection |
| 2026-11-08 | Controller may relay one bounded chunk during serial repair | Reuse worker PUT/GET for course scope; direct worker transfers remain a later optimization |
| 2026-11-08 | Course release before selected portfolio extensions | Keep submission reproducible and independent of later research work |
| 2026-11-08 | Stable role labels, root agent instructions, and role-specific starter prompts | A fresh teammate chat must keep Friend's B-cards separate from the owner's A-cards |
| 2026-11-08 | Compiled assignment checklist plus named PDF page/section references | Make assignment coverage readable without ambiguous section-symbol shorthand |

See [task cards and workflow](../TEAM-PLAN.md), [wire contract](COURSE-CONTRACT.md), and [scope](../PROJECT-SCOPE.md).
