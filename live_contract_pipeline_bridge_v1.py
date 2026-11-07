"""Bridge live anonymous discovery into document recovery + commercial decision.

Does not create a parallel discovery system. Reuses OpenGov public docs,
free package chase for non-BidNet URLs, L23 store, and verified commercial decision.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any
from uuid import uuid4

from application_clock import now_utc

log = logging.getLogger("govtracker.live_contract_pipeline_bridge_v1")

BUILD = "20261107-m3-live-contract-pipeline-bridge-v1"

_PRODUCT = re.compile(
    r"\b(supply|supplies|equipment|materials|furniture|paper|toner|ppe|gloves|"
    r"tools|parts|vehicle|fleet|chemical|commodity|goods|product|kit)\b",
    re.I,
)
_SERVICE = re.compile(
    r"\b(service|services|consulting|construction|renovation|staffing|labor|"
    r"professional\s+services|janitorial\s+service|landscap|roofing)\b",
    re.I,
)


def _is_bidnet(rec: dict[str, Any]) -> bool:
    blob = " ".join(
        str(rec.get(k) or "")
        for k in ("platform", "source_id", "source", "authoritative_url", "detail_url")
    ).lower()
    return "bidnet" in blob


def _is_productish(rec: dict[str, Any]) -> bool:
    title = str(rec.get("title") or "")
    if _PRODUCT.search(title):
        return True
    if _SERVICE.search(title) and not _PRODUCT.search(title):
        return False
    return True  # unknown → allow through for doc stage


def select_live_pipeline_candidates(*, limit: int = 40) -> list[tuple[str, dict[str, Any]]]:
    from phase_l.l23_full_population_funnel import load_store

    store = load_store()
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for cid, rec in store.items():
        if not isinstance(rec, dict) or _is_bidnet(rec):
            continue
        if not _is_productish(rec):
            continue
        url = str(rec.get("authoritative_url") or rec.get("detail_url") or "")
        if not url.startswith("http"):
            continue
        score = 0.0
        plat = str(rec.get("platform") or rec.get("source_id") or "").lower()
        if "socrata" in plat or "opengov" in plat or "jaggaer" in plat:
            score += 3.0
        if "opengov" in url.lower():
            score += 4.0
        if rec.get("deadline") or rec.get("response_deadline"):
            score += 2.0
        if _PRODUCT.search(str(rec.get("title") or "")):
            score += 2.0
        # Prefer recently touched
        for k in ("updated_at", "discovered_at", "ingested_at", "last_seen_at"):
            if rec.get(k):
                score += 1.0
                break
        scored.append((score, str(cid), rec))
    scored.sort(key=lambda x: -x[0])
    return [(cid, rec) for _, cid, rec in scored[:limit]]


def recover_package_for_record(cid: str, rec: dict[str, Any]) -> dict[str, Any]:
    """Best-effort anonymous package recovery for one non-BidNet opportunity."""
    url = str(rec.get("authoritative_url") or rec.get("detail_url") or "")
    out: dict[str, Any] = {
        "opportunity_id": cid,
        "url": url[:200],
        "platform": rec.get("platform") or rec.get("source_id"),
        "docs": 0,
        "status": "NO_ATTEMPT",
    }
    if "bidnet" in url.lower():
        out["status"] = "SKIP_BIDNET_ABANDONED"
        return out

    # Already have chase docs?
    br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
    chase = br.get("free_package_chase") if isinstance(br.get("free_package_chase"), dict) else {}
    existing = chase.get("documents") or rec.get("attachments_metadata") or []
    if existing:
        out["docs"] = len(existing) if isinstance(existing, list) else 1
        out["status"] = "ALREADY_HAS_PACKAGE_HINTS"
        return out

    # OpenGov project docs (numeric project IDs only — matches public API)
    if "opengov.com" in url.lower() or "procurement.opengov" in url.lower():
        try:
            from opengov_discovery.public_document_client import OpenGovPublicDocumentClient
            import re as _re

            m = _re.search(r"/projects?/(\d+)", url, _re.I)
            if not m:
                m = _re.search(r"[?&](?:projectId|project_id)=(\d+)", url, _re.I)
            gov = None
            gm = _re.search(r"/portal/([^/?#]+)", url, _re.I)
            if gm:
                gov = gm.group(1)
            if m:
                with OpenGovPublicDocumentClient() as client:
                    result = client.fetch_project_documents(
                        project_id=m.group(1),
                        government_code=str(gov or "") or None,
                    )
                docs = list(result.get("documents") or [])
                out["docs"] = len(docs)
                out["status"] = str(result.get("status") or ("DOCS" if docs else "NO_DOCS"))
                out["opengov_project_id"] = m.group(1)
                if docs:
                    from phase_l.l23_full_population_funnel import load_store, save_store

                    store = load_store()
                    row = dict(store.get(cid) or rec)
                    br2 = dict(row.get("bidnet_recovery") or {})
                    br2["free_package_chase"] = {
                        "status": "DOCUMENTS_FOUND_PUBLIC",
                        "documents": docs[:40],
                        "document_count": len(docs),
                        "matched_source": "live_pipeline_opengov",
                        "source_url": url,
                        "via": "live_contract_pipeline_bridge_v1",
                        "at": now_utc().isoformat(),
                    }
                    row["bidnet_recovery"] = br2
                    row["live_pipeline_stage"] = "DOCUMENT_RECOVERY"
                    store[cid] = row
                    save_store(store)
                return out
            out["status"] = "OPENGOV_NO_NUMERIC_PROJECT_ID"
            return out
        except Exception as exc:
            out["status"] = f"OPENGOV_ERROR:{type(exc).__name__}"
            out["error"] = str(exc)[:160]
            return out

    # Generic public HTTP page — capture attachment-like links (listing evidence only)
    try:
        from discovery.http_client import PublicProcurementHttpClient, RequestBudget

        client = PublicProcurementHttpClient(
            budget=RequestBudget(
                max_total_requests=3,
                max_requests_per_source=2,
                max_pages_per_source=1,
                max_records_per_source=5,
                max_runtime_seconds=30.0,
                max_retries=0,
                min_interval_seconds=0.2,
                timeout_seconds=15.0,
            ),
            authorize_live=True,
            user_agent="M3LivePipelineBridge/1.0",
        )
        r = client.get(url)
        html = r.text or ""
        out["http_status"] = int(r.status_code)
        # Find pdf/doc links
        links = re.findall(
            r'href=["\']([^"\']+\.(?:pdf|docx?|xlsx?|csv))["\']',
            html,
            flags=re.I,
        )
        out["attachment_link_count"] = len(links)
        out["status"] = "LISTING_OR_PARTIAL_LINKS" if links or r.status_code < 400 else "DETAIL_FETCH_FAILED"
        if links:
            from phase_l.l23_full_population_funnel import load_store, save_store

            store = load_store()
            row = dict(store.get(cid) or rec)
            br2 = dict(row.get("bidnet_recovery") or {})
            docs = [{"document_url": u, "document_name": u.rsplit("/", 1)[-1][:120]} for u in links[:20]]
            br2["free_package_chase"] = {
                "status": "PARTIAL_FREE_PACKAGE_FOUND",
                "documents": docs,
                "document_count": len(docs),
                "matched_source": "live_pipeline_html_links",
                "source_url": url,
                "via": "live_contract_pipeline_bridge_v1",
                "at": now_utc().isoformat(),
            }
            row["bidnet_recovery"] = br2
            row["live_pipeline_stage"] = "DOCUMENT_RECOVERY"
            store[cid] = row
            save_store(store)
            out["docs"] = len(docs)
        return out
    except Exception as exc:
        out["status"] = f"HTTP_ERROR:{type(exc).__name__}"
        out["error"] = str(exc)[:160]
        return out


def ingest_opengov_open_projects(*, limit: int = 40, max_entities: int = 40) -> dict[str, Any]:
    """Pull currently open OpenGov projects and merge into L23 (no BidNet)."""
    from m3_canonical_discovery_bridge import merge_discovery_into_canonical
    from opengov_recovery.public_docs_batch import select_opengov_projects_from_directory

    projects = select_opengov_projects_from_directory(
        limit=limit,
        open_only=True,
        max_entities=max_entities,
    )
    records: list[dict[str, Any]] = []
    for p in projects:
        pid = str(p.get("project_id") or "").strip()
        code = str(p.get("government_code") or "").strip()
        if not pid or not code:
            continue
        detail = f"https://procurement.opengov.com/portal/{code}/projects/{pid}"
        records.append(
            {
                "title": p.get("title") or f"OpenGov project {pid}",
                "buyer": code,
                "agency": code,
                "solicitation_number": p.get("financial_id") or pid,
                "deadline": p.get("proposal_deadline"),
                "response_deadline": p.get("proposal_deadline"),
                "detail_url": detail,
                "authoritative_url": detail,
                "source_url": detail,
                "external_id": f"opengov:{code}:{pid}",
                "platform": "live_opengov",
                "source_id": f"opengov_public:{code}",
                "source": "opengov_public_directory",
                "department": p.get("department"),
                "status": p.get("status") or "open",
                "live_pipeline_stage": "DISCOVERY",
                "discovered_via": BUILD,
            }
        )
    merge = None
    if records:
        merge = merge_discovery_into_canonical(
            run_id=f"lcpb-og-{uuid4().hex[:8]}",
            trigger="live_contract_pipeline_bridge",
            records=records,
            sources_attempted=[f"opengov_public:{r.get('buyer')}" for r in records[:20]],
            sources_succeeded=list({f"opengov_public:{r.get('buyer')}" for r in records}),
            sources_failed=[],
            raw_opportunities_found=len(records),
            records_normalized=len(records),
            api_usage={"SAM": 0, "opengov_public_directory": True},
            persist=True,
        )
    return {
        "projects_selected": len(projects),
        "records_normalized": len(records),
        "canonical_merge": {
            "new": (merge or {}).get("new_canonical_opportunities_added"),
            "updated": (merge or {}).get("existing_opportunities_updated"),
            "duplicates": (merge or {}).get("duplicates_detected"),
        }
        if merge
        else {"new": 0, "updated": 0},
    }


def attach_opengov_docs_to_l23(
    *,
    limit: int = 80,
    batch_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach recovered OpenGov public docs onto matching L23 rows by project id."""
    from m3_data_root import data_path
    from phase_l.l23_full_population_funnel import load_store, save_store

    docs_root = data_path("opengov_public_docs", "documents")
    by_pid: dict[str, list[dict[str, Any]]] = {}

    # Disk downloads
    if docs_root.exists():
        for gov_dir in docs_root.iterdir():
            if not gov_dir.is_dir():
                continue
            for pid_dir in gov_dir.iterdir():
                if not pid_dir.is_dir():
                    continue
                files = []
                for f in pid_dir.iterdir():
                    if f.is_file() and f.suffix.lower() in {
                        ".pdf",
                        ".doc",
                        ".docx",
                        ".xlsx",
                        ".xls",
                        ".csv",
                        ".zip",
                        ".json",
                        ".txt",
                    }:
                        files.append(
                            {
                                "document_name": f.name,
                                "local_path": str(f),
                                "document_url": f"file://{f}",
                                "government_code": gov_dir.name,
                            }
                        )
                if files:
                    by_pid[str(pid_dir.name)] = files

    # Also index batch report samples (metadata even when download skipped)
    samples_in = list((batch_report or {}).get("samples") or [])
    for s in samples_in:
        if not isinstance(s, dict):
            continue
        pid = str(s.get("project_id") or "").strip()
        if not pid:
            continue
        n = int(s.get("docs") or s.get("document_count") or 0)
        docs = list(s.get("documents") or [])
        if not docs and n > 0:
            docs = [
                {
                    "document_name": f"opengov_doc_{i}",
                    "via": "batch_report",
                    "government_code": s.get("government_code"),
                    "quality": s.get("quality"),
                    "package_status": s.get("package_status"),
                    "line_items": s.get("line_items"),
                }
                for i in range(min(n, 40))
            ]
        if docs and pid not in by_pid:
            by_pid[pid] = docs

    # Last report fallback
    if not by_pid:
        try:
            last = data_path("m3_opengov_public_docs_last_report.json")
            if last.exists():
                last_doc = json.loads(last.read_text(encoding="utf-8"))
                for s in last_doc.get("samples") or []:
                    if not isinstance(s, dict):
                        continue
                    pid = str(s.get("project_id") or "").strip()
                    n = int(s.get("document_count") or 0)
                    if pid and n > 0 and pid not in by_pid:
                        by_pid[pid] = [
                            {"document_name": f"opengov_doc_{i}", "via": "last_report"} for i in range(min(n, 20))
                        ]
        except Exception:
            pass

    if not by_pid:
        return {"attached": 0, "reason": "no_downloaded_docs"}

    store = load_store()
    attached = 0
    samples: list[dict[str, Any]] = []
    for cid, rec in store.items():
        if attached >= limit:
            break
        if not isinstance(rec, dict):
            continue
        blob = " ".join(
            str(rec.get(k) or "")
            for k in ("detail_url", "authoritative_url", "external_id", "solicitation_number")
        )
        matched_pid = None
        for pid in by_pid:
            if pid and re.search(rf"(?:/projects?/|{re.escape(':')}|^){re.escape(pid)}(?:/|$|\b)", blob):
                matched_pid = pid
                break
            if pid and pid in blob:
                matched_pid = pid
                break
        if not matched_pid:
            continue
        docs = by_pid[matched_pid]
        row = dict(rec)
        br = dict(row.get("bidnet_recovery") or {})
        existing = br.get("free_package_chase") if isinstance(br.get("free_package_chase"), dict) else {}
        if int(existing.get("document_count") or 0) >= len(docs) and existing.get("documents"):
            continue
        br["free_package_chase"] = {
            "status": "DOCUMENTS_FOUND_PUBLIC",
            "documents": docs[:40],
            "document_count": len(docs),
            "matched_source": "opengov_public_docs_disk_or_report",
            "via": BUILD,
            "at": now_utc().isoformat(),
            "opengov_project_id": matched_pid,
        }
        row["bidnet_recovery"] = br
        row["live_pipeline_stage"] = "DOCUMENT_RECOVERY"
        row["package_document_count"] = len(docs)
        store[cid] = row
        attached += 1
        if len(samples) < 12:
            samples.append({"opportunity_id": cid, "project_id": matched_pid, "docs": len(docs)})
    if attached:
        save_store(store)
    return {"attached": attached, "projects_with_docs": len(by_pid), "samples": samples}


