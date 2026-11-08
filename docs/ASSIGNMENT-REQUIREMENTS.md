# Original assignment: readable requirements checklist

This is a compiled summary of the supplied three-page **Cloud Computing — Infrastructure Project: Mini Distributed Cloud Object Store** assignment. The [original PDF](../CC-research-handoff/references/assignment.pdf) is authoritative. Page numbers below refer to that PDF, counted from 1. This document adds ownership and acceptance pointers; it does not add grading criteria. All implementation remains TODO.

The root `Mini_Distributed_Cloud_Object_Store.pdf` is an identical copy of the bundled assignment, verified by SHA-256 during preparation. Use the handoff copy linked above as the canonical documentation link.

## 1. Project objective — PDF page 1, section 1

Store and retrieve arbitrary objects across multiple storage workers, including documents, images, videos, audio, compressed files, and other binary data. Split objects into chunks and replicate them.

**Owned by:** A3 and B2; demonstrated by B5/B6.

## 2. System configuration — PDF page 1, section 2

| Requirement | Required value |
|---|---|
| Controllers | 1 |
| Storage workers | 4 |
| Clients | 1 or more |
| Chunk size | 1 MB |
| Replication factor | 2 |
| Deployment | Runnable locally with Docker; Docker Compose allowed |
| Worker storage | Persistent |
| Worker orchestration | Kubernetes must not be used |

**Owned by:** B1 deployment, B2 persistence, A3 chunking/placement. The assignment does not specify a programming language or database.

## 3. Objects and metadata — PDF page 1, section 3

Every object needs a unique Object ID. Store at least:

- Object name and size.
- Content type.
- Creation and last-modified times.
- Checksum.
- Number of chunks.
- Chunk information and storage locations.
- Custom user-defined metadata as key-value pairs.

**Owned by:** A2/A3/A4; worker storage evidence comes from B2.

## 4. Chunking and replication — PDF page 1, section 4

- Split objects into 1 MB chunks; the final chunk may be smaller.
- Give every chunk a unique identifier and preserve its order within the object.
- Keep two total copies of each chunk during normal operation, on different workers.
- Distribute chunks across available workers.
- Detect when the required replication factor is missing and restore it when possible.

**Owned by:** A3 chunking/placement and A5 recovery; B2 stores the copies and B3 reports worker state. Automatic restoration is required; merely detecting a missing copy is insufficient.

## 5. Client REST API — PDF pages 1–2, section 5

| Method | Endpoint | Required behavior | Success | Owner |
|---|---|---|---|---|
| POST | `/api/v1/objects` | Multipart upload; required binary `file`, optional `metadata` containing JSON key-value data; assign an ID and return information about the new object | 201 | A3 |
| GET | `/api/v1/objects/{object_id}` | Return the complete original binary object with an appropriate Content-Type; client need not know worker locations | 200 | A3 |
| GET | `/api/v1/objects/{object_id}/metadata` | Return system and custom metadata without downloading data | 200 | A4 |
| PATCH | `/api/v1/objects/{object_id}/metadata` | Add/update custom metadata from a JSON body without re-uploading data | 200 | A4 |
| DELETE | `/api/v1/objects/{object_id}` | Delete the object and associated stored data | 204 | A4/B3 |
| GET | `/api/v1/objects` | List at least Object ID, name, size, and content type; pagination is optional | 200 | A4 |

The route table starts on page 1. Detailed upload/download/metadata/update/delete/list rules and errors are on page 2, sections 5.1–5.7. Structured data uses JSON; file data uses an appropriate binary HTTP representation. Unknown objects return 404 as specified by the relevant routes.

### Error handling — PDF page 2, section 5.7

| Status | Assignment meaning |
|---|---|
| 200 | Successful request |
| 201 | Object created |
| 204 | Successful deletion |
| 400 | Invalid request |
| 404 | Object not found |
| 409 | Conflict |
| 500 | Internal server error |
| 503 | Required service/storage unavailable |

Errors must include a meaningful JSON message. **Owned by:** A1/A4 for common/public behavior, B2/B3 for internal errors; B5 exercises the system.

## 6. Internal worker APIs — PDF pages 2–3, section 6

