"""Opt-in C2 acceptance against a real, ready deployment."""

import json
import os
import random
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest

from client.cli import main
from common.models import ErrorEnvelope


def test_c2_object_lifecycle(tmp_path, capsys):
    url = os.environ.get("OBJECT_STORE_C2_TEST_URL")
    if not url:
        pytest.skip("Set OBJECT_STORE_C2_TEST_URL to test a disposable deployment with A4")

    def cli(*args):
        code = main(["--url", url, *args])
        output = capsys.readouterr()
        assert code == 0, output.err
        return json.loads(output.out)

    # Fail before creating a test object if the required A4 list route is absent.
    cli("health")
    cli("list")
    data = random.Random(838).randbytes(1_000_123)
    digest = sha256(data).hexdigest()
    source = tmp_path / f"c2-{uuid4()}.bin"
    source.write_bytes(data)
    uploaded = cli("upload", str(source), "--metadata", '{"course":"CS838","keep":"original"}')
    object_id = uploaded["object_id"]
    object_path = f"/api/v1/objects/{object_id}"

    with httpx.Client(base_url=url, timeout=30, trust_env=False) as http:
        deleted = False
        try:
            before = cli("metadata", object_id)
            assert before == uploaded and before["checksum"]["value"] == digest
            assert [chunk["size_bytes"] for chunk in before["chunks"]] == [1_000_000, 123]
            assert all(len(set(chunk["locations"])) == 2 for chunk in before["chunks"])
            assert cli("inspect", object_id)["chunks"] == before["chunks"]
            assert any(obj["object_id"] == object_id for obj in cli("list")["objects"])

            after = cli("patch", object_id, '{"course":"Cloud Computing","reviewed":true}')
            assert after["custom_metadata"] == {
                "course": "Cloud Computing", "keep": "original", "reviewed": True,
            }
            assert after["metadata_revision"] == before["metadata_revision"] + 1
            for field in ("object_id", "generation", "created_at", "size_bytes", "checksum", "chunks"):
                assert after[field] == before[field]
            assert cli("metadata", object_id) == after

            output = tmp_path / "copy.bin"
            assert cli("download", object_id, str(output), "--sha256", digest)["sha256"] == digest
            assert output.read_bytes() == data

            invalid = http.patch(f"{object_path}/metadata", json=[])
            assert invalid.status_code == 400
            assert ErrorEnvelope.model_validate_json(invalid.content).error.code
            assert cli("delete", object_id)["deleted"]
            deleted = True
            for route in (object_path, f"{object_path}/metadata"):
                missing = http.get(route)
                assert missing.status_code == 404
                assert ErrorEnvelope.model_validate_json(missing.content).error.code
            assert all(obj["object_id"] != object_id for obj in cli("list")["objects"])
        finally:
            if not deleted:
                http.delete(object_path)
