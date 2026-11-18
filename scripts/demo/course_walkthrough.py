"""Run the six course demonstrations against a disposable local Compose project."""

import argparse
import json
import random
import subprocess
import time
from hashlib import sha256
from uuid import uuid4

import httpx

WORKERS = ("worker1", "worker2", "worker3", "worker4")
OBJECTS = "/api/v1/objects"
STATUS = "/api/v1/admin/status"
SNAPSHOT = """
import hashlib, json, sqlite3, sys
from pathlib import Path
db = sqlite3.connect('file:/data/catalog.sqlite3?mode=ro', uri=True)
rows = db.execute('SELECT chunk_id, operation_sequence, state FROM placements WHERE object_id=?',
                  (sys.argv[1],))
result = {}
for cid, seq, state in rows:
    path = Path('/data/chunks') / (cid + '.bin')
    if path.is_file():
        result[cid] = {'sequence': seq, 'state': state,
                       'digest': hashlib.sha256(path.read_bytes()).hexdigest()}
print(json.dumps(result))
"""
REPORT_COUNT = """
import sqlite3, sys
db = sqlite3.connect('file:/data/metadata.sqlite3?mode=ro', uri=True)
print(db.execute('SELECT count(*) FROM integrity_reports WHERE chunk_id=? AND worker_id=?',
                 sys.argv[1:]).fetchone()[0])
"""


def compose(*args: str) -> str:
    return subprocess.check_output(["docker", "compose", *args], text=True, timeout=90).strip()


