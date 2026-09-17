"""Procurement Document Discovery Adapters + Attachment Recovery Engine.

Given a discovered opportunity, locate and preserve publicly available
authoritative procurement documents. Research only — no auth bypass,
CAPTCHA defeat, account creation, or aggressive scraping.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from copy import deepcopy
from typing import Any

from application_clock import now_utc
from portal_document_resolver import classify_portal_family, resolve_portal_documents

log = logging.getLogger("govtracker.m3_document_discovery")

DISCOVERY_INDEX_KEY = "m3_document_discovery_v1"

# Access / discovery statuses (Phase 1)
DOCUMENTS_FOUND = "DOCUMENTS_FOUND"
NO_DOCUMENTS_AVAILABLE = "NO_DOCUMENTS_AVAILABLE"
AUTH_REQUIRED = "AUTH_REQUIRED"
REGISTRATION_REQUIRED = "REGISTRATION_REQUIRED"
ACCESS_BLOCKED = "ACCESS_BLOCKED"
PORTAL_ERROR = "PORTAL_ERROR"
UNKNOWN = "UNKNOWN"

# Recovery queues (Phase 9)
Q_DOCUMENTS_FOUND_PENDING_PROCESSING = "DOCUMENTS_FOUND_PENDING_PROCESSING"
Q_NO_DOCUMENTS_AVAILABLE = "NO_DOCUMENTS_AVAILABLE"
Q_AUTH_REQUIRED = "AUTH_REQUIRED"
Q_ACCESS_BLOCKED = "ACCESS_BLOCKED"
Q_PORTAL_RESEARCH_REQUIRED = "PORTAL_RESEARCH_REQUIRED"

# Evidence provenance
FOUND_DURING_DISCOVERY = "FOUND_DURING_DISCOVERY"
RECOVERED_LATER = "RECOVERED_LATER"
PROCESSED = "PROCESSED"
FAILED_PROCESSING = "FAILED_PROCESSING"

VA_ALLOWED_ACTIONS = frozenset(
    {
        "REVIEW_MISSING_DOCUMENTS",
        "VERIFY_DOCUMENT_AVAILABILITY",
        "ATTACH_PERMITTED_FILE",
        "UPDATE_STATUS",
        "ADD_NOTES",
        "ATTACH_EVIDENCE",
        "NOTE",
        "ESCALATE",
        "MARK_RECOVERY_ATTEMPTED",
    }
)
VA_FORBIDDEN_ACTIONS = frozenset(
    {
        "BYPASS_ACCESS",
        "CREATE_ACCOUNT",
        "CONTACT_VENDOR",
        "SUBMIT_BID",
        "CHANGE_SCORING",
        "APPROVE_DEAL",
        "SPEND_MONEY",
    }
)

EXPECTED_DOC_TYPES = (
    "solicitation",
    "attachment",
    "amendment",
    "pricing_schedule",
    "spreadsheet",
    "bom",
    "technical_specification",
    "drawing",
    "product_list",
    "award_notice",
    "historical_award_document",
)


def _utc() -> str:
    return now_utc().isoformat()


def _known(v: Any) -> bool:
    return v not in {None, "", "UNKNOWN", "unknown"}


def load_discovery_index() -> dict[str, Any]:
    try:
        from models import AppSetting, session_scope

        with session_scope() as session:
            row = session.get(AppSetting, DISCOVERY_INDEX_KEY)
            if row and isinstance(row.value, dict):
                return deepcopy(row.value)
    except Exception:
        log.debug("document discovery index load failed", exc_info=True)
    return {"kind": "M3DocumentDiscoveryIndex", "by_id": {}, "updated_at": None}


def save_discovery_index(index: dict[str, Any]) -> None:
    index = deepcopy(index)
    index["kind"] = "M3DocumentDiscoveryIndex"
    index["updated_at"] = _utc()
    try:
        from models import AppSetting, session_scope

        with session_scope() as session:
            row = session.get(AppSetting, DISCOVERY_INDEX_KEY)
            if row is None:
                session.add(AppSetting(key=DISCOVERY_INDEX_KEY, value=index))
            else:
                row.value = index
    except Exception:
        log.debug("document discovery index save failed", exc_info=True)


def normalize_access_status(portal_result: dict[str, Any]) -> str:
    explicit = str(
        portal_result.get("access_status")
        or portal_result.get("status")
        or portal_result.get("access_state")
        or ""
    ).upper()
    if explicit in {
        DOCUMENTS_FOUND,
        NO_DOCUMENTS_AVAILABLE,
        AUTH_REQUIRED,
        REGISTRATION_REQUIRED,
        ACCESS_BLOCKED,
        PORTAL_ERROR,
        UNKNOWN,
    }:
        return explicit
    if explicit in {"DOCUMENT_BYTES_RECOVERED", "PARTIAL_PACKAGE", "PACKAGE_COMPLETE"}:
        return DOCUMENTS_FOUND
    if explicit in {"BOT_PROTECTED", "HTTP_403"}:
        return ACCESS_BLOCKED
    if explicit in {"AUTH_GATED", "LOGIN_REQUIRED"}:
        return AUTH_REQUIRED
    docs = portal_result.get("documents") or []
    if docs:
        return DOCUMENTS_FOUND
    failure = str(portal_result.get("failure") or "").upper()
    if "REGISTRATION" in failure:
        return REGISTRATION_REQUIRED
    if "LOGIN" in failure or "AUTH" in failure or "SESSION" in failure:
        return AUTH_REQUIRED
    if "BOT" in failure or "403" in failure or "BLOCK" in failure:
        return ACCESS_BLOCKED
    if failure and portal_result.get("ok") is False:
        if "404" in failure or "NOT_FOUND" in failure or "UNRESOLVED" in failure:
            return NO_DOCUMENTS_AVAILABLE
        return PORTAL_ERROR
    if portal_result.get("ok") is False:
        return NO_DOCUMENTS_AVAILABLE
    return UNKNOWN


def confidence_for_result(*, status: str, documents: list[dict[str, Any]], family: str) -> str:
    with_bytes = sum(1 for d in documents if isinstance(d, dict) and d.get("bytes_recovered"))
    if status == DOCUMENTS_FOUND and with_bytes >= 2:
        return "HIGH"
    if status == DOCUMENTS_FOUND and (with_bytes or documents):
        return "MEDIUM"
    if status in {AUTH_REQUIRED, REGISTRATION_REQUIRED, ACCESS_BLOCKED}:
        return "HIGH"  # confident about the blocker classification
    if status == NO_DOCUMENTS_AVAILABLE and family != "GENERIC":
        return "MEDIUM"
    if status == PORTAL_ERROR:
        return "LOW"
    return "LOW"


def _track_document(doc: dict[str, Any], *, source: str, provenance: str) -> dict[str, Any]:
    name = str(doc.get("name") or doc.get("title") or doc.get("filename") or "document")
    url = str(doc.get("url") or doc.get("download_url") or "UNKNOWN")
    dtype = str(doc.get("document_type") or doc.get("type") or "unknown").lower()
    return {
        "Name": name[:180],
        "URL": url,
        "Type": dtype,
        "Source": source or str(doc.get("source") or "UNKNOWN"),
        "Retrieved_date": doc.get("retrieved_at") or _utc(),
        "Status": "RECOVERED" if doc.get("bytes_recovered") else str(doc.get("status") or "LINKED"),
        "bytes_recovered": bool(doc.get("bytes_recovered")),
        "Provenance": provenance,
        "attachment_id": doc.get("attachment_id"),
        "format": doc.get("format"),
    }


def build_document_discovery_result(
    row: dict[str, Any],
    portal_result: dict[str, Any] | None = None,
    *,
    found_during_discovery: bool = False,
) -> dict[str, Any]:
    """Phase 1 — DOCUMENT_DISCOVERY_RESULT."""
    family = (portal_result or {}).get("family") or classify_portal_family(row)
    portal_result = portal_result or {}
    status = normalize_access_status(portal_result) if portal_result else UNKNOWN
    docs_raw = list(portal_result.get("documents") or [])
    provenance = FOUND_DURING_DISCOVERY if found_during_discovery else RECOVERED_LATER
    tracked = [_track_document(d, source=family, provenance=provenance) for d in docs_raw if isinstance(d, dict)]

    present_types = {str(t.get("Type") or "").lower() for t in tracked}
    missing = list(portal_result.get("documents_missing") or [])
    if not missing and status == DOCUMENTS_FOUND:
        missing = [t for t in ("pricing_schedule", "bom", "solicitation") if t not in present_types]
    elif not missing and status != DOCUMENTS_FOUND:
        missing = list(EXPECTED_DOC_TYPES[:5])

    attempts = list(portal_result.get("attempts") or [])
    conf = confidence_for_result(status=status, documents=docs_raw, family=str(family))

    return {
        "kind": "DOCUMENT_DISCOVERY_RESULT",
        "Opportunity_ID": row.get("canonical_id") or row.get("notice_id") or row.get("external_id") or "UNKNOWN",
        "Source": row.get("source_id") or row.get("source") or "UNKNOWN",
        "Portal_family": family,
        "Original_URL": row.get("detail_url") or row.get("source_url") or row.get("url") or "UNKNOWN",
        "Document_discovery_attempts": attempts,
        "Documents_found": tracked,
        "Documents_missing": missing,
        "Access_status": status,
        "Timestamp": _utc(),
        "Confidence": conf,
        "documents_raw": docs_raw,
        "line_items": portal_result.get("line_items") or [],
        "retrieval_method": portal_result.get("retrieval_method"),
        "failure": portal_result.get("failure"),
        "bytes_pending": bool(portal_result.get("bytes_pending")),
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def assign_recovery_queue(result: dict[str, Any]) -> dict[str, Any]:
    status = str(result.get("Access_status") or UNKNOWN)
    docs = result.get("Documents_found") or []
    with_bytes = [d for d in docs if isinstance(d, dict) and d.get("bytes_recovered")]
    pending = bool(result.get("bytes_pending")) or (docs and not with_bytes)

    if status == AUTH_REQUIRED or status == REGISTRATION_REQUIRED:
        queue = Q_AUTH_REQUIRED
        problem = f"Official portal requires {'registration' if status == REGISTRATION_REQUIRED else 'authentication'}"
        next_action = "VA: verify public availability; attach permitted files if already obtained legally"
    elif status == ACCESS_BLOCKED:
        queue = Q_ACCESS_BLOCKED
        problem = "Portal blocked automated public retrieval (bot wall / HTTP block)"
        next_action = "Do not bypass; classify blocker and continue other opportunities"
    elif status == DOCUMENTS_FOUND and (with_bytes or pending):
        queue = Q_DOCUMENTS_FOUND_PENDING_PROCESSING
        problem = "Documents located; processing / extraction pending" if pending or with_bytes else "Documents linked"
        next_action = "Run document extraction → product identity → BOM → commercial completeness"
    elif status in {NO_DOCUMENTS_AVAILABLE, PORTAL_ERROR, UNKNOWN}:
        if status == PORTAL_ERROR or result.get("Portal_family") == "GENERIC":
            queue = Q_PORTAL_RESEARCH_REQUIRED
            problem = "Portal family recovery inconclusive or errored"
            next_action = "Research alternate official document route; do not use unverified third parties"
        else:
            queue = Q_NO_DOCUMENTS_AVAILABLE
            problem = "No public procurement package documents found"
            next_action = "Classify as unavailable; continue pipeline without fabricating package"
    else:
        queue = Q_PORTAL_RESEARCH_REQUIRED
        problem = f"Unhandled access status: {status}"
        next_action = "Investigate portal adapter coverage"

    return {
        "queue": queue,
        "Opportunity": result.get("Opportunity_ID"),
        "Source": result.get("Source"),
        "Portal_family": result.get("Portal_family"),
        "Problem": problem,
        "Last_attempt": result.get("Timestamp"),
        "Next_action": next_action,
        "Access_status": status,
        "Documents_found_count": len(docs),
        "Documents_with_bytes": len(with_bytes),
    }


def apply_discovery_to_row(row: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Merge discovered documents onto pipeline row + evidence provenance."""
    working = deepcopy(row)
    docs = list(working.get("documents") or []) if isinstance(working.get("documents"), list) else []
    existing_urls = {str(d.get("url") or "").split("?")[0].lower() for d in docs if isinstance(d, dict)}

    recovered_count = 0
    for raw in result.get("documents_raw") or []:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or raw.get("download_url") or "").split("?")[0].lower()
        if url and url in existing_urls:
            # Prefer bytes_recovered version
            for i, d in enumerate(docs):
                if not isinstance(d, dict):
                    continue
                if str(d.get("url") or "").split("?")[0].lower() == url:
                    if raw.get("bytes_recovered") and not d.get("bytes_recovered"):
                        docs[i] = {**d, **raw, "evidence_provenance": RECOVERED_LATER}
                        recovered_count += 1
                    break
            continue
        stamped = {
            **raw,
            "evidence_provenance": RECOVERED_LATER,
            "found_during_discovery": False,
        }
        docs.append(stamped)
        if url:
            existing_urls.add(url)
        if raw.get("bytes_recovered"):
            recovered_count += 1

    working["documents"] = docs
    if result.get("line_items") and not (working.get("line_items") or working.get("bom")):
        working["line_items"] = result["line_items"]
        working["bom"] = result["line_items"]

    status = result.get("Access_status")
    if status == AUTH_REQUIRED:
        working["source_access_state"] = "AUTH_REQUIRED"
        working["package_access"] = "AUTH_GATED"
        working["auth_required_for_spec"] = True
    elif status == REGISTRATION_REQUIRED:
        working["source_access_state"] = "REGISTRATION_REQUIRED"
        working["package_access"] = "REGISTRATION_REQUIRED"
        working["auth_required_for_spec"] = True
    elif status == ACCESS_BLOCKED:
        working["source_access_state"] = "BOT_PROTECTED"
        working["package_access"] = "BOT_PROTECTED"

    working["document_discovery"] = {
        k: result.get(k)
        for k in (
            "kind",
            "Opportunity_ID",
            "Source",
            "Portal_family",
            "Original_URL",
            "Access_status",
            "Timestamp",
            "Confidence",
            "Documents_missing",
            "retrieval_method",
            "failure",
        )
    }
    working["document_discovery"]["Documents_found_count"] = len(result.get("Documents_found") or [])
    working["document_discovery"]["recovered_later_count"] = recovered_count

    # Evidence chain refresh
    try:
        from m3_evidence_chain import ensure_evidence_chain_on_row

        ensure_evidence_chain_on_row(working)
        chain = working.get("evidence_chain")
        if isinstance(chain, dict):
            chain["document_recovery"] = {
                "status": status,
                "recovered_later": recovered_count,
                "portal_family": result.get("Portal_family"),
                "at": _utc(),
            }
            # Mark attachment steps
            for att in chain.get("attachment_traceability") or []:
                if isinstance(att, dict) and att.get("bytes_recovered"):
                    att["recovery_state"] = PROCESSED if att.get("has_text") else RECOVERED_LATER
    except Exception:
        log.debug("evidence chain update after document discovery failed", exc_info=True)

    return working


