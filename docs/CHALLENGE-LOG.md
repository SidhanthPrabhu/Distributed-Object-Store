# C3/C4 challenge log

18 November 2026 · Docker Compose walkthrough and controller restart checks

## 1. A restarted controller could briefly report three copies

- **Symptom.** The walkthrough sometimes counted three replicas for a chunk after restarting the controller.
- **Cause.** The worker health checks passed while the controller was still verifying copies and retiring an extra replica. Repair can temporarily leave three recorded locations when it adds a replacement before removing the old copy.
- **Fix.** The walkthrough now waits for the object's repair jobs and replication deficits to clear. It then checks the download and confirms exactly two physical replicas with the expected digests. [Course walkthrough](../scripts/demo/course_walkthrough.py)

## 2. The first download after both workers restarted could still return 503

- **Symptom.** The deliberate two-copy outage returned the expected HTTP 503 `no_valid_copy`. An earlier run also got 503 on the first download after restarting both holders.
- **Cause.** Docker could report the containers started while the controller still had health information from the previous worker processes. The test could attempt the download before the new processes registered and the controller reconciled their stored chunks.
- **Fix.** The walkthrough records the old worker boot IDs. After restart, it waits for both workers to report healthy with new boot IDs and for any repair of the affected chunk to finish. It then checks the original bytes and both physical replicas. [Course walkthrough](../scripts/demo/course_walkthrough.py)

## 3. The A4 restart probe treated a valid replica move as a metadata failure

- **Symptom.** All six demos passed, but the separate A4 probe failed when it compared the saved metadata with the response after restart. The metadata endpoint returned HTTP 200.
- **Cause.** The retained journal and worker logs showed that the controller wrote the chunk to workers 2 and 3 before restart. Recovery added a copy on worker 1 and retired the older copy on worker 2. The probe required the entire metadata response to match, including the current locations.
- **Fix.** The probe waits for the object's repair to finish and compares every metadata field except the worker locations. It verifies that exactly two distinct advertised workers have valid catalog entries and physical files of the expected length and SHA-256. It also downloads the file and compares the bytes with the original upload. [A4 service probe](../tests/controller/check_a4_services.py)
- **Verification.** Holding worker 2 offline forced the same move from workers 2 and 3 to workers 1 and 3. The other metadata fields matched, including the custom keys and revision. Both replica files and the public download matched. [Live verification results](testing/course-services.json)

## 4. A worker's port could refuse connections during startup

- **Symptom.** The first controlled late-rejoin run aborted when the readiness probe connected to the restarted worker before its HTTP port was listening.
- **Cause.** The connection error happened inside a subprocess. It exited with an error, so the outer readiness loop received a subprocess failure instead of a connection error it could retry.
- **Fix.** The subprocess now passes the connection error back to the existing readiness loop, which retries within its timeout. The final controlled late-rejoin run completed without manual intervention. [A4 service probe](../tests/controller/check_a4_services.py)

## Final verification

The local suite passed 194 tests and skipped one opt-in integration test, which passed separately against Docker. All six course demos passed in an isolated five-service Compose stack. The A4 probe passed two ordinary restarts and one controlled late rejoin that forced replica movement. [Recorded results](testing/course-services.json)