def probe_sam_live_one_call(*, authorize_live: bool = True) -> dict[str, Any]:
    """Spend at most one SAM API call (never the reserve) for live federal discovery proof."""
    try:
        from discovery.sam_budgeted_client import (
            owner_credit_dashboard,
            search_opportunities,
            sam_daily_call_budget,
        )
    except Exception as exc:
        return {"ok": False, "error": f"import:{type(exc).__name__}:{exc}"}

    dash_before = owner_credit_dashboard()
    remaining = int(
        dash_before.get("production_budget_remaining")
        or dash_before.get("REMAINING_REAL_CREDITS")
        or 0
    )
    if remaining <= 0:
        return {
            "ok": False,
            "status": "SAM_DAILY_BUDGET_EXHAUSTED",
            "dashboard": {
                "REAL_SAM_API_CALLS_TODAY": dash_before.get("REAL_SAM_API_CALLS_TODAY"),
                "REMAINING_REAL_CREDITS": dash_before.get("REMAINING_REAL_CREDITS"),
                "production_budget_remaining": dash_before.get("production_budget_remaining"),
            },
            "note": "No production SAM calls remaining; skipped live call",
        }

    # Prefer cache; if miss, one live call for active product-ish notices
    result = search_opportunities(
        {
            "limit": 25,
            "offset": 0,
            "ptype": "o,k",
            "ncode": "339113",
        },
        reason="live_contract_pipeline_bridge:product_probe",
        authorize_live=authorize_live,
        use_reserve=False,
    )
    meta = result.get("_meta") or {}
    opps = list(result.get("opportunitiesData") or [])
    merged = None
    if opps:
        records = []
        for o in opps[:25]:
            if not isinstance(o, dict):
                continue
            notice_id = str(o.get("noticeId") or o.get("solicitationNumber") or "")
            title = o.get("title") or ""
            url = o.get("uiLink") or o.get("fullParentPathName") or ""
            if notice_id and not str(url).startswith("http"):
                url = f"https://sam.gov/opp/{notice_id}/view"
            records.append(
                {
                    "title": title,
                    "buyer": o.get("fullParentPathName") or o.get("department") or "Federal",
                    "agency": o.get("department") or o.get("organizationName"),
                    "solicitation_number": o.get("solicitationNumber") or notice_id,
                    "deadline": o.get("responseDeadLine") or o.get("response_deadline"),
                    "response_deadline": o.get("responseDeadLine"),
                    "detail_url": url,
                    "authoritative_url": url,
                    "external_id": f"sam:{notice_id}" if notice_id else None,
                    "platform": "sam_gov",
                    "source_id": "sam_gov_api",
                    "source": "sam_budgeted_live_probe",
                    "naics": o.get("naicsCode") or o.get("ncode"),
                    "live_pipeline_stage": "DISCOVERY",
                    "discovered_via": BUILD,
                    "sam_notice_id": notice_id,
                }
            )
        if records:
            from m3_canonical_discovery_bridge import merge_discovery_into_canonical

            merged = merge_discovery_into_canonical(
                run_id=f"lcpb-sam-{uuid4().hex[:8]}",
                trigger="live_contract_pipeline_bridge_sam",
                records=records,
                sources_attempted=["sam_gov_api"],
                sources_succeeded=["sam_gov_api"] if opps else [],
                sources_failed=[],
                raw_opportunities_found=len(opps),
                records_normalized=len(records),
                api_usage={"SAM": int(meta.get("credits_consumed") or 0)},
                persist=True,
            )
    return {
        "ok": bool(opps) or meta.get("status") == "CACHE_HIT",
        "meta": meta,
        "opportunity_count": len(opps),
        "samples": [
            {
                "title": (o.get("title") or "")[:100],
                "sol": o.get("solicitationNumber"),
                "deadline": o.get("responseDeadLine"),
                "notice": o.get("noticeId"),
            }
            for o in opps[:5]
            if isinstance(o, dict)
        ],
        "canonical_merge": {
            "new": (merged or {}).get("new_canonical_opportunities_added"),
            "updated": (merged or {}).get("existing_opportunities_updated"),
        }
        if merged
        else None,
        "dashboard_before": {
            "used": dash_before.get("REAL_SAM_API_CALLS_TODAY"),
            "remaining_real": dash_before.get("REMAINING_REAL_CREDITS"),
            "production_remaining": remaining,
            "budget": sam_daily_call_budget(),
        },
    }