def run_document_processing_handoff(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 7 — auto pass recovered docs through extraction / identity / BOM / completeness."""
    from m3_document_evidence import build_document_evidence_intelligence
    from m3_procurement_package import build_procurement_package

    package = None
    evidence = None
    errors: list[str] = []
    try:
        evidence = build_document_evidence_intelligence(row, run_recovery=False, allow_paid=False)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"evidence:{exc}")
    try:
        package = build_procurement_package(row, allow_paid_web=False)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"package:{exc}")

    moved_pricing = False
    moved_economics = False
    if isinstance(evidence, dict):
        moved_pricing = (evidence.get("PROCUREMENT_COMPLETENESS") or {}).get(
            "PROCUREMENT_COMPLETENESS"
        ) in {"READY_FOR_PRICING", "COMPLETE_PURCHASE_PACKAGE", "READY_FOR_SUPPLIER_RESEARCH"}
        handoff = evidence.get("ECONOMICS_HANDOFF") or {}
        moved_economics = bool(handoff.get("moved_into_economics") or handoff.get("can_handoff"))
    if isinstance(package, dict) and not moved_economics:
        eh = package.get("ECONOMICS_HANDOFF") or {}
        moved_economics = bool(eh.get("can_calculate") or eh.get("moved_into_economics"))

    return {
        "kind": "DOCUMENT_PROCESSING_HANDOFF",
        "ok": not errors,
        "errors": errors,
        "evidence_completeness": (
            (evidence or {}).get("PROCUREMENT_COMPLETENESS") or {}
        ).get("PROCUREMENT_COMPLETENESS")
        if evidence
        else None,
        "package_readiness": ((package or {}).get("RESEARCH_READINESS") or {}).get("RESEARCH_QUEUE")
        if package
        else None,
        "moved_toward_pricing": moved_pricing,
        "moved_toward_economics": moved_economics,
        "product_identity": (package or {}).get("PRODUCT_IDENTITY") if package else None,
        "commercial_completeness": (package or {}).get("COMMERCIAL_COMPLETENESS") if package else None,
    }


def discover_documents_for_opportunity(
    row: dict[str, Any],
    *,
    run_processing: bool = True,
) -> dict[str, Any]:
    """Run portal family adapter + build DOCUMENT_DISCOVERY_RESULT + optional processing."""
    family = classify_portal_family(row)
    try:
        portal_result = resolve_portal_documents(row)
    except Exception as exc:  # noqa: BLE001
        log.exception("document discovery adapter failed for %s", row.get("canonical_id"))
        portal_result = {
            "family": family,
            "ok": False,
            "failure": str(exc)[:300],
            "access_status": PORTAL_ERROR,
            "documents": [],
            "documents_missing": list(EXPECTED_DOC_TYPES[:5]),
            "attempts": [{"step": "ADAPTER", "error": str(exc)[:300], "at": _utc()}],
            "line_items": [],
        }

    result = build_document_discovery_result(row, portal_result)
    queue_card = assign_recovery_queue(result)
    working = apply_discovery_to_row(row, result)

    processing = None
    if run_processing and result.get("Access_status") == DOCUMENTS_FOUND:
        # Prefer row with merged docs
        processing = run_document_processing_handoff(working)
        if processing.get("moved_toward_pricing") or processing.get("ok"):
            for d in working.get("documents") or []:
                if isinstance(d, dict) and d.get("bytes_recovered"):
                    d["processing_status"] = PROCESSED
        elif processing.get("errors"):
            for d in working.get("documents") or []:
                if isinstance(d, dict) and d.get("bytes_recovered"):
                    d["processing_status"] = FAILED_PROCESSING

    return {
        "kind": "M3DocumentDiscoveryRunItem",
        "DOCUMENT_DISCOVERY_RESULT": result,
        "RECOVERY_QUEUE": queue_card,
        "PROCESSING_HANDOFF": processing,
        "working_row_patch": {
            "documents": working.get("documents"),
            "line_items": working.get("line_items"),
            "bom": working.get("bom"),
            "source_access_state": working.get("source_access_state"),
            "package_access": working.get("package_access"),
            "auth_required_for_spec": working.get("auth_required_for_spec"),
            "document_discovery": working.get("document_discovery"),
            "evidence_chain": working.get("evidence_chain"),
        },
        "VA": {
            "allowed_actions": sorted(VA_ALLOWED_ACTIONS),
            "forbidden_actions": sorted(VA_FORBIDDEN_ACTIONS),
            "role": "DOCUMENT_RECOVERY_OPERATOR",
            "may_bypass_access": False,
            "may_create_accounts": False,
            "may_contact_vendors": False,
            "may_bid": False,
            "may_change_scoring": False,
        },
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def analyze_document_discovery_top(
    store: Any,
    *,
    limit: int = 25,
    run_processing: bool = True,
    persist: bool = True,
) -> dict[str, Any]:
    """Batch document discovery across TOP pipeline opportunities."""
    rows = []
    try:
        rows = list(store.all()) if hasattr(store, "all") else []
    except Exception:
        rows = []

    # Prefer rows that look document-starved
    def _need_score(r: dict[str, Any]) -> tuple[int, str]:
        docs = r.get("documents") if isinstance(r.get("documents"), list) else []
        with_bytes = sum(1 for d in docs if isinstance(d, dict) and d.get("bytes_recovered"))
        has_url = 1 if (r.get("detail_url") or r.get("source_url")) else 0
        starved = 0 if with_bytes else 2
        return (-(starved + has_url), str(r.get("canonical_id") or ""))

    ranked = sorted([r for r in rows if isinstance(r, dict)], key=_need_score)[: max(1, min(limit, 50))]

    items: list[dict[str, Any]] = []
    queues: dict[str, list] = defaultdict(list)
    by_family: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    docs_found = 0
    attachments_found = 0
    pricing_found = 0
    bom_found = 0
    award_found = 0
    complete = partial = none_avail = blocked = 0
    moved_pricing = moved_economics = still_blocked = 0
    improved: list[dict[str, Any]] = []
    index = load_discovery_index() if persist else {"by_id": {}}
    by_id = index.setdefault("by_id", {})

    for row in ranked:
        item = discover_documents_for_opportunity(row, run_processing=run_processing)
        result = item["DOCUMENT_DISCOVERY_RESULT"]
        qcard = item["RECOVERY_QUEUE"]
        items.append(item)
        queues[qcard["queue"]].append(
            {
                "Opportunity": row.get("title") or result.get("Opportunity_ID"),
                "canonical_id": row.get("canonical_id"),
                "Source": result.get("Source"),
                "Portal_family": result.get("Portal_family"),
                "Problem": qcard["Problem"],
                "Last_attempt": qcard["Last_attempt"],
                "Next_action": qcard["Next_action"],
                "Access_status": result.get("Access_status"),
                "Documents_found": len(result.get("Documents_found") or []),
            }
        )
        family = str(result.get("Portal_family") or "OTHER")
        by_family[family] += 1
        status = str(result.get("Access_status") or UNKNOWN)
        status_counts[status] += 1

        tracked = result.get("Documents_found") or []
        docs_found += len(tracked)
        for t in tracked:
            if not isinstance(t, dict):
                continue
            dtype = str(t.get("Type") or "").lower()
            name = str(t.get("Name") or "").lower()
            url = str(t.get("URL") or "").lower()
            hay = f"{dtype} {name} {url}"
            counted_attach = False
            if dtype in {
                "attachment",
                "solicitation",
                "amendment",
                "technical_specification",
                "drawing",
                "product_list",
                "contract_file",
                "unknown",
                "",
            } or any(x in hay for x in (".pdf", ".doc", "solicitation", "attachment", "spec")):
                attachments_found += 1
                counted_attach = True
            if dtype == "pricing_schedule" or "pric" in hay or ("schedule" in hay and ".xls" in hay):
                pricing_found += 1
            if dtype == "bom" or "bom" in hay or "bill of material" in hay:
                bom_found += 1
            if "award" in dtype or "award" in name:
                award_found += 1
            if not counted_attach and (
                dtype == "spreadsheet" or url.endswith((".xlsx", ".xls", ".csv", ".zip"))
            ):
                attachments_found += 1

        with_bytes = sum(1 for t in tracked if isinstance(t, dict) and t.get("bytes_recovered"))
        if status in {AUTH_REQUIRED, REGISTRATION_REQUIRED, ACCESS_BLOCKED}:
            blocked += 1
            still_blocked += 1
        elif status == DOCUMENTS_FOUND and with_bytes >= 2:
            complete += 1
        elif status == DOCUMENTS_FOUND:
            partial += 1
        else:
            none_avail += 1

        proc = item.get("PROCESSING_HANDOFF") or {}
        if proc.get("moved_toward_pricing"):
            moved_pricing += 1
        if proc.get("moved_toward_economics"):
            moved_economics += 1

        if with_bytes or status == DOCUMENTS_FOUND:
            product = ((proc.get("product_identity") or {}) if isinstance(proc, dict) else {}) or {}
            improved.append(
                {
                    "Opportunity": row.get("title"),
                    "canonical_id": row.get("canonical_id"),
                    "Documents_recovered": with_bytes,
                    "Product_information": product.get("Manufacturer")
                    or product.get("Model")
                    or product.get("match")
                    or "UNKNOWN",
                    "Quantity": (row.get("line_items") or [{}])[0].get("quantity")
                    if isinstance(row.get("line_items"), list) and row.get("line_items")
                    else "UNKNOWN",
                    "Value": row.get("estimated_value") or row.get("award_amount") or "UNKNOWN",
                    "New_readiness": proc.get("evidence_completeness") or proc.get("package_readiness") or status,
                    "Portal_family": family,
                }
            )

        # Persist patch onto store row
        cid = row.get("canonical_id")
        if cid and hasattr(store, "_rows"):
            existing = store._rows.get(cid) or dict(row)
            patch = item.get("working_row_patch") or {}
            for k, v in patch.items():
                if v is not None:
                    existing[k] = v
            existing["document_discovery_intelligence"] = {
                "DOCUMENT_DISCOVERY_RESULT": {
                    k: result.get(k) for k in result if k != "documents_raw"
                },
                "RECOVERY_QUEUE": qcard,
                "PROCESSING_HANDOFF": proc,
            }
            store._rows[cid] = existing
        elif cid and hasattr(store, "get"):
            existing = store.get(cid) or dict(row)
            patch = item.get("working_row_patch") or {}
            existing.update({k: v for k, v in patch.items() if v is not None})

        if cid:
            by_id[str(cid)] = {
                "DOCUMENT_DISCOVERY_RESULT": {
                    k: result.get(k)
                    for k in result
                    if k not in {"documents_raw"}
                },
                "RECOVERY_QUEUE": qcard,
                "PROCESSING_HANDOFF": {
                    k: proc.get(k)
                    for k in (
                        "kind",
                        "ok",
                        "moved_toward_pricing",
                        "moved_toward_economics",
                        "evidence_completeness",
                        "package_readiness",
                        "errors",
                    )
                }
                if proc
                else None,
                "updated_at": _utc(),
            }

    if persist:
        try:
            if hasattr(store, "save"):
                store.save()
        except Exception:
            pass
        save_discovery_index(index)

    def _family_bucket(name: str) -> int:
        return int(by_family.get(name) or 0)

    return {
        "kind": "M3DocumentDiscoveryRun",
        "analyzed": len(items),
        "DOCUMENT_RECOVERY": {
            "Opportunities_analyzed": len(items),
            "Documents_found": docs_found,
            "Attachments_found": attachments_found,
            "Pricing_schedules_found": pricing_found,
            "BOM_files_found": bom_found,
            "Award_documents_found": award_found,
        },
        "BY_SOURCE_FAMILY": {
            "SAM": _family_bucket("SAM") + _family_bucket("DLA"),
            "JAGGAER": _family_bucket("JAGGAER")
            + _family_bucket("IOWA")
            + _family_bucket("MONTANA"),
            "BONFIRE": _family_bucket("BONFIRE"),
            "BIDNET": _family_bucket("BIDNET"),
            "OTHER": sum(
                v
                for k, v in by_family.items()
                if k
                not in {
                    "SAM",
                    "DLA",
                    "JAGGAER",
                    "IOWA",
                    "MONTANA",
                    "BONFIRE",
                    "BIDNET",
                }
            ),
            "detail": dict(by_family),
        },
        "RECOVERY_RESULTS": {
            "Complete_packages": complete,
            "Partial_packages": partial,
            "No_documents_available": none_avail,
            "Blocked": blocked,
            "status_counts": dict(status_counts),
        },
        "ECONOMICS_IMPACT": {
            "Moved_toward_pricing": moved_pricing,
            "Moved_toward_economics": moved_economics,
            "Still_blocked": still_blocked,
        },
        "TOP_IMPROVED_OPPORTUNITIES": improved[:10],
        "queues": {k: v[:25] for k, v in queues.items()},
        "items": items,
        "LIMITATIONS": {
            "Remaining_blockers": [
                q
                for q in (Q_AUTH_REQUIRED, Q_ACCESS_BLOCKED, Q_PORTAL_RESEARCH_REQUIRED)
                if queues.get(q)
            ],
            "Access_issues": status_counts.get(AUTH_REQUIRED, 0)
            + status_counts.get(REGISTRATION_REQUIRED, 0)
            + status_counts.get(ACCESS_BLOCKED, 0),
            "Missing_sources": status_counts.get(NO_DOCUMENTS_AVAILABLE, 0),
        },
        "COST": {"Paid_spend": 0},
        "SAFETY": {"Outreach_actions": 0, "Auth_bypass_attempts": 0},
        "NEXT_STATE": "PROCUREMENT_DOCUMENT_RECOVERY_OPERATIONAL",
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def build_va_document_queues(limit: int = 25) -> dict[str, Any]:
    idx = load_discovery_index()
    by_id = idx.get("by_id") if isinstance(idx.get("by_id"), dict) else {}
    queues: dict[str, list] = defaultdict(list)
    for cid, pkg in by_id.items():
        if not isinstance(pkg, dict):
            continue
        q = pkg.get("RECOVERY_QUEUE") or {}
        queues[q.get("queue") or Q_PORTAL_RESEARCH_REQUIRED].append(
            {
                "Opportunity": q.get("Opportunity") or cid,
                "canonical_id": cid,
                "Source": q.get("Source"),
                "Problem": q.get("Problem"),
                "Last_attempt": q.get("Last_attempt"),
                "Next_action": q.get("Next_action"),
                "Access_status": (pkg.get("DOCUMENT_DISCOVERY_RESULT") or {}).get("Access_status"),
            }
        )
    for q in queues:
        queues[q] = queues[q][: max(1, min(limit, 50))]
    return {
        "kind": "M3DocumentRecoveryQueues",
        "queues": dict(queues),
        "VA_allowed_actions": sorted(VA_ALLOWED_ACTIONS),
        "VA_forbidden_actions": sorted(VA_FORBIDDEN_ACTIONS),
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def apply_va_document_update(
    store: Any,
    canonical_id: str,
    *,
    action: str,
    note: str | None = None,
    status: str | None = None,
    attached_document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Phase 10 — VA document recovery ops (no access bypass / accounts / outreach / scoring)."""
    action_u = str(action or "").upper().strip()
    if action_u not in VA_ALLOWED_ACTIONS:
        return {
            "ok": False,
            "error": "action_not_allowed",
            "action": action_u,
            "VA_allowed_actions": sorted(VA_ALLOWED_ACTIONS),
            "VA_forbidden_actions": sorted(VA_FORBIDDEN_ACTIONS),
        }
    if action_u in VA_FORBIDDEN_ACTIONS:
        return {"ok": False, "error": "forbidden_action", "action": action_u}

    row = store.get(canonical_id) if hasattr(store, "get") else None
    if not isinstance(row, dict):
        return {"ok": False, "error": "opportunity_not_found", "canonical_id": canonical_id}

    idx = load_discovery_index()
    by_id = idx.setdefault("by_id", {})
    pkg = by_id.get(canonical_id) if isinstance(by_id.get(canonical_id), dict) else {}
    notes = list((pkg.get("VA_notes") or []))

    if action_u in {"ADD_NOTES", "NOTE"} and note:
        notes.append({"at": _utc(), "note": str(note)[:2000], "action": action_u})
    if action_u == "UPDATE_STATUS" and status:
        ddr = pkg.get("DOCUMENT_DISCOVERY_RESULT") if isinstance(pkg.get("DOCUMENT_DISCOVERY_RESULT"), dict) else {}
        ddr["Access_status"] = str(status).upper()
        ddr["Timestamp"] = _utc()
        pkg["DOCUMENT_DISCOVERY_RESULT"] = ddr
        pkg["RECOVERY_QUEUE"] = assign_recovery_queue(
            {
                **ddr,
                "Documents_found": ddr.get("Documents_found") or [],
                "bytes_pending": ddr.get("bytes_pending"),
                "Portal_family": ddr.get("Portal_family"),
            }
        )
    if action_u in {"ATTACH_PERMITTED_FILE", "ATTACH_EVIDENCE"} and isinstance(attached_document, dict):
        docs = list(row.get("documents") or [])
        docs.append(
            {
                **attached_document,
                "source": "va_permitted_attach",
                "authority": "authoritative",
                "evidence_provenance": RECOVERED_LATER,
                "retrieved_at": _utc(),
                "bytes_recovered": bool(
                    attached_document.get("bytes_recovered")
                    or attached_document.get("extracted_text")
                    or attached_document.get("url")
                ),
            }
        )
        row["documents"] = docs
        notes.append({"at": _utc(), "note": "VA attached permitted file", "action": action_u})
    if action_u == "MARK_RECOVERY_ATTEMPTED":
        notes.append({"at": _utc(), "note": note or "VA marked recovery attempted", "action": action_u})
    if action_u in {"REVIEW_MISSING_DOCUMENTS", "VERIFY_DOCUMENT_AVAILABILITY", "ESCALATE"}:
        notes.append({"at": _utc(), "note": note or action_u, "action": action_u})

    pkg["VA_notes"] = notes[-20:]
    pkg["updated_at"] = _utc()
    by_id[canonical_id] = pkg
    save_discovery_index(idx)

    if hasattr(store, "_rows") and canonical_id in getattr(store, "_rows", {}):
        store._rows[canonical_id] = row
    if hasattr(store, "save"):
        try:
            store.save()
        except Exception:
            pass

    return {
        "ok": True,
        "canonical_id": canonical_id,
        "action": action_u,
        "VA_notes": pkg.get("VA_notes"),
        "DOCUMENT_DISCOVERY_RESULT": pkg.get("DOCUMENT_DISCOVERY_RESULT"),
        "RECOVERY_QUEUE": pkg.get("RECOVERY_QUEUE"),
        "DEVELOPMENT_NO_OUTREACH": True,
    }