| Method | Endpoint | Purpose | Served by / owner |
|---|---|---|---|
| POST | `/internal/workers/register` | Register a worker with the controller | Controller A2; sender B3 |
| POST | `/internal/workers/heartbeat` | Report worker availability | Controller A2; sender B3 |
| PUT | `/internal/chunks/{chunk_id}` | Store a chunk | Worker B2 |
| GET | `/internal/chunks/{chunk_id}` | Retrieve a chunk | Worker B2 |
| DELETE | `/internal/chunks/{chunk_id}` | Delete a chunk | Worker B2 |
| GET | `/internal/workers/{worker_id}/chunks` | Report stored chunks | Worker B2; consumer A2/A5 |

The registration entry is on page 2 and the remaining entries are on page 3. The assignment allows extended request/response formats. See [our course contract](COURSE-CONTRACT.md) for the chosen payloads, headers, and small supporting extensions.

## 7. Worker failures — PDF page 3, section 7

Detect unavailable workers and keep serving objects when sufficient copies of every required chunk remain available. Detect missing replication. Demonstrate behavior before, during, and after stopping and restarting a worker, including whether replication is maintained:

```bash
docker stop worker2
docker start worker2
```

**Owned by:** A2/A3/A5 and B3; B6 prepares the demonstration. These commands are assignment requirements for the eventual application; the planning repository cannot run them yet.

## 8. Data integrity — PDF page 3, section 8

Detect stored-data corruption. Avoid returning corrupted data when a valid copy is available. Restore corrupted data when possible. Demonstrate intentional corruption of actual stored data.

**Owned by:** B2/B4 detection and mutation tooling; A3/A5 fallback/recovery; B6 demonstration. The assignment does not require a particular scanning algorithm or research-paper reproduction.

## 9. Deployment and instructions — PDF page 3, section 9

Deploy 1 controller, 4 workers, and at least 1 client locally using Docker. Give each worker persistent storage. Include build, startup, and interaction instructions. Compose is allowed; Kubernetes worker orchestration is not.

**Owned by:** B1 deployment and B6 instructions, with both teammates checking the final checkout.

## 10. Six required demonstrations — PDF page 3, section 10

| Demo | What must be shown | Preparation / support |
|---|---|---|
| Object storage | Upload/download and verify that retrieved bytes match the original | B6 / A3, B5 |
| Chunking and distribution | Upload an object larger than 1 MB and show chunks distributed across workers | B6 / A3, B2, B5 |
| Replication | Show two copies of every chunk and their locations | B6 / A3, B2, B5 |
| Metadata | Upload, retrieve, and modify custom metadata without re-uploading data | B6 / A4, B5 |
| Worker failure | Stop a worker, retrieve using a surviving copy, restart it, and show replication behavior | B6 / A2, A3, A5, B3 |
| Data integrity | Intentionally corrupt stored data and demonstrate detection and handling | B6 / B4, A3, A5 |

Both teammates must independently run these; record actual results in [the tracker](COURSE-TRACKER.md).

## Our choices, separate from assignment requirements

| Chosen implementation | Why we choose it; assignment boundary |
|---|---|
| Python/FastAPI, HTTPX, SQLite, pytest | Familiar implementation stack; no particular stack is mandated |
| 1 MB = 1,000,000 bytes initially | Documented interpretation; assignment gives no exact byte count |
| SHA-256, UUIDs, rotating worker pairs | Concrete choices for required integrity, identity, and distribution |
| Persistent controller metadata, atomic worker writes, operation receipts, tombstones | Safeguards for correct restart/upload/delete/repair; their exact mechanisms are our design |
| Fixed-order periodic local scans | Selected course addition that detects cold corruption; periodic scans are not explicitly required |
| Serial controller-relayed repair | Simple implementation of required replication restoration; parallel/direct transfers are not required |
| Fully verified temporary-file download before success | Simple public error behavior; exact buffering/spooling strategy is not mandated |
| Pending deletion returns 503 until all workers acknowledge cleanup | Conservative handling of an unspecified offline-worker edge case |
| CLI/inspection tools and read-only status extension | Make required demonstrations explainable; a dashboard is not required |

No adaptive replica algorithm, duplicate-read mechanism, batch API, advanced repair scheduler, or maintenance-reuse engine is required for the course submission. See [PROJECT-SCOPE.md](../PROJECT-SCOPE.md) for chosen later work.
