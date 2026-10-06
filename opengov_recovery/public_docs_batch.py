"""Staged OpenGov public document recovery + quality gate + line-item handoff."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from typing import Any, Callable
from uuid import uuid4

from application_clock import now_utc
from document_quality import (
    AMBIGUOUS_DOCUMENTS,
    PARTIAL_SOLICITATION_PACKAGE,
    UNRELATED_DOCUMENTS,
    VALID_SOLICITATION_PACKAGE,
    classify_package_documents,
    extract_pdf_text,
)
from opengov_discovery.public_document_client import (
    DOCUMENT_ACCESS_REQUIRES_AUTH,
    DOCUMENTS_FOUND_PUBLIC,
    INVALID_PROJECT,
    NO_PUBLIC_DOCUMENTS,
    PROJECT_NOT_FOUND,
    RETRYABLE_ERROR,
    OpenGovPublicDocumentClient,
)

log = logging.getLogger("govtracker.opengov_recovery.public_docs_batch")

CHECKPOINT = "m3_opengov_public_docs_checkpoint.json"
REPORT = "m3_opengov_public_docs_last_report.json"
BUILD_TARGET = "20261003-m3-package-access-v1"

VALID_FREE_PACKAGE_FOUND = "VALID_FREE_PACKAGE_FOUND"
PARTIAL_FREE_PACKAGE_FOUND = "PARTIAL_FREE_PACKAGE_FOUND"

# Aliases requested by package-access phase
UNRELATED_DOCUMENT = "UNRELATED_DOCUMENT"
GENERIC_AGENCY_DOCUMENT = "GENERIC_AGENCY_DOCUMENT"
MARKETING_DOCUMENT = "MARKETING_DOCUMENT"
AMBIGUOUS_DOCUMENT = "AMBIGUOUS_DOCUMENT"

NO_PACKAGE = "NO_PACKAGE"
PARTIAL_PACKAGE = "PARTIAL_PACKAGE"
CORE_PACKAGE = "CORE_PACKAGE"
FULL_PACKAGE = "FULL_PACKAGE"


def _paths():
    from m3_data_root import data_path

    return data_path(CHECKPOINT), data_path(REPORT)


def _load_ck() -> dict[str, Any]:
    path, _ = _paths()
    if not path.exists():
        return {"kind": "OpenGovPublicDocsCheckpoint", "processed_ids": [], "stats": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"kind": "OpenGovPublicDocsCheckpoint", "processed_ids": [], "stats": {}}


def _save_ck(payload: dict[str, Any]) -> None:
    path, _ = _paths()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = now_utc().isoformat()
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _save_report(payload: dict[str, Any]) -> None:
    _, path = _paths()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _map_quality(label: str) -> str:
    if label == VALID_SOLICITATION_PACKAGE:
        return VALID_FREE_PACKAGE_FOUND
    if label == PARTIAL_SOLICITATION_PACKAGE:
        return PARTIAL_FREE_PACKAGE_FOUND
    if label == UNRELATED_DOCUMENTS:
        return UNRELATED_DOCUMENT
    if label == AMBIGUOUS_DOCUMENTS:
        return AMBIGUOUS_DOCUMENT
    return label


def _completeness(quality: str, type_counts: dict[str, int]) -> str:
    if quality not in {VALID_FREE_PACKAGE_FOUND, PARTIAL_FREE_PACKAGE_FOUND, VALID_SOLICITATION_PACKAGE, PARTIAL_SOLICITATION_PACKAGE}:
        return NO_PACKAGE
    has_pricing = int(type_counts.get("pricing_schedule") or 0) > 0
    has_spec = int(type_counts.get("specifications") or 0) > 0
    has_addenda = int(type_counts.get("addenda") or 0) > 0
    has_bid = int(type_counts.get("bid_form") or 0) > 0
    has_core = (
        int(type_counts.get("solicitation_packet") or 0) > 0
        or has_spec
        or quality in {VALID_FREE_PACKAGE_FOUND, VALID_SOLICITATION_PACKAGE}
    )
    if has_core and (has_pricing or has_bid) and (has_addenda or has_spec):
        return FULL_PACKAGE
    if has_core or quality in {VALID_FREE_PACKAGE_FOUND, VALID_SOLICITATION_PACKAGE}:
        return CORE_PACKAGE
    return PARTIAL_PACKAGE


def select_opengov_projects_from_directory(
    *,
    limit: int,
    open_only: bool = True,
    max_entities: int | None = None,
) -> list[dict[str, Any]]:
    """Pull open projects from working government codes via project/public (no store required)."""
    from opengov_discovery.government_directory import OpenGovGovernmentDirectory
    from opengov_discovery.public_data_client import OpenGovPublicDataClient

    out: list[dict[str, Any]] = []
    with OpenGovGovernmentDirectory() as directory:
        codes = list(directory.codes)
    if max_entities is not None:
        codes = codes[: max(0, int(max_entities))]

    with OpenGovPublicDataClient() as client:
        for code in codes:
            if len(out) >= limit:
                break
            fr = client.fetch_project_public(
                code,
                page_size=50,
                max_pages=3,
                open_only=open_only,
            )
            if not fr.get("ok"):
                continue
            for row in fr.get("rows") or []:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                out.append(
                    {
                        "project_id": row.get("id"),
                        "government_code": code,
                        "title": row.get("title"),
                        "financial_id": row.get("financialId"),
                        "status": row.get("status"),
                        "proposal_deadline": row.get("proposalDeadline"),
                        "department": (row.get("department") or {}).get("name")
                        if isinstance(row.get("department"), dict)
                        else None,
                    }
                )
                if len(out) >= limit:
                    break
    return out[:limit]


def _persist_documents(
    *,
    project_id: str,
    government_code: str | None,
    docs: list[dict[str, Any]],
    client: OpenGovPublicDocumentClient,
    download: bool,
    extract_text_limit: int = 2,
) -> list[dict[str, Any]]:
    from m3_data_root import data_path

    out_dir = data_path("opengov_public_docs", "documents", str(government_code or "unknown"), str(project_id))
    out_dir.mkdir(parents=True, exist_ok=True)
    scored_inputs: list[dict[str, Any]] = []
    extract_budget = max(0, int(extract_text_limit))
    for d in docs:
        text = ""
        local_path = None
        name = str(d.get("filename") or d.get("document_name") or "doc.bin")
        ext = (d.get("file_extension") or (name.rsplit(".", 1)[-1] if "." in name else "")).lower()
        is_pdf = ext == "pdf" or name.lower().endswith(".pdf")
        role = str(d.get("document_type") or d.get("source_role") or "")
        want_text = extract_budget > 0 and is_pdf
        if download and d.get("document_url"):
            raw = client.download_bytes(str(d["document_url"]))
            if raw:
                safe = "".join(c if c.isalnum() or c in "._-+" else "_" for c in name)[:140]
                path = out_dir / safe
                path.write_bytes(raw)
                local_path = str(path)
                d["local_path"] = local_path
                d["byte_size"] = len(raw)
                d["content_hash"] = hashlib.sha1(raw).hexdigest()[:16]
                d["retrieval_status"] = "DOWNLOADED"
                if want_text and len(raw) <= 12_000_000:
                    text = extract_pdf_text(local_path, max_pages=12)
                    if text.strip():
                        extract_budget -= 1
        if not text:
            # Seed scorer with role + filename so solicitation_packet snapshots aren't empty
            text = f"{role} {name}".strip()
        scored_inputs.append(
            {
                "document_name": d.get("document_name") or d.get("filename") or "",
                "text": text,
                "local_path": local_path,
            }
        )
        d["extracted_text_chars"] = len(text or "")
    return scored_inputs


def _maybe_line_items(
    *,
    opportunity_key: str,
    title: str | None,
    buyer: str | None,
    docs: list[dict[str, Any]],
    scored_inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Handoff to product-identity pipeline — never treat PDF text as CSV."""
    # Prefer structured identity from downloaded package files when available
    try:
        from pathlib import Path

        from product_identity.normalizer import ProductIdentityNormalizer
        from product_identity.package_process import process_package

        # opportunity_key like opengov:{code}:{pid}
        parts = str(opportunity_key).split(":")
        if len(parts) >= 3 and parts[0] == "opengov":
            from m3_data_root import data_path

            pkg = data_path("opengov_public_docs", "documents", parts[1], parts[2])
            if pkg.exists():
                result = process_package(parts[1], parts[2], Path(pkg), normalizer=ProductIdentityNormalizer())
                usable = result.get("usable") or []
                exact = sum(
                    1
                    for u in usable
                    if str(u.get("identity_type") or "").startswith("EXACT")
                )
                generic = sum(
                    1 for u in usable if str(u.get("identity_type") or "") == "STRONG_GENERIC_SPEC"
                )
                return {
                    "line_count": len(result.get("identities") or []),
                    "exact_identity": exact,
                    "strong_generic": generic,
                    "usable_abc": len(usable),
                    "status": "PRODUCT_IDENTITY",
                }
    except Exception as exc:
        pass

    # Fallback: body text only (never csv_text=PDF)
    body_bits = [title or "", buyer or ""]
    for s in scored_inputs:
        body_bits.append(str(s.get("document_name") or ""))
        body_bits.append(str(s.get("text") or "")[:40000])
    body = "\n".join(body_bits)
    try:
        from line_item_economics.engine import analyze_line_item_economics

        lie = analyze_line_item_economics(
            opportunity_id=opportunity_key,
            title=title,
            buyer=buyer,
            body_text=body,
            csv_text=None,
            persist=True,
        )
        lines = lie.get("lines") or []
        exact = 0
        generic = 0
        for ln in lines if isinstance(lines, list) else []:
            if not isinstance(ln, dict):
                continue
            ic = str(ln.get("identity_class") or "").upper()
            if any(x in ic for x in ("EXACT", "NSN", "PART", "MODEL")):
                exact += 1
            elif "GENERIC" in ic or "SPEC" in ic:
                generic += 1
        return {
            "line_count": len(lines) if isinstance(lines, list) else 0,
            "exact_identity": exact,
            "strong_generic": generic,
            "status": lie.get("status") or lie.get("analysis_status"),
        }
    except Exception as exc:
        return {"line_count": 0, "error": type(exc).__name__}


