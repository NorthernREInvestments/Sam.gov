"""Process one OpenGov package directory → quality-gated product identities."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from product_identity.classifier import classify_identity
from product_identity.document_join import join_specs_into_lines
from product_identity.line_quality import classify_extraction_row, line_quality_gate
from product_identity.models import (
    LINE_ITEM_CONFIRMED,
    LINE_ITEM_LIKELY,
    REAL_PURCHASING_LINE,
    USABLE_GRADES,
)
from product_identity.normalizer import ProductIdentityNormalizer
from product_identity.pdf_extract import extract_pdf_rows
from product_identity.spreadsheet import extract_spreadsheet

_PRICING_NAME = re.compile(
    r"(price|pricing|bid\s*sheet|cost\s*proposal|schedule|quote|usage|inventory|catalog|parts)",
    re.I,
)
_SPEC_NAME = re.compile(r"(spec|scope|addend|technical|requirement|salient)", re.I)
_SKIP_NAME = re.compile(r"(terms\s*and\s*conditions|w-?9|insurance|signature|instructions?\b)", re.I)


def discover_package_dirs(root: Path) -> list[tuple[str, str, Path]]:
    """Yield (government_code, project_id, path) for downloaded packages."""
    out: list[tuple[str, str, Path]] = []
    if not root.exists():
        return out
    for gov in sorted(root.iterdir()):
        if not gov.is_dir():
            continue
        for proj in sorted(gov.iterdir()):
            if not proj.is_dir():
                continue
            if any(proj.iterdir()):
                out.append((gov.name, proj.name, proj))
    return out


def _files(pkg: Path) -> list[Path]:
    return [p for p in pkg.iterdir() if p.is_file()]


_BRAND_FROM_NAME = re.compile(
    r"\b(Cummins|Ford|Caterpillar|John\s*Deere|3M|Honeywell|Grainger|Milwaukee|"
    r"DeWalt|Makita|Bosch|Siemens|Schneider|Eaton|GE|Motorola|Cisco|Dell|HP|"
    r"Toyota|Ford\s+Meter\s+Box|Mueller|Clow|American\s+Cast\s+Iron)\b",
    re.I,
)


def _guess_package_manufacturer(files: list[Path]) -> str | None:
    # Underscores are word chars — normalize to spaces for brand matching
    blob = " ".join(re.sub(r"[_\-]+", " ", f.stem) for f in files)
    m = _BRAND_FROM_NAME.search(blob)
    return m.group(1).strip() if m else None


def process_package(
    government_code: str,
    project_id: str,
    pkg_dir: Path,
    normalizer: ProductIdentityNormalizer | None = None,
) -> dict[str, Any]:
    normalizer = normalizer or ProductIdentityNormalizer()
    opportunity_id = f"opengov:{government_code}:{project_id}"
    files = _files(pkg_dir)
    package_mfr = _guess_package_manufacturer(files)

    raw_rows: list[dict[str, Any]] = []
    spec_rows: list[dict[str, Any]] = []
    src_counts: dict[str, int] = {"xlsx": 0, "xls": 0, "csv": 0, "pdf": 0}

    for f in files:
        name = f.name
        if _SKIP_NAME.search(name) and not _PRICING_NAME.search(name):
            continue
        ext = f.suffix.lower()
        if ext in {".xlsx", ".xls", ".csv"}:
            rows = extract_spreadsheet(f)
            for r in rows:
                r["_source_kind"] = ext.lstrip(".")
            raw_rows.extend(rows)
            src_counts[ext.lstrip(".")] = src_counts.get(ext.lstrip("."), 0) + len(rows)
        elif ext == ".pdf":
            pricing, specs = extract_pdf_rows(f)
            for r in pricing:
                r["_source_kind"] = "pdf"
            raw_rows.extend(pricing)
            spec_rows.extend(specs)
            src_counts["pdf"] += len(pricing)
            # Spec-named PDFs also feed join corpus from full pricing extract
            if _SPEC_NAME.search(name):
                for r in pricing:
                    spec_rows.append({**r, "_kind": "spec_context"})

    # Inherit package-level manufacturer when row lacks one
    if package_mfr:
        for r in raw_rows:
            if not r.get("manufacturer"):
                r["manufacturer"] = package_mfr
                r["inherited_manufacturer"] = True

    # Document-set join before quality/identity
    joined = join_specs_into_lines(raw_rows, spec_rows)

    seen: set[str] = set()
    extraction_counts: dict[str, int] = {}
    quality_counts: dict[str, int] = {}
    identities: list[dict[str, Any]] = []
    usable: list[dict[str, Any]] = []

    for i, row in enumerate(joined, start=1):
        ex_class = classify_extraction_row(row, seen_keys=seen)
        extraction_counts[ex_class] = extraction_counts.get(ex_class, 0) + 1
        quality, qscore, qreasons = line_quality_gate(row, ex_class)
        quality_counts[quality] = quality_counts.get(quality, 0) + 1
        if quality not in {LINE_ITEM_CONFIRMED, LINE_ITEM_LIKELY}:
            continue
        if ex_class != REAL_PURCHASING_LINE:
            continue
        # Prefer catalog as part_number when part_number empty (common in inventory sheets)
        if not row.get("part_number") and row.get("catalog_number"):
            row = {**row, "part_number": row.get("catalog_number")}
        norm = normalizer.normalize_row(row)
        ident = classify_identity(
            norm,
            extra={
                "equal_allowed": bool(row.get("equal_allowed")),
                "brand_or_equal_context": bool(row.get("brand_or_equal_context")),
                "enriched_from_spec": bool(row.get("enriched_from_spec")),
            },
        )
        ident["line_id"] = f"PI-{i:04d}-{uuid4().hex[:6]}"
        ident["opportunity_id"] = opportunity_id
        ident["line_quality"] = quality
        ident["extraction_class"] = ex_class
        ident["source_document"] = row.get("_source_path")
        ident["source_kind"] = row.get("_source_kind")
        ident["provenance"] = [
            {
                "government_code": government_code,
                "project_id": project_id,
                "sheet": row.get("_sheet"),
                "row": row.get("_row"),
                "quality_score": qscore,
                "quality_reasons": qreasons,
            }
        ]
        identities.append(ident)
        if ident.get("confidence_grade") in USABLE_GRADES:
            usable.append(ident)

    return {
        "opportunity_id": opportunity_id,
        "government_code": government_code,
        "project_id": project_id,
        "files": len(files),
        "raw_rows": len(joined),
        "extraction_counts": extraction_counts,
        "quality_counts": quality_counts,
        "identities": identities,
        "usable": usable,
        "source_counts": src_counts,
        "spec_rows": len(spec_rows),
        "enriched_from_spec": sum(1 for x in identities if x.get("enriched_from_spec")),
        "inherited_manufacturer": sum(1 for x in identities if x.get("inherited_manufacturer")),
        "brand_or_equal": sum(1 for x in identities if x.get("brand_or_equal_context")),
    }
