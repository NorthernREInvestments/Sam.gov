"""BidNet → official source resolution + free package recovery sample batch."""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any, Callable
from uuid import uuid4

from application_clock import now_utc
from document_quality import (
    PARTIAL_SOLICITATION_PACKAGE,
    VALID_SOLICITATION_PACKAGE,
    classify_package_documents,
    extract_pdf_text,
)
from official_source.agency_profile import PLATFORM_OPENGOV
from official_source.resolver import OfficialSourceResolver
from opengov_discovery.public_document_client import (
    DOCUMENTS_FOUND_PUBLIC,
    OpenGovPublicDocumentClient,
)

log = logging.getLogger("govtracker.official_source.batch")

CHECKPOINT = "m3_official_source_checkpoint.json"
REPORT = "m3_official_source_last_report.json"
BUILD_TARGET = "20261003-m3-package-access-v1"

VALID_FREE_PACKAGE_FOUND = "VALID_FREE_PACKAGE_FOUND"
PARTIAL_FREE_PACKAGE_FOUND = "PARTIAL_FREE_PACKAGE_FOUND"


def _paths():
    from m3_data_root import data_path

    return data_path(CHECKPOINT), data_path(REPORT)


def _load_ck() -> dict[str, Any]:
    path, _ = _paths()
    if not path.exists():
        return {"kind": "OfficialSourceCheckpoint", "processed_ids": [], "stats": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"kind": "OfficialSourceCheckpoint", "processed_ids": [], "stats": {}}


def _save_ck(payload: dict[str, Any]) -> None:
    path, _ = _paths()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = now_utc().isoformat()
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _save_report(payload: dict[str, Any]) -> None:
    _, path = _paths()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _is_product_eligible(rec: dict[str, Any]) -> bool:
    from universe_pass.classify import ELIGIBLE_FOR_PROFIT

    uc = str(rec.get("universe_class") or rec.get("product_service_classification") or "")
    if uc in ELIGIBLE_FOR_PROFIT:
        return True
    if rec.get("eligible_for_profit_research"):
        return True
    if uc in {"PURE_SERVICE", "CONSTRUCTION"}:
        return False
    return uc in {"", "UNKNOWN", "TANGIBLE_PRODUCT", "MIXED_PRODUCT_SERVICE"}


def _is_bidnet(rec: dict[str, Any]) -> bool:
    plat = str(rec.get("platform") or rec.get("platform_family") or "").lower()
    src = str(rec.get("source") or "").lower()
    return "bidnet" in plat or "bidnet" in src


