#!/usr/bin/env python3
"""Check research-handoff links, source provenance, coverage IDs and Python syntax.

These are documentation/source checks, not tests of the unimplemented object store.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-pdfs", action="store_true", help="Also require and hash-check the downloaded PDFs")
    args = parser.parse_args()
    errors = []
    documents = []
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        content = path.read_text()
        if sum(line.startswith("```") for line in content.splitlines()) % 2:
            errors.append(f"Unclosed code fence: {path.name}")
        for target in re.findall(r"\]\(([^)]+)\)", content):
            if target.startswith(("https://", "http://", "#", "mailto:")):
                continue
            if not (path.parent / target.split("#")[0]).resolve().exists():
                errors.append(f"Broken local link in {path.name}: {target}")
        documents.append({"file": str(path.relative_to(ROOT)), "words": len(content.split()), "lines": len(content.splitlines())})

    sources = json.loads((ROOT / "research/sources.json").read_text())
    downloads = json.loads((ROOT / "research/downloads.json").read_text())
    coverage = json.loads((ROOT / "research/coverage-index.json").read_text())
    if {p["id"] for p in sources["papers"]} != {f"P{i}" for i in range(1, 9)}:
        errors.append("Expected primary sources P1-P8")
    verified = 0
    for paper in sources["papers"]:
        ident = paper["id"]
        if paper["reviewed_sha256"] != downloads[ident]["sha256"]:
            errors.append(f"Provenance mismatch: {ident}")
        if paper["reviewed_pdf_pages"] != coverage[ident]["pdf_pages"] or not coverage[ident].get("reviewed_sections"):
            errors.append(f"Coverage mismatch: {ident}")
        if args.with_pdfs:
            pdf = ROOT / "research/papers" / (paper["slug"] + ".pdf")
            if not pdf.exists():
                errors.append(f"Missing source PDF: {ident}; run fetch_research.py")
            else:
                data = pdf.read_bytes()
                if not data.startswith(b"%PDF-") or hashlib.sha256(data).hexdigest() != paper["reviewed_sha256"]:
                    errors.append(f"Source PDF mismatch: {ident}")
                else:
                    verified += 1

    for path in (ROOT / "scripts").glob("*.py"):
        ast.parse(path.read_text(), filename=str(path))
    readme = (ROOT / "README.md").read_text()
    features = (ROOT / "docs/feature-spec.md").read_text()
    validations = (ROOT / "docs/validation-plan.md").read_text()
    reviews = (ROOT / "docs/research-review.md").read_text()
    families = [
        (readme, r"\| R{} \|", range(1, 14), "requirement"),
        (features, r"^## F{}\.", range(1, 13), "feature"),
        (reviews, r"^## P{}\.", range(1, 9), "paper review"),
        (validations, r"\| T{} \|", range(1, 19), "correctness scenario"),
        (validations, r"^### E{}\.", range(9), "experiment"),
    ]
    for content, pattern, numbers, family in families:
        for number in numbers:
            if not re.search(pattern.format(number), content, re.MULTILINE):
                errors.append(f"Missing {family} {number}")
    if not (ROOT / "references/assignment.pdf").read_bytes().startswith(b"%PDF-"):
        errors.append("Assignment copy is not a PDF")

    report = {
        "review_date": sources["review_date"],
        "scope": "Research handoff only; application not implemented or tested",
        "source_pdfs_verified": verified,
        "requirements": 13, "features": 12, "correctness_scenarios": 18, "experiments": 9,
        "documents": documents, "errors": errors,
    }
    (ROOT / "research/handoff-validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    raise SystemExit(bool(errors))


if __name__ == "__main__":
    main()
