"""Exercise the CLI against recorded public API shapes without a live controller."""

import json
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest

from client.cli import main

DATA = b"\x00binary data\xff"
DIGEST = sha256(DATA).hexdigest()


@pytest.fixture
def manifest():
    now = "2026-11-10T10:00:00Z"
    return {
        "object_id": str(uuid4()), "generation": str(uuid4()), "name": "sample.bin",
        "size_bytes": len(DATA), "content_type": "application/octet-stream",
        "created_at": now, "modified_at": now,
        "checksum": {"algorithm": "sha256", "value": DIGEST},
        "chunk_size_bytes": 1_000_000, "chunk_count": 1,
        "metadata_revision": 1, "custom_metadata": {"course": "CS838"},
        "chunks": [{
            "chunk_id": str(uuid4()), "ordinal": 0, "size_bytes": len(DATA), "sha256": DIGEST,
            "locations": ["worker1", "worker2"],
        }],
    }


def test_upload_sends_binary_multipart_and_metadata(tmp_path, capsys, manifest):
    source = tmp_path / manifest["name"]
    source.write_bytes(DATA)

    def exchange(request):
        assert request.method == "POST" and request.url.path == "/api/v1/objects"
        assert request.headers["content-type"].startswith("multipart/form-data;")
        assert b'name="file"; filename="sample.bin"' in request.content
        assert DATA in request.content
        assert b'name="metadata"' in request.content and b'{"course": "CS838"}' in request.content
        return httpx.Response(201, json=manifest)

    assert main(
        ["upload", str(source), "--metadata", '{"course":"CS838"}'],
        transport=httpx.MockTransport(exchange),
    ) == 0
    assert json.loads(capsys.readouterr().out) == manifest


def test_download_checks_hash_and_preserves_existing_file(tmp_path, capsys, manifest):
    output = tmp_path / "copy.bin"

    def exchange(request):
        assert request.method == "GET" and request.url.path == f"/api/v1/objects/{manifest['object_id']}"
        return httpx.Response(200, content=DATA)

    transport = httpx.MockTransport(exchange)
    args = ["download", manifest["object_id"], str(output), "--sha256", DIGEST.upper()]
    assert main(args, transport=transport) == 0
    assert output.read_bytes() == DATA
    assert json.loads(capsys.readouterr().out) == {
        "path": str(output), "size_bytes": len(DATA), "sha256": DIGEST,
    }
    output.write_bytes(b"keep me")
    assert main(args, transport=transport) == 1
    assert "already exists" in capsys.readouterr().err
    assert output.read_bytes() == b"keep me"
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize(("size", "digest", "message"), [
    (len(DATA) + 1, DIGEST, "Content-Length"),
    (len(DATA) - 1, DIGEST, "Content-Length"),
    (len(DATA), "0" * 64, "SHA-256"),
])
def test_invalid_download_leaves_no_output(tmp_path, capsys, manifest, size, digest, message):
    output = tmp_path / "copy.bin"
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"Content-Length": str(size)}, content=DATA),
    )
    assert main(["download", manifest["object_id"], str(output), "--sha256", digest],
                transport=transport) == 1
    assert message in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_list_metadata_patch_and_inspection_use_public_routes(capsys, manifest):
    object_id = manifest["object_id"]
    object_path = f"/api/v1/objects/{object_id}"
    listing = {"objects": [{key: manifest[key] for key in ("object_id", "name", "size_bytes", "content_type")}]}
    updated = {**manifest, "custom_metadata": {"course": "CS838", "reviewed": True}, "metadata_revision": 2}
    health = {"status": "ready", "controller_epoch": 1}
    status = {"workers": [], "replication_deficits": [], "repairs": []}

    def exchange(request):
        match request.method, request.url.path:
            case "GET", "/api/v1/objects":
                return httpx.Response(200, json=listing)
            case "GET", "/health/ready":
                return httpx.Response(200, json=health)
            case "GET", "/api/v1/admin/status":
                return httpx.Response(200, json=status)
            case "GET", path if path == f"{object_path}/metadata":
                return httpx.Response(200, json=manifest)
            case "PATCH", path if path == f"{object_path}/metadata":
                assert json.loads(request.content) == {"reviewed": True}
                return httpx.Response(200, json=updated)
            case "DELETE", path if path == object_path:
                return httpx.Response(204)
        raise AssertionError(f"Unexpected request {request.method} {request.url.path}")

    commands = [
        (["list"], listing),
        (["metadata", object_id], manifest),
        (["patch", object_id, '{"reviewed":true}'], updated),
        (["inspect", object_id],
         {key: manifest[key] for key in ("object_id", "name", "checksum", "chunk_count", "chunks")}),
        (["health"], health),
        (["status"], status),
        (["delete", object_id], {"object_id": object_id, "deleted": True}),
    ]
    for args, expected in commands:
        assert main(args, transport=httpx.MockTransport(exchange)) == 0
        assert json.loads(capsys.readouterr().out) == expected


def test_pending_delete_reports_error_and_request_id(capsys, manifest):
    request_id = str(uuid4())
    transport = httpx.MockTransport(lambda _: httpx.Response(503, json={"error": {
        "code": "deletion_pending", "message": "Waiting for all workers", "request_id": request_id,
    }}))
    assert main(["delete", manifest["object_id"]], transport=transport) == 1
    result = capsys.readouterr()
    assert result.out == ""
    assert "503 deletion_pending" in result.err and request_id in result.err


@pytest.mark.parametrize("metadata", ["[]", '{"bad":NaN}', '{"bad":1e999}'])
def test_non_object_or_non_finite_metadata_is_rejected_before_requests(metadata, manifest):
    with pytest.raises(SystemExit) as error:
        main(["patch", manifest["object_id"], metadata])
    assert error.value.code == 2
