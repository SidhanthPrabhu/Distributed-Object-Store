# Course implementation tracker

Last updated 9 November 2026. **The A1/A2 and B1/B2 foundations are in review. Joint milestones remain open.** Owners are role labels until GitHub handles are added. Update this file as implementation proceeds and link to commits, issues, or check results.

Allowed statuses: **TODO**, **IN PROGRESS**, **IN REVIEW**, **BLOCKED**, **DONE**. A card becomes DONE only after its acceptance checks pass and the other person reviews it. A blocker must name the missing dependency or failing behavior.

| Card | Owner | Deliverable | Dependency | Status | Commit / issue / evidence / blocker |
|---|---|---|---|---|---|
| A1 | You | Shared models and controller skeleton | None | IN REVIEW | `codex/a1-controller-foundation`: shared configuration/wire models, factory, error/health behavior, owned SQLite/HTTPX lifespan. [Evidence and handoff](CONTROLLER-FOUNDATION.md): 56 controller checks passed; compile check passed. Friend review and imports/error/lifecycle checks from the real B1/B2 worker application remain pending; no worker app or packaging is available in this checkout. |
| B1 | Friend | Packaging, Docker, Compose, initial CI | Entry-point agreement with A1 | IN REVIEW | Locked Python 3.12 packaging, shared image, five services with separate volumes, and pytest CI. Lock, compile, and Compose config checks passed. The real five-service build and restart preserved worker storage IDs and controller registration records. [Evidence](WORKER-FOUNDATION.md). Human review, hosted CI run, and ready-state acceptance with B3 and A2 reconciliation are pending. |
| A2 | You | Controller metadata and worker registry | A1; B1 packaging/persistent deployment; B2 inventory/checked GET/durable receipts; B3 persistent identities/heartbeat/tombstone cleanup | IN REVIEW | 9 Nov 2026: foundation extended with configured-address bounded inventory requests, worker/storage/boot and manifest/intent comparison, fresh replica checks, guarded HEALTHY transitions, expiry/backlog exclusion, durable placement faults, interrupted-upload recovery, validated cleanup acknowledgements/retries, schema-v1 upgrade, preserved manifests/epochs/sequences and immutable expected hashes. [A2/A3 handoff and exact dependencies](CONTROLLER-A2-A3.md): 113 controller checks passed with contract doubles; compile and whitespace checks passed. **Remaining:** Friend review; B1 real packaged services/persistent volumes, B2 actual inventory/checked reads and durable storage, B3 restart/heartbeat/terminal cleanup fencing; real four-worker, expiry, controller/worker restart and pending-cleanup acceptance. Unknown/ambiguous ACTIVE extras stay RECONCILING for A5 recovery. C0/C1 remain open; no real-service gate passed. |
| B2 | Friend | Durable chunk store and worker APIs | A1 wire models | IN REVIEW | Durable PUT/GET/DELETE/inventory using the shared models. 13 worker checks and all 56 controller checks passed. A checked 1,000,000-byte chunk survived a real Compose restart. [Evidence and handoff](WORKER-FOUNDATION.md). Human review and use by the A2/A3 controller paths remain pending. B3 owns registration, heartbeats, and terminal tombstones. |
| B3 | Friend | Registration, heartbeats, cleanup fencing | B2; integrate A2 | TODO | — |
| A3 | You | Upload and checked download | A2; B1 adds python-multipart and locked runtime/deployment; B2 idempotent durable PUT/checked GET/inventory; B3 registration/heartbeat/tombstone cleanup | IN REVIEW | 9 Nov 2026: multipart POST/201 and binary GET/200; UUIDs, ordered decimal chunks/empty files, persisted rotating capacity-aware pairs, intent-before-PUT/stable retries/validated receipts/two-copy publication, hidden incomplete uploads, terminal failed/interrupted-upload cleanup, checked fallback and full-object verification in quota-limited temporary storage with failure/disconnect cleanup. [Evidence/handoff](CONTROLLER-A2-A3.md): 113 total controller checks passed, including 0/1/C−1/C/C+1/2C/2C+1, corrupt/missing/no-valid-copy reads, identity/receipt/expiry races, reply loss/cancellation, restart and cleanup fencing; compile/whitespace checks passed. **Remaining:** Friend review and actual arbitrary-file/>1 MB/two-copy/restart/cleanup checks with B1/B2/B3 persistent services; doubles do not prove remote durability or C1. Public metadata/list/delete and serial repair remain A4/A5. No teammate-owned files changed. |
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
| 2026-11-08 | Both contributors commit and push directly to main; no task branches or pull requests required | Owner preference; synchronize before pushing and preserve technical review/acceptance evidence separately |
| 2026-11-08 | Stable role labels, root agent instructions, and role-specific starter prompts | A fresh teammate chat must keep Friend's B-cards separate from the owner's A-cards |
| 2026-11-08 | Compiled assignment checklist plus named PDF page/section references | Make assignment coverage readable without ambiguous section-symbol shorthand |

See [task cards and workflow](../TEAM-PLAN.md), [wire contract](COURSE-CONTRACT.md), and [scope](../PROJECT-SCOPE.md).
