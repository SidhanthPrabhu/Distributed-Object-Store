#!/usr/bin/env python3
"""Cache the eight public source PDFs. Does not build or run the object store."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def fetch(paper):
    destination = ROOT / "research" / "papers" / (paper["slug"] + ".pdf")
    expected = paper.get("reviewed_sha256")
    if destination.exists() and destination.read_bytes().startswith(b"%PDF-"):
        data = destination.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if expected and digest != expected:
            return {"id": paper["id"], "errors": [{"error": "Cached PDF differs from the reviewed SHA-256; inspect it before replacing the reviewed source."}]}
        return {"id": paper["id"], "path": str(destination.relative_to(ROOT)),
                "url": paper.get("reviewed_download_url"),
                "sha256": digest, "bytes": len(data), "cached": True}
    failures = []
    for url in paper["pdf_urls"]:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "AcademicProjectResearch/1.0"})
            with urllib.request.urlopen(request, timeout=25) as response:
                data = response.read()
            if not data.startswith(b"%PDF-"):
                raise ValueError("Response is not a PDF")
            if expected and hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("Downloaded PDF differs from the reviewed SHA-256; a new review/version is needed")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            return {"id": paper["id"], "path": str(destination.relative_to(ROOT)),
                    "url": url, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        except Exception as exc:
            failures.append({"url": url, "error": str(exc)})
    return {"id": paper["id"], "errors": failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper", action="append", help="Fetch only this paper ID, e.g. P1")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "research" / "sources.json").read_text())
    papers = [p for p in manifest["papers"] if not args.paper or p["id"] in args.paper]
    if not papers:
        parser.error("No matching paper IDs")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, papers))
    lockfile = ROOT / "research" / "downloads.json"
    previous = json.loads(lockfile.read_text()) if lockfile.exists() else {}
    # Failed refreshes must not erase successful review provenance.
    previous.update({result["id"]: result for result in results if "errors" not in result})
    lockfile.write_text(json.dumps(previous, indent=2) + "\n")
    for result in results:
        print(json.dumps(result))
    raise SystemExit(1 if any("errors" in result for result in results) else 0)


if __name__ == "__main__":
    main()
