"""Exercise the controller's public object and inspection APIs."""

import argparse
import json
import math
import os
import re
import sys
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import ValidationError

from common.models import ErrorEnvelope, ObjectList, ObjectMetadata

OBJECTS = "/api/v1/objects"
BLOCK_SIZE = 64 * 1024


class ClientError(Exception):
    """A request or response could not be used by the CLI."""


def json_object(value: str) -> dict:
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError
        json.dumps(parsed, allow_nan=False)
    except (ValueError, RecursionError) as exc:
        raise argparse.ArgumentTypeError("metadata must be a JSON object with finite values") from exc
    return parsed


def controller_origin(value: str) -> str:
    try:
        url = urlsplit(value)
        if (
            url.scheme in {"http", "https"} and url.hostname
            and url.username is None and url.password is None
            and url.path in {"", "/"} and not url.query and not url.fragment
            and url.port != 0
        ):
            return value
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("controller URL must be an HTTP(S) origin without credentials")


def positive_seconds(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a positive number of seconds") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive number of seconds")
    return timeout


def sha256_hex(value: str) -> str:
    if re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise argparse.ArgumentTypeError("expected a 64-digit SHA-256 hex digest")
    return value.lower()


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="python -m client", description=__doc__)
    cli.add_argument("--url", type=controller_origin,
                     default=os.environ.get("OBJECT_STORE_URL", "http://127.0.0.1:8000"),
                     help="controller origin (default: OBJECT_STORE_URL or http://127.0.0.1:8000)")
    cli.add_argument("--timeout", type=positive_seconds, default=30.0,
                     help="HTTP timeout in seconds (default: 30)")
    commands = cli.add_subparsers(dest="command", required=True)
    upload = commands.add_parser("upload", help="upload a file")
    upload.add_argument("file", type=Path)
    upload.add_argument("--metadata", type=json_object, help="custom metadata as a JSON object")

    download = commands.add_parser("download", help="download an object to a new file")
    download.add_argument("object_id", type=UUID)
    download.add_argument("output", type=Path)
    download.add_argument("--sha256", type=sha256_hex, help="verify against an independently computed SHA-256")

    patch = commands.add_parser("patch", help="merge custom metadata")
    patch.add_argument("object_id", type=UUID)
    patch.add_argument("metadata", type=json_object, help="custom keys as a JSON object")

    for command, help_text in (
        ("metadata", "show all object metadata"),
        ("inspect", "show recorded chunk placements"),
        ("delete", "delete an object"),
    ):
        commands.add_parser(command, help=help_text).add_argument("object_id", type=UUID)

    commands.add_parser("list", help="list objects")
    commands.add_parser("health", help="check controller readiness")
    commands.add_parser("status", help="show worker health, missing replicas, and repair state")
    return cli


def expect(response: httpx.Response, expected: int) -> httpx.Response:
    if response.status_code == expected:
        return response
    response.read()
    try:
        error = ErrorEnvelope.model_validate_json(response.content).error
    except ValidationError:
        raise ClientError(f"HTTP {response.status_code} {response.reason_phrase}; expected {expected}") from None
    raise ClientError(f"HTTP {response.status_code} {error.code}: {error.message} (request {error.request_id})")


def upload_object(client: httpx.Client, path: Path, metadata: dict | None) -> ObjectMetadata:
    fields = {} if metadata is None else {"metadata": json.dumps(metadata)}
    with path.open("rb") as source:
        response = expect(client.post(OBJECTS, files={"file": (path.name, source)}, data=fields), 201)
    return ObjectMetadata.model_validate_json(response.content)


def download_object(client: httpx.Client, path: str, output: Path, expected_hash: str | None) -> dict:
    if output.exists():
        raise ClientError(f"Output already exists: {output}")
    size = 0
    digest = sha256()
    with client.stream("GET", path, headers={"Accept-Encoding": "identity"}) as response:
        expect(response, 200)
        length = response.headers.get("Content-Length", "")
        if re.fullmatch(r"[0-9]{1,19}", length) is None:
            raise ClientError("Download response has an invalid Content-Length")
        expected_size = int(length)
        with NamedTemporaryFile(dir=output.parent, prefix=".object-download-", delete_on_close=False) as temporary:
            for block in response.iter_bytes(chunk_size=BLOCK_SIZE):
                size += len(block)
                if size > expected_size:
                    raise ClientError("Download size does not match Content-Length")
                temporary.write(block)
                digest.update(block)
            if size != expected_size:
                raise ClientError("Download size does not match Content-Length")
            actual_hash = digest.hexdigest()
            if expected_hash is not None and actual_hash != expected_hash:
                raise ClientError("Download SHA-256 does not match the expected digest")
            temporary.flush()
            # Publish the verified file atomically and refuse an existing destination.
            os.link(temporary.name, output)
    return {"path": str(output), "size_bytes": size, "sha256": actual_hash}


def json_response(response: httpx.Response) -> dict:
    body = expect(response, 200).json()
    if not isinstance(body, dict):
        raise ClientError("Expected a JSON object from the controller")
    return body


def execute(client: httpx.Client, args: argparse.Namespace) -> dict:
    object_path = f"{OBJECTS}/{args.object_id}" if hasattr(args, "object_id") else OBJECTS
    match args.command:
        case "upload":
            return upload_object(client, args.file, args.metadata).model_dump(mode="json")
        case "download":
            return download_object(client, object_path, args.output, args.sha256)
        case "list":
            response = expect(client.get(OBJECTS), 200)
            return ObjectList.model_validate_json(response.content).model_dump(mode="json")
        case "patch":
            response = expect(client.patch(f"{object_path}/metadata", json=args.metadata), 200)
            return ObjectMetadata.model_validate_json(response.content).model_dump(mode="json")
        case "metadata" | "inspect":
            response = expect(client.get(f"{object_path}/metadata"), 200)
            metadata = ObjectMetadata.model_validate_json(response.content).model_dump(mode="json")
            if args.command == "inspect":
                return {key: metadata[key] for key in ("object_id", "name", "checksum", "chunk_count", "chunks")}
            return metadata
        case "delete":
            expect(client.delete(object_path), 204)
            return {"object_id": str(args.object_id), "deleted": True}
        case "health":
            return json_response(client.get("/health/ready"))
        case "status":
            return json_response(client.get("/api/v1/admin/status"))
    raise AssertionError("Unknown client command")


def main(argv: list[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        with httpx.Client(base_url=args.url, timeout=args.timeout, trust_env=False, transport=transport) as client:
            output = json.dumps(execute(client, args), indent=2, allow_nan=False)
    except ValidationError:
        print("Error: controller response does not match the API contract", file=sys.stderr)
        return 1
    except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError, ClientError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0