def _priority_score(rec: dict[str, Any]) -> float:
    score = 0.0
    sol = str(rec.get("solicitation_number") or rec.get("solicitation_event_id") or "")
    if sol and len(sol) >= 4 and not sol.isdigit():
        score += 5.0
    elif sol:
        score += 1.0
    if rec.get("buyer") or rec.get("agency"):
        score += 3.0
    if rec.get("state") or rec.get("city") or rec.get("location"):
        score += 2.0
    title = str(rec.get("title") or "")
    if len(title) >= 40:
        score += 2.0
    if any(x in title.lower() for x in ("line item", "schedule", "supply", "equipment", "materials")):
        score += 1.5
    try:
        from datetime import datetime, timezone

        dl = str(rec.get("deadline") or "")[:10]
        if dl:
            days = (datetime.fromisoformat(dl).replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
            if 0 <= days <= 30:
                score += 3.0
            elif 0 <= days <= 90:
                score += 1.5
    except Exception:
        pass
    return score


def select_bidnet_official_sample(*, limit: int = 500, store: dict[str, Any] | None = None) -> list[tuple[str, dict[str, Any]]]:
    if store is None:
        from phase_l.l23_full_population_funnel import load_store

        store = load_store()
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for cid, rec in store.items():
        if not isinstance(rec, dict) or not _is_bidnet(rec):
            continue
        if not _is_product_eligible(rec):
            continue
        scored.append((_priority_score(rec), str(cid), rec))
    scored.sort(key=lambda x: -x[0])
    return [(cid, rec) for _, cid, rec in scored[:limit]]


def _recover_opengov_package(
    client: OpenGovPublicDocumentClient,
    *,
    government_code: str,
    project_id: str,
    title: str | None,
    buyer: str | None,
    sol: str | None,
    download: bool = True,
) -> dict[str, Any]:
    result = client.fetch_project_documents(project_id=project_id, government_code=government_code)
    status = str(result.get("status") or "")
    docs = list(result.get("documents") or [])
    if status != DOCUMENTS_FOUND_PUBLIC or not docs:
        return {
            "package_status": "NO_PUBLIC_PACKAGE",
            "access_status": status,
            "documents": docs,
            "false_doc": False,
        }

    scored_inputs = []
    for d in docs:
        text = str(d.get("document_name") or d.get("filename") or "")
        if download and d.get("document_url"):
            raw = client.download_bytes(str(d["document_url"]))
            if raw and ((d.get("file_extension") or "").lower() == "pdf" or text.lower().endswith(".pdf")):
                from m3_data_root import data_path
                import hashlib

                out = data_path("official_source_docs", government_code, str(project_id))
                out.mkdir(parents=True, exist_ok=True)
                safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in text)[:120] or "doc.pdf"
                path = out / safe
                path.write_bytes(raw)
                d["local_path"] = str(path)
                d["content_hash"] = hashlib.sha1(raw).hexdigest()[:16]
                text = extract_pdf_text(str(path), max_pages=30) or text
        scored_inputs.append({"document_name": d.get("document_name") or d.get("filename") or "", "text": text})

    agg = classify_package_documents(
        scored_inputs,
        title=title,
        buyer=buyer,
        solicitation_number=sol,
    )
    raw_q = str(agg.get("package_quality") or "")
    if raw_q == VALID_SOLICITATION_PACKAGE:
        pkg = VALID_FREE_PACKAGE_FOUND
    elif raw_q == PARTIAL_SOLICITATION_PACKAGE:
        pkg = PARTIAL_FREE_PACKAGE_FOUND
    else:
        pkg = "FALSE_DOC_REJECTED"
    return {
        "package_status": pkg,
        "access_status": status,
        "documents": docs,
        "package_quality": agg,
        "false_doc": pkg == "FALSE_DOC_REJECTED",
        "type_counts": result.get("type_counts") or {},
    }