def run_opengov_public_docs_stage(
    *,
    limit: int = 20,
    download: bool = True,
    resume: bool = True,
    max_entities: int | None = 80,
    run_id: str | None = None,
    on_progress: Callable[..., None] | None = None,
    handoff_line_items: bool = True,
) -> dict[str, Any]:
    """Staged OpenGov public document recovery with quality gate."""
    run_id = run_id or f"OGDOC-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    ck = _load_ck() if resume else {"kind": "OpenGovPublicDocsCheckpoint", "processed_ids": [], "stats": {}}
    done = set(ck.get("processed_ids") or []) if resume else set()

    # When resuming, `limit` is the total target attempted (not additional).
    remaining = max(0, int(limit) - len(done)) if resume else int(limit)
    projects = select_opengov_projects_from_directory(
        limit=max(remaining * 3, limit * 2),
        max_entities=max_entities,
    )
    seen_pids: set[str] = set()
    selected = []
    for p in projects:
        pid = str(p.get("project_id"))
        if not pid or pid in done or pid in seen_pids:
            continue
        seen_pids.add(pid)
        selected.append(p)
        if len(selected) >= remaining:
            break

    stats: Counter = Counter(ck.get("stats") or {}) if resume else Counter()
    # Current-run counters start from prior checkpoint totals when resuming.
    samples: list[dict[str, Any]] = []
    processed: list[str] = list(done)
    route_ok = int(stats.get("DOCUMENTS_FOUND_PUBLIC") or 0)


    def prog(pct: int, **extra: Any) -> None:
        if on_progress:
            try:
                on_progress(phase="OPENGOV_PUBLIC_DOCS", pct=pct, **extra)
            except Exception:
                pass

    prog(5, retrieved=0, selected=len(selected))

    with OpenGovPublicDocumentClient() as client:
        for i, proj in enumerate(selected):
            pid = str(proj.get("project_id"))
            code = proj.get("government_code")
            stats["attempted"] += 1
            result = client.fetch_project_documents(project_id=pid, government_code=code)
            status = str(result.get("status") or RETRYABLE_ERROR)
            stats[status] += 1
            docs = list(result.get("documents") or [])
            type_counts = dict(result.get("type_counts") or {})

            quality_label = None
            package_status = None
            completeness = NO_PACKAGE
            line_stats: dict[str, Any] = {}
            false_doc = False

            if status == DOCUMENTS_FOUND_PUBLIC and docs:
                route_ok += 1
                scored_inputs = _persist_documents(
                    project_id=pid,
                    government_code=code,
                    docs=docs,
                    client=client,
                    download=download,
                )
                meta = result.get("project_metadata") or {}
                agg = classify_package_documents(
                    scored_inputs,
                    title=meta.get("title") or proj.get("title"),
                    buyer=meta.get("government_name") or code,
                    solicitation_number=meta.get("financial_id") or proj.get("financial_id"),
                )
                raw_q = str(agg.get("package_quality") or AMBIGUOUS_DOCUMENTS)
                quality_label = _map_quality(raw_q)
                stats[quality_label] += 1
                if quality_label in {VALID_FREE_PACKAGE_FOUND, PARTIAL_FREE_PACKAGE_FOUND}:
                    package_status = quality_label
                    stats["valid_or_partial"] += 1
                    completeness = _completeness(quality_label, type_counts)
                    stats[completeness] += 1
                    if handoff_line_items and completeness in {CORE_PACKAGE, FULL_PACKAGE, PARTIAL_PACKAGE}:
                        line_stats = _maybe_line_items(
                            opportunity_key=f"opengov:{code}:{pid}",
                            title=meta.get("title") or proj.get("title"),
                            buyer=meta.get("government_name") or code,
                            docs=docs,
                            scored_inputs=scored_inputs,
                        )
                        if int(line_stats.get("line_count") or 0) > 0:
                            stats["line_item_ready"] += 1
                            stats["total_lines"] += int(line_stats.get("line_count") or 0)
                            stats["exact_identity"] += int(line_stats.get("exact_identity") or 0)
                            stats["strong_generic"] += int(line_stats.get("strong_generic") or 0)
                else:
                    false_doc = True
                    stats["false_docs_rejected"] += 1
                    package_status = quality_label
            elif status == NO_PUBLIC_DOCUMENTS:
                stats["no_docs"] += 1
            elif status == RETRYABLE_ERROR:
                stats["retryable"] += 1

            # Extension tallies
            for k, v in type_counts.items():
                if k.startswith("ext_"):
                    stats[k] += int(v)
                elif k in {"pricing_schedule", "specifications", "addenda", "bid_form", "solicitation_packet"}:
                    stats[f"type_{k}"] += int(v)
            stats["documents_total"] += len(docs)

            processed.append(pid)
            if len(samples) < 30:
                samples.append(
                    {
                        "project_id": pid,
                        "government_code": code,
                        "title": (proj.get("title") or "")[:100],
                        "access_status": status,
                        "docs": len(docs),
                        "quality": quality_label,
                        "package_status": package_status,
                        "completeness": completeness,
                        "false_doc": false_doc,
                        "line_items": line_stats.get("line_count"),
                        "type_counts": type_counts,
                    }
                )

            if (i + 1) % 10 == 0:
                _save_ck(
                    {
                        "kind": "OpenGovPublicDocsCheckpoint",
                        "run_id": run_id,
                        "processed_ids": processed[-100000:],
                        "stats": dict(stats),
                    }
                )
            pct = 5 + int(90 * (i + 1) / max(1, len(selected)))
            if (i + 1) % 5 == 0 or (i + 1) == len(selected):
                prog(
                    pct,
                    retrieved=i + 1,
                    found=stats.get("valid_or_partial", 0),
                    docs=stats.get("documents_total", 0),
                    selected=len(selected),
                )

    attempted = int(stats.get("attempted") or 0)
    valid_partial = int(stats.get("valid_or_partial") or 0)
    report = {
        "kind": "OpenGovPublicDocsStageReport",
        "build_target": BUILD_TARGET,
        "run_id": run_id,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "limit": limit,
        "selected": len(selected),
        "attempted": attempted,
        "projects_with_public_docs": int(stats.get(DOCUMENTS_FOUND_PUBLIC) or 0),
        "valid_packages": int(stats.get(VALID_FREE_PACKAGE_FOUND) or 0),
        "partial_packages": int(stats.get(PARTIAL_FREE_PACKAGE_FOUND) or 0),
        "valid_or_partial": valid_partial,
        "no_docs": int(stats.get("no_docs") or stats.get(NO_PUBLIC_DOCUMENTS) or 0),
        "retryable": int(stats.get("retryable") or stats.get(RETRYABLE_ERROR) or 0),
        "auth_required": int(stats.get(DOCUMENT_ACCESS_REQUIRES_AUTH) or 0),
        "not_found": int(stats.get(PROJECT_NOT_FOUND) or 0),
        "invalid": int(stats.get(INVALID_PROJECT) or 0),
        "false_docs_rejected": int(stats.get("false_docs_rejected") or 0),
        "documents_total": int(stats.get("documents_total") or 0),
        "pricing_schedules": int(stats.get("type_pricing_schedule") or 0),
        "specifications": int(stats.get("type_specifications") or 0),
        "addenda": int(stats.get("type_addenda") or 0),
        "bid_forms": int(stats.get("type_bid_form") or 0),
        "pdfs": int(stats.get("ext_pdf") or 0),
        "xls": int(stats.get("ext_xls") or 0) + int(stats.get("ext_xlsx") or 0),
        "csv": int(stats.get("ext_csv") or 0),
        "line_item_ready": int(stats.get("line_item_ready") or 0),
        "total_lines": int(stats.get("total_lines") or 0),
        "exact_identity": int(stats.get("exact_identity") or 0),
        "strong_generic": int(stats.get("strong_generic") or 0),
        "completeness": {
            "CORE_PACKAGE": int(stats.get(CORE_PACKAGE) or 0),
            "FULL_PACKAGE": int(stats.get(FULL_PACKAGE) or 0),
            "PARTIAL_PACKAGE": int(stats.get(PARTIAL_PACKAGE) or 0),
        },
        "opengov_public_doc_yield": round(valid_partial / attempted, 4) if attempted else 0.0,
        "route": "GET /api/v1/project/{id}",
        "route_successes": route_ok,
        "samples": samples,
        "stats": dict(stats),
        "note": "Public route only. BidNet membership not used. Quality gate required for valid/partial.",
    }
    _save_ck(
        {
            "kind": "OpenGovPublicDocsCheckpoint",
            "run_id": run_id,
            "processed_ids": processed[-100000:],
            "stats": dict(stats),
            "last_report_summary": {
                "attempted": attempted,
                "valid_or_partial": valid_partial,
                "yield": report["opengov_public_doc_yield"],
            },
        }
    )
    _save_report(report)
    prog(100, retrieved=attempted, found=valid_partial, docs=report["documents_total"])
    return report