def run_live_contract_pipeline_bridge(
    *,
    limit: int = 30,
    run_opengov_batch: bool = True,
    opengov_limit: int = 25,
    ingest_opengov: bool = True,
    probe_sam: bool = True,
    evaluate_commercial: bool = True,
    run_product_identity: bool = True,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Live discovery → docs → lines → commercial decision (existing engines only)."""
    run_id = run_id or f"LCPB-{uuid4().hex[:10]}"
    started = now_utc().isoformat()

    og_ingest = None
    if ingest_opengov:
        try:
            og_ingest = ingest_opengov_open_projects(limit=max(opengov_limit, 20), max_entities=40)
        except Exception as exc:
            og_ingest = {"error": f"{type(exc).__name__}: {exc}"}

    sam_probe = None
    if probe_sam:
        try:
            sam_probe = probe_sam_live_one_call(authorize_live=True)
        except Exception as exc:
            sam_probe = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    candidates = select_live_pipeline_candidates(limit=limit)
    package_results: list[dict[str, Any]] = []
    status_c: Counter = Counter()

    for cid, rec in candidates:
        row = recover_package_for_record(cid, rec)
        package_results.append(row)
        status_c[str(row.get("status") or "UNKNOWN")] += 1

    og_batch = None
    if run_opengov_batch:
        try:
            from opengov_recovery.public_docs_batch import run_opengov_public_docs_stage

            og_batch = run_opengov_public_docs_stage(
                limit=opengov_limit,
                resume=True,
                download=True,
                handoff_line_items=True,
                run_id=f"{run_id}-ogdocs",
            )
        except Exception as exc:
            og_batch = {"error": f"{type(exc).__name__}: {exc}"}

    attach = None
    try:
        attach = attach_opengov_docs_to_l23(
            limit=max(limit, 60),
            batch_report=og_batch if isinstance(og_batch, dict) else None,
        )
    except Exception as exc:
        attach = {"error": f"{type(exc).__name__}: {exc}"}

    product_identity = None
    if run_product_identity:
        try:
            from product_identity.batch import run_product_identity_stage

            product_identity = run_product_identity_stage(
                line_limit=min(200, max(limit * 5, 50)),
                resume=True,
                handoff=True,
                run_id=f"{run_id}-pi",
            )
        except Exception as exc:
            product_identity = {"error": f"{type(exc).__name__}: {exc}"}

    commercial = None
    if evaluate_commercial:
        try:
            from owner_commercial_queue_v1 import scan_owner_commercial_queues

            commercial = scan_owner_commercial_queues(
                limit=min(150, max(limit * 3, 80)),
                prefer_non_bidnet=True,
            )
        except Exception as exc:
            commercial = {"error": f"{type(exc).__name__}: {exc}"}

    # Inventory snapshot of live pipeline stages
    stage_counts: Counter = Counter()
    product_only = 0
    with_docs = 0
    with_lines = 0
    try:
        from phase_l.l23_full_population_funnel import load_store

        store = load_store()
        for cid, rec in store.items():
            if not isinstance(rec, dict) or _is_bidnet(rec):
                continue
            stage = str(rec.get("live_pipeline_stage") or "")
            if stage:
                stage_counts[stage] += 1
            if _PRODUCT.search(str(rec.get("title") or "")):
                product_only += 1
            br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
            chase = br.get("free_package_chase") if isinstance(br.get("free_package_chase"), dict) else {}
            docs = chase.get("documents") or rec.get("attachments_metadata") or []
            if docs:
                with_docs += 1
            if rec.get("line_items") or rec.get("usable_product_lines"):
                with_lines += 1
    except Exception:
        pass

    report = {
        "kind": "LiveContractPipelineBridgeReport",
        "build": BUILD,
        "run_id": run_id,
        "started_at": started,
        "completed_at": now_utc().isoformat(),
        "opengov_ingest": og_ingest,
        "sam_live_probe": sam_probe,
        "candidates_selected": len(candidates),
        "package_status_counts": dict(status_c),
        "packages_with_docs": sum(1 for r in package_results if int(r.get("docs") or 0) > 0),
        "package_samples": package_results[:25],
        "opengov_public_docs_batch": {
            "attempted": (og_batch or {}).get("attempted"),
            "valid_packages": (og_batch or {}).get("valid_packages"),
            "partial_packages": (og_batch or {}).get("partial_packages"),
            "valid_or_partial": (og_batch or {}).get("valid_or_partial"),
            "line_item_ready": (og_batch or {}).get("line_item_ready"),
            "error": (og_batch or {}).get("error"),
        }
        if og_batch
        else None,
        "l23_package_attach": attach,
        "product_identity": {
            "packages_tested": ((product_identity or {}).get("PACKAGE_LEVEL") or {}).get("Packages tested"),
            "researchable_rate": ((product_identity or {}).get("PACKAGE_LEVEL") or {}).get(
                "Commercially researchable line rate"
            ),
            "error": (product_identity or {}).get("error"),
        }
        if product_identity
        else None,
        "commercial_decision_scan": {
            "examined": (commercial or {}).get("examined"),
            "counts": (commercial or {}).get("counts"),
            "verify_blocker_counts": (commercial or {}).get("verify_blocker_counts"),
            "proven_ids": [
                o.get("opportunity_id") for o in ((commercial or {}).get("proven_opportunities") or [])[:20]
            ],
            "error": (commercial or {}).get("error"),
        }
        if commercial
        else None,
        "inventory_snapshot": {
            "live_pipeline_stage_counts": dict(stage_counts),
            "product_title_hits_non_bidnet": product_only,
            "non_bidnet_with_package_docs": with_docs,
            "non_bidnet_with_line_hints": with_lines,
        },
        "note": (
            "Live bridge: OpenGov/SAM/Socrata discovery → public package recovery → "
            "product identity → verified commercial decision. BidNet abandoned. "
            "No invented specs. SAM ≤1 production call (reserve preserved)."
        ),
    }
    try:
        from m3_data_root import data_path

        path = data_path("m3_live_contract_pipeline_bridge_last_report.json")
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        report["persisted_path"] = str(path)
    except Exception:
        log.exception("persist live pipeline bridge failed")
    return report