def run_official_source_batch(
    *,
    limit: int = 500,
    resume: bool = True,
    download: bool = True,
    run_id: str | None = None,
    on_progress: Callable[..., None] | None = None,
    handoff_line_items: bool = True,
) -> dict[str, Any]:
    run_id = run_id or f"OSR-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    ck = _load_ck() if resume else {"kind": "OfficialSourceCheckpoint", "processed_ids": [], "stats": {}}
    done = set(ck.get("processed_ids") or []) if resume else set()

    from phase_l.l23_full_population_funnel import load_store, save_store

    store = load_store()
    selected = [(cid, rec) for cid, rec in select_bidnet_official_sample(limit=limit * 2, store=store) if cid not in done][
        :limit
    ]

    stats: Counter = Counter()
    platform_c: Counter = Counter()
    route_c: Counter = Counter()
    fail_c: Counter = Counter()
    samples: list[dict[str, Any]] = []
    processed = list(done)
    resolver = OfficialSourceResolver()

    def prog(pct: int, **extra: Any) -> None:
        if on_progress:
            try:
                on_progress(phase="OFFICIAL_SOURCE_BATCH", pct=pct, **extra)
            except Exception:
                pass

    prog(3, selected=len(selected))

    with OpenGovPublicDocumentClient() as og_client:
        for i, (cid, rec) in enumerate(selected):
            stats["attempted"] += 1
            resolution = resolver.resolve(rec)
            rec = dict(rec)
            br = dict(rec.get("bidnet_recovery") or {})
            br["official_source"] = resolution
            resolved = bool(resolution.get("official_source_resolved"))
            plat = str(resolution.get("resolved_platform") or "UNKNOWN")
            if resolved:
                stats["official_source_resolved"] += 1
                stats["platform_identified"] += 1
                platform_c[plat] += 1
                route_c[f"resolve:{resolution.get('match_tier') or plat}"] += 1
            else:
                stats["unresolved"] += 1
                fail_c["unresolved"] += 1

            pkg_status = "NO_PUBLIC_PACKAGE"
            false_doc = False
            line_stats: dict[str, Any] = {}

            # OpenGov with project match → recover docs; agency-only → retryable
            if resolved and plat == PLATFORM_OPENGOV and resolution.get("project_id"):
                recovered = _recover_opengov_package(
                    og_client,
                    government_code=str(resolution.get("government_code") or ""),
                    project_id=str(resolution["project_id"]),
                    title=rec.get("title") or resolution.get("project_title"),
                    buyer=rec.get("buyer") or resolution.get("resolved_agency"),
                    sol=rec.get("solicitation_number") or resolution.get("project_financial_id"),
                    download=download,
                )
                pkg_status = str(recovered.get("package_status") or pkg_status)
                false_doc = bool(recovered.get("false_doc"))
                docs = list(recovered.get("documents") or [])
                br["free_package_chase"] = {
                    **dict(br.get("free_package_chase") or {}),
                    "status": pkg_status
                    if pkg_status in {VALID_FREE_PACKAGE_FOUND, PARTIAL_FREE_PACKAGE_FOUND}
                    else ("PACKAGE_MATCH_AMBIGUOUS" if false_doc else "PACKAGE_UNAVAILABLE_FREE"),
                    "documents": docs,
                    "document_count": len(docs),
                    "matched_source": "official_source_opengov",
                    "source_url": resolution.get("resolved_opportunity_url"),
                    "package_quality": recovered.get("package_quality"),
                    "via": "OfficialSourceResolver",
                    "at": now_utc().isoformat(),
                }
                if pkg_status == VALID_FREE_PACKAGE_FOUND:
                    stats["valid_packages"] += 1
                    route_c["GET /api/v1/project/{id}"] += 1
                elif pkg_status == PARTIAL_FREE_PACKAGE_FOUND:
                    stats["partial_packages"] += 1
                    route_c["GET /api/v1/project/{id}"] += 1
                elif false_doc:
                    stats["false_docs_rejected"] += 1
                    fail_c["false_doc"] += 1
                else:
                    stats["no_public_package"] += 1
                    fail_c[str(recovered.get("access_status") or "no_docs")] += 1

                if (
                    handoff_line_items
                    and pkg_status in {VALID_FREE_PACKAGE_FOUND, PARTIAL_FREE_PACKAGE_FOUND}
                    and docs
                ):
                    try:
                        from line_item_economics.engine import analyze_line_item_economics

                        body = "\n".join(
                            [
                                str(rec.get("title") or ""),
                                str(rec.get("buyer") or ""),
                                *[str(d.get("document_name") or "") for d in docs],
                            ]
                        )
                        lie = analyze_line_item_economics(
                            opportunity_id=cid,
                            title=rec.get("title"),
                            buyer=rec.get("buyer"),
                            body_text=body,
                            persist=True,
                        )
                        lines = lie.get("lines") or []
                        n = len(lines) if isinstance(lines, list) else 0
                        if n:
                            stats["line_item_ready"] += 1
                            stats["total_lines"] += n
                            line_stats = {"line_count": n}
                    except Exception as exc:
                        line_stats = {"error": type(exc).__name__}
            elif resolved and plat == PLATFORM_OPENGOV and resolution.get("government_code"):
                # Agency portal known but no project id — run free chase OpenGov matcher
                try:
                    from bidnet_recovery.free_package_chase import chase_free_package

                    chase = chase_free_package(rec, store=store, refresh_overview=False)
                    st = str(chase.get("status") or "")
                    docs = list(chase.get("documents") or [])
                    br["free_package_chase"] = {
                        **dict(chase),
                        "via": "OfficialSourceResolver+opengov_free_match",
                        "official_source": resolution,
                    }
                    if st in {VALID_FREE_PACKAGE_FOUND, PARTIAL_FREE_PACKAGE_FOUND} and docs:
                        pkg_status = st
                        if st == VALID_FREE_PACKAGE_FOUND:
                            stats["valid_packages"] += 1
                        else:
                            stats["partial_packages"] += 1
                        route_c["opengov_free_match"] += 1
                    elif docs:
                        stats["ambiguous"] += 1
                        fail_c["opengov_match_ambiguous"] += 1
                        pkg_status = "AMBIGUOUS"
                    else:
                        stats["no_public_package"] += 1
                        stats["retryable"] += 1
                        fail_c["opengov_agency_no_project_match"] += 1
                        pkg_status = "NO_PUBLIC_PACKAGE"
                except Exception as exc:
                    stats["retryable"] += 1
                    stats["no_public_package"] += 1
                    fail_c[f"chase_error:{type(exc).__name__}"] += 1
            elif resolved:
                # Platform identified but no project-level OpenGov match yet
                stats["no_public_package"] += 1
                fail_c[f"platform_no_project:{plat}"] += 1
                br["free_package_chase"] = {
                    **dict(br.get("free_package_chase") or {}),
                    "status": "PACKAGE_RECOVERY_RETRYABLE",
                    "note": "Official platform resolved; project-level package not yet recovered",
                    "matched_source": plat,
                    "via": "OfficialSourceResolver",
                    "at": now_utc().isoformat(),
                }
                stats["retryable"] += 1
            else:
                stats["no_public_package"] += 1

            rec["bidnet_recovery"] = br
            store[cid] = rec
            processed.append(cid)

            if len(samples) < 25:
                samples.append(
                    {
                        "id": cid[:18],
                        "title": str(rec.get("title") or "")[:90],
                        "platform": plat,
                        "resolved": resolved,
                        "confidence": resolution.get("confidence"),
                        "match_tier": resolution.get("match_tier"),
                        "package_status": pkg_status,
                        "project_id": resolution.get("project_id"),
                        "line_items": line_stats.get("line_count"),
                    }
                )

            if (i + 1) % 25 == 0:
                save_store(store)
                _save_ck(
                    {
                        "kind": "OfficialSourceCheckpoint",
                        "run_id": run_id,
                        "processed_ids": processed[-100000:],
                        "stats": dict(stats),
                    }
                )
            if (i + 1) % 10 == 0 or (i + 1) == len(selected):
                prog(
                    5 + int(90 * (i + 1) / max(1, len(selected))),
                    retrieved=i + 1,
                    resolved=stats.get("official_source_resolved", 0),
                    found=int(stats.get("valid_packages", 0)) + int(stats.get("partial_packages", 0)),
                )

    save_store(store)
    attempted = int(stats.get("attempted") or 0)
    valid_partial = int(stats.get("valid_packages") or 0) + int(stats.get("partial_packages") or 0)
    resolved_n = int(stats.get("official_source_resolved") or 0)
    false_n = int(stats.get("false_docs_rejected") or 0)
    report = {
        "kind": "OfficialSourceBatchReport",
        "build_target": BUILD_TARGET,
        "run_id": run_id,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "attempted": attempted,
        "official_source_resolved": resolved_n,
        "platform_identified": int(stats.get("platform_identified") or 0),
        "platforms": dict(platform_c),
        "valid_packages": int(stats.get("valid_packages") or 0),
        "partial_packages": int(stats.get("partial_packages") or 0),
        "no_public_package": int(stats.get("no_public_package") or 0),
        "retryable": int(stats.get("retryable") or 0),
        "ambiguous": int(stats.get("ambiguous") or 0),
        "false_docs_rejected": false_n,
        "unresolved": int(stats.get("unresolved") or 0),
        "line_item_ready": int(stats.get("line_item_ready") or 0),
        "total_lines": int(stats.get("total_lines") or 0),
        "official_source_resolution_yield": round(resolved_n / attempted, 4) if attempted else 0.0,
        "valid_or_partial_package_yield": round(valid_partial / attempted, 4) if attempted else 0.0,
        "false_document_rate": round(false_n / max(1, false_n + valid_partial), 4),
        "top_working_routes": dict(route_c.most_common(12)),
        "top_failure_routes": dict(fail_c.most_common(12)),
        "samples": samples,
        "stats": dict(stats),
    }
    _save_ck(
        {
            "kind": "OfficialSourceCheckpoint",
            "run_id": run_id,
            "processed_ids": processed[-100000:],
            "stats": dict(stats),
            "last_report_summary": {
                "attempted": attempted,
                "resolved": resolved_n,
                "valid_or_partial": valid_partial,
            },
        }
    )
    _save_report(report)
    prog(100, retrieved=attempted, resolved=resolved_n, found=valid_partial)
    return report