def until(check, label, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except httpx.TransportError:
            pass
        time.sleep(0.25)
    raise AssertionError(f"Timed out waiting for {label}")


def snapshot(object_id, workers=WORKERS):
    return {worker: json.loads(compose("exec", "-T", worker, "python", "-c", SNAPSHOT, object_id))
            for worker in workers}


def assert_copies(obj, copies):
    for chunk in obj["chunks"]:
        cid = chunk["chunk_id"]
        actual = {worker for worker, records in copies.items() if cid in records}
        assigned = set(chunk["locations"]) & copies.keys()
        assert actual == assigned and len(actual) == 2, (cid, actual, assigned)
        assert all(copies[w][cid]["digest"] == chunk["sha256"] for w in actual), cid


def checked(response, status):
    assert response.status_code == status, (response.status_code, response.text[:500])
    return response


def upload(http, data):
    return checked(http.post(OBJECTS, files={"file": (f"course-demo-{uuid4()}.bin", data)},
                             data={"metadata": '{"course":"CS838","reviewed":false}'}), 201).json()


def download(http, path, data):
    returned = checked(http.get(path), 200).content
    assert returned == data
    return sha256(returned).hexdigest()


def replicas_settled(http, chunk_ids=(), *, worker_ids=WORKERS, previous_boots=None):
    response = http.get(STATUS)
    if response.status_code != 200:
        return False
    status = response.json()
    healthy_boots = {w["worker_id"]: w["boot_id"] for w in status["workers"] if w["state"] == "HEALTHY"}
    if any(not healthy_boots.get(worker) for worker in worker_ids):
        return False
    # Docker can start a container before the old boot's heartbeat expires.
    if any(healthy_boots.get(worker) in (None, boot) for worker, boot in (previous_boots or {}).items()):
        return False
    return not any(entry["chunk_id"] in chunk_ids
                   for field in ("replication_deficits", "repairs") for entry in status[field])


def run(http):
    until(lambda: replicas_settled(http), "four reconciled workers")
    before = checked(http.get(OBJECTS), 200).json()
    created = []
    stopped = set()
    try:
        original = random.Random(838).randbytes(2_500_000)
        obj = upload(http, original)
        object_id = obj["object_id"]
        created.append(object_id)
        path = f"{OBJECTS}/{object_id}"
        metadata = path + "/metadata"
        digest = download(http, path, original)
        assert digest == sha256(original).hexdigest() == obj["checksum"]["value"]
        print("1. Upload/download: original SHA-256 matched", flush=True)

        assert [c["size_bytes"] for c in obj["chunks"]] == [1_000_000, 1_000_000, 500_000]
        assert {w for c in obj["chunks"] for w in c["locations"]} == set(WORKERS)
        print("2. Chunking: 2,500,000 bytes split over all four workers", flush=True)
        copies = snapshot(object_id)
        assert_copies(obj, copies)
        print("3. Replication: six actual files, each copy matched its chunk SHA-256", flush=True)

        assert checked(http.get(metadata), 200).json() == obj
        updated = checked(http.patch(metadata, json={"reviewed": True, "demo": 4}), 200).json()
        assert updated["custom_metadata"] == {"course": "CS838", "reviewed": True, "demo": 4}
        assert updated["metadata_revision"] == obj["metadata_revision"] + 1
        for key in obj.keys() - {"custom_metadata", "metadata_revision", "modified_at"}:
            assert updated[key] == obj[key], key
        assert snapshot(object_id) == copies
        print("4. Metadata: merged keys with zero chunk writes", flush=True)

        id_set = {chunk["chunk_id"] for chunk in obj["chunks"]}
        stopped.add("worker2")
        compose("stop", "worker2")
        assert download(http, path, original) == digest

        def repaired_with_worker2_down():
            status = checked(http.get(STATUS), 200).json()
            workers = {w["worker_id"]: w["state"] for w in status["workers"]}
            return workers["worker2"] == "UNAVAILABLE" and not any(
                d["chunk_id"] in id_set for d in status["replication_deficits"])

        until(repaired_with_worker2_down, "two live copies with worker2 stopped")
        online = tuple(w for w in WORKERS if w != "worker2")
        assert_copies(checked(http.get(metadata), 200).json(), snapshot(object_id, online))
        compose("start", "worker2")
        stopped.remove("worker2")

        until(lambda: replicas_settled(http, id_set), "worker2 rejoin and excess retirement")
        settled_obj = checked(http.get(metadata), 200).json()
        assert_copies(settled_obj, snapshot(object_id))
        print("5. Outage: read survived, two live copies restored, rejoin retired extras", flush=True)

        chunk = settled_obj["chunks"][0]
        cid, target = chunk["chunk_id"], chunk["locations"][0]
        baseline = snapshot(object_id, (target,))[target][cid]
        damaged = json.loads(compose("exec", "-T", "-e", "OBJECT_STORE_ALLOW_FAULT_INJECTION=1",
                                      target, "python", "-m", "worker.corrupt", object_id, cid))
        assert damaged["corrupted_sha256"] != chunk["sha256"]

        def cold_repair():
            row = snapshot(object_id, (target,))[target].get(cid)
            return row and row["sequence"] > baseline["sequence"] and row["digest"] == chunk["sha256"]

        until(cold_repair, "cold corruption detected and repaired before download")
        assert int(compose("exec", "-T", "controller", "python", "-c", REPORT_COUNT, cid, target)) >= 1
        assert download(http, path, original) == digest
        assert_copies(checked(http.get(metadata), 200).json(), snapshot(object_id))
        print("6. Integrity: real byte corruption reported and repaired before the next read", flush=True)

        compose("restart", "controller")
        until(lambda: replicas_settled(http, id_set), "controller restart, worker rejoin, and excess retirement")
        assert download(http, path, original) == digest
        assert_copies(checked(http.get(metadata), 200).json(), snapshot(object_id))
        assert checked(http.get(metadata), 200).json()["custom_metadata"] == updated["custom_metadata"]

        negative = upload(http, b"two copies unavailable")
        negative_id = negative["object_id"]
        created.append(negative_id)
        negative_chunk = negative["chunks"][0]
        unavailable = negative_chunk["locations"]
        previous_boots = {w["worker_id"]: w["boot_id"] for w in checked(http.get(STATUS), 200).json()["workers"]
                          if w["worker_id"] in unavailable}
        # Freeze repair while both holders stop so the outage has no replacement.
        stopped.add("controller")
        compose("stop", "controller")
        stopped.update(unavailable)
        compose("stop", *unavailable)
        compose("start", "controller")
        stopped.remove("controller")
        survivors = tuple(worker for worker in WORKERS if worker not in unavailable)
        until(lambda: replicas_settled(http, worker_ids=survivors), "surviving workers ready with both copies offline")
        negative_path = f"{OBJECTS}/{negative_id}"
        failed = checked(http.get(negative_path), 503)
        assert failed.json()["error"]["code"] == "no_valid_copy"
        compose("start", *unavailable)
        stopped.difference_update(unavailable)
        until(lambda: replicas_settled(http, {negative_chunk["chunk_id"]}, previous_boots=previous_boots),
              "both new worker boots reconciled and the unavailable object repaired")
        download(http, negative_path, b"two copies unavailable")
        assert_copies(checked(http.get(negative_path + "/metadata"), 200).json(), snapshot(negative_id))
        print("Extra checks: restart preserved data; both copies offline produced 503", flush=True)
    finally:
        if stopped:
            compose("start", *sorted(stopped))
        if created:
            until(lambda: replicas_settled(http), "workers ready for demo cleanup")
            for oid in created:
                until(lambda: http.delete(f"{OBJECTS}/{oid}").status_code in (204, 404),
                      f"cleanup of {oid}")
    assert checked(http.get(OBJECTS), 200).json() == before
    print(json.dumps({"demos_passed": 6, "sha256": digest, "size_bytes": len(original),
                      "preexisting_objects_preserved": len(before["objects"])}))


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--disposable", action="store_true",
                     help="confirm this Compose project may be stopped and its demo bytes corrupted")
    args = cli.parse_args()
    if not args.disposable:
        cli.error("Run only on a disposable local Compose deployment with --disposable")
    port = compose("port", "controller", "8000")
    with httpx.Client(base_url=f"http://{port}", timeout=30, trust_env=False) as http:
        run(http)


if __name__ == "__main__":
    main()
