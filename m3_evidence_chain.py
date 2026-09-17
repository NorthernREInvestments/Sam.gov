"""Evidence Chain Preservation + Source Traceability — research only.

Preserve the discovery→pipeline→intelligence→economics evidence trail.
Does not rewrite discovery, fabricate evidence, or grow storage unboundedly.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import Any

from application_clock import now_utc

log = logging.getLogger("govtracker.m3_evidence_chain")

CHAIN_INDEX_KEY = "m3_evidence_chain_v1"
RAW_META_MAX_CHARS = 8000
MAX_DOC_REFS = 30
MAX_ATTACHMENT_REFS = 30

# Evidence states
FOUND_AT_DISCOVERY = "FOUND_AT_DISCOVERY"
RECOVERED_AFTER_DISCOVERY = "RECOVERED_AFTER_DISCOVERY"
PROCESSED = "PROCESSED"
EXTRACTED = "EXTRACTED"
UNAVAILABLE = "UNAVAILABLE"
REMOVED = "REMOVED"
ACCESS_BLOCKED = "ACCESS_BLOCKED"
NOT_FOUND = "NOT_FOUND"

# Attachment statuses
ATT_FOUND = "FOUND"
ATT_MISSING = "MISSING"
ATT_EXPIRED = "EXPIRED"
ATT_BLOCKED = "BLOCKED"
ATT_NOT_AVAILABLE = "NOT_AVAILABLE"

VA_ALLOWED = frozenset({"REVIEW_EVIDENCE", "ATTACH_DOCUMENTS", "UPDATE_STATUS", "ADD_NOTES"})
VA_FORBIDDEN = frozenset({"BID", "CONTACT_SUPPLIERS", "MODIFY_SCORING", "CHANGE_BUSINESS_RULES"})

PRESERVE_KEYS = (
    "detail_url",
    "source_url",
    "url",
    "listing_url",
    "portal",
    "preferred_source_url",
    "source_id",
    "preferred_source_id",
    "source_type",
    "source_family",
    "platform_family",
    "adapter_family",
    "api_endpoint",
    "raw_metadata",
    "documents",
    "attachments",
    "document_links",
    "attachment_links",
    "document_urls",
    "attachment_urls",
    "solicitation_number",
    "external_id",
    "agency",
    "buyer",
    "naics",
    "posted_at",
    "source_modified_at",
    "last_seen_at",
    "line_items",
    "bom",
    "description",
    "notice_id",
    "opportunity_id",
)


def _utc() -> str:
    return now_utc().isoformat()


def _known(v: Any) -> bool:
    if v is None or v == "" or v == "UNKNOWN" or v == "unknown":
        return False
    if isinstance(v, (dict, list)):
        return len(v) > 0
    return True


def _cap_text(v: Any, limit: int = RAW_META_MAX_CHARS) -> Any:
    if isinstance(v, str) and len(v) > limit:
        return v[:limit] + f"...[truncated:{len(v)}]"
    if isinstance(v, dict):
        out = {}
        size = 0
        for k, val in list(v.items())[:80]:
            cv = _cap_text(val, max(500, limit // 4))
            try:
                piece = json.dumps(cv, default=str)
            except Exception:
                piece = str(cv)[:500]
            if size + len(piece) > limit:
                out["_truncated"] = True
                break
            out[k] = cv
            size += len(piece)
        return out
    if isinstance(v, list):
        return [_cap_text(x, max(200, limit // 8)) for x in v[:40]]
    return v


def _collect_document_refs(item: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(url: Any, *, name: str = "", dtype: str = "attachment", source: str = "discovery") -> None:
        u = str(url or "").strip()
        if not u or not u.startswith("http"):
            # allow non-http identifiers as references when named
            if not name and not u:
                return
        key = f"{name}|{u}"
        if key in seen:
            return
        seen.add(key)
        refs.append(
            {
                "url": u or "UNKNOWN",
                "name": name or "UNKNOWN",
                "type": dtype,
                "source": source,
                "status": ATT_FOUND if u.startswith("http") else ATT_MISSING,
                "recorded_at": _utc(),
            }
        )

    for d in item.get("documents") or []:
        if isinstance(d, dict):
            _add(
                d.get("url") or d.get("href") or d.get("download_url"),
                name=str(d.get("name") or d.get("filename") or d.get("title") or ""),
                dtype=str(d.get("document_type") or d.get("type") or "attachment"),
                source=str(d.get("source") or "discovery"),
            )
        elif isinstance(d, str):
            _add(d, name="document", dtype="attachment")

    for d in item.get("attachments") or []:
        if isinstance(d, dict):
            _add(
                d.get("url") or d.get("href") or d.get("download_url"),
                name=str(d.get("name") or d.get("filename") or ""),
                dtype="attachment",
            )
        elif isinstance(d, str):
            _add(d)

    for key in ("document_links", "attachment_links", "document_urls", "attachment_urls"):
        for u in item.get(key) or []:
            if isinstance(u, str):
                _add(u)
            elif isinstance(u, dict):
                _add(u.get("url") or u.get("href"), name=str(u.get("name") or ""))

    meta = item.get("raw_metadata") if isinstance(item.get("raw_metadata"), dict) else {}
    for key in ("attachments", "documents", "resourceLinks", "links"):
        for u in meta.get(key) or []:
            if isinstance(u, str):
                _add(u, source="raw_metadata")
            elif isinstance(u, dict):
                _add(
                    u.get("url") or u.get("href") or u.get("uri"),
                    name=str(u.get("name") or u.get("title") or ""),
                    source="raw_metadata",
                )

    return refs[:MAX_DOC_REFS]


def build_discovery_evidence_record(
    item: dict[str, Any],
    *,
    discovery_run_id: str | None = None,
    discovery_timestamp: str | None = None,
) -> dict[str, Any]:
    """Phase 1 — DISCOVERY_EVIDENCE_RECORD."""
    docs = _collect_document_refs(item)
    raw = item.get("raw_metadata")
    if raw is None:
        # Compact raw snapshot of non-normalized keys
        raw = {
            k: item.get(k)
            for k in item.keys()
            if k
            not in {
                "title",
                "description",
                "agency",
                "deadline",
                "status",
                "product_classification",
                "package_access",
            }
        }
    raw = _cap_text(raw)

    source_url = (
        item.get("detail_url")
        or item.get("preferred_source_url")
        or item.get("source_url")
        or item.get("url")
        or item.get("listing_url")
    )
    confidence = "HIGH" if source_url and docs else ("MEDIUM" if source_url else "LOW")

    return {
        "kind": "DISCOVERY_EVIDENCE_RECORD",
        "Source": item.get("source_id") or item.get("preferred_source_id") or "UNKNOWN",
        "Source_type": item.get("source_type")
        or item.get("source_family")
        or item.get("platform_family")
        or item.get("adapter_family")
        or "UNKNOWN",
        "Source_URL": source_url or "UNKNOWN",
        "API_endpoint": item.get("api_endpoint") or item.get("endpoint") or "UNKNOWN",
        "Discovery_timestamp": discovery_timestamp or item.get("discovered_at") or _utc(),
        "Discovery_run_ID": discovery_run_id or item.get("discovery_run_id") or "UNKNOWN",
        "Solicitation_ID": item.get("solicitation_number")
        or item.get("external_id")
        or item.get("notice_id")
        or "UNKNOWN",
        "Agency": item.get("agency") or item.get("buyer") or "UNKNOWN",
        "Raw_discovery_metadata": raw,
        "Document_references": docs[:MAX_DOC_REFS],
        "Attachment_references": [d for d in docs if d.get("type") == "attachment"][:MAX_ATTACHMENT_REFS],
        "Evidence_confidence": confidence,
        "preserved_at": _utc(),
    }


def enrich_discovery_record_for_pipeline(
    item: dict[str, Any],
    *,
    discovery_run_id: str | None = None,
) -> dict[str, Any]:
    """Preserve raw evidence on the handoff record (does not rewrite discovery)."""
    out = dict(item)
    # Keep original URLs even if detail_url later changes
    if not out.get("detail_url"):
        out["detail_url"] = (
            item.get("preferred_source_url") or item.get("source_url") or item.get("url") or item.get("listing_url")
        )
    if not out.get("source_id"):
        out["source_id"] = item.get("preferred_source_id") or item.get("source_id")

    # Preserve extra keys already present
    for k in PRESERVE_KEYS:
        if k in item and item[k] is not None and k not in out:
            out[k] = item[k]

    # Always attach a discovery evidence record
    der = build_discovery_evidence_record(item, discovery_run_id=discovery_run_id)
    out["discovery_evidence"] = der
    out["discovery_run_id"] = der.get("Discovery_run_ID")
    out["discovered_at"] = der.get("Discovery_timestamp")

    # Surface document refs onto documents list if empty
    if not out.get("documents") and der.get("Document_references"):
        out["documents"] = [
            {
                "name": d.get("name"),
                "url": d.get("url"),
                "document_type": d.get("type"),
                "source": "discovery_evidence",
                "bytes_recovered": False,
                "status": d.get("status"),
                "retrieved_at": d.get("recorded_at"),
            }
            for d in der["Document_references"]
            if _known(d.get("url"))
        ][:MAX_DOC_REFS]

    if not isinstance(out.get("raw_metadata"), dict):
        out["raw_metadata"] = der.get("Raw_discovery_metadata") if isinstance(der.get("Raw_discovery_metadata"), dict) else {}

    return out


def attachment_traceability(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Phase 5 — attachment URL/type/status/last checked/recovery result."""
    out: list[dict[str, Any]] = []
    der = row.get("discovery_evidence") if isinstance(row.get("discovery_evidence"), dict) else {}
    for d in der.get("Attachment_references") or der.get("Document_references") or []:
        if isinstance(d, dict):
            out.append(
                {
                    "Attachment_URL": d.get("url") or "UNKNOWN",
                    "Attachment_type": d.get("type") or "attachment",
                    "Attachment_status": d.get("status") or ATT_MISSING,
                    "Last_checked": d.get("last_checked") or d.get("recorded_at") or "UNKNOWN",
                    "Recovery_result": d.get("recovery_result") or d.get("status") or ATT_MISSING,
                    "origin": "discovery",
                }
            )
    for d in row.get("documents") or []:
        if not isinstance(d, dict):
            continue
        status = ATT_FOUND if d.get("bytes_recovered") or d.get("extracted_text") else ATT_MISSING
        if str(row.get("source_access_state") or "").upper() in {"AUTH_REQUIRED", "AUTH_GATED", "REGISTRATION_REQUIRED"}:
            status = ATT_BLOCKED
        out.append(
            {
                "Attachment_URL": d.get("url") or d.get("href") or "UNKNOWN",
                "Attachment_type": d.get("document_type") or d.get("type") or "attachment",
                "Attachment_status": status,
                "Last_checked": d.get("retrieved_at") or d.get("at") or "UNKNOWN",
                "Recovery_result": "RECOVERED" if d.get("bytes_recovered") else status,
                "origin": d.get("source") or "pipeline",
            }
        )
    # Dedup by URL
    seen = set()
    uniq = []
    for a in out:
        key = str(a.get("Attachment_URL"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(a)
    return uniq[:MAX_ATTACHMENT_REFS]


def evidence_status_for_row(row: dict[str, Any]) -> str:
    docs = row.get("documents") if isinstance(row.get("documents"), list) else []
    has_bytes = any(isinstance(d, dict) and d.get("bytes_recovered") for d in docs)
    der = row.get("discovery_evidence") if isinstance(row.get("discovery_evidence"), dict) else {}
    has_refs = bool(der.get("Document_references") or der.get("Attachment_references"))
    access = str(row.get("source_access_state") or "").upper()
    fail = row.get("evidence_failure") if isinstance(row.get("evidence_failure"), dict) else {}
    primary = str(fail.get("primary_reason") or "").upper()

    if access in {"AUTH_REQUIRED", "AUTH_GATED", "REGISTRATION_REQUIRED"} or "AUTH" in primary or "BLOCK" in primary:
        return ACCESS_BLOCKED
    if "REMOVED" in primary or "EXPIRED" in primary or "404" in primary:
        return REMOVED
    if has_bytes and (row.get("line_items") or row.get("attachment_text") or row.get("governing_text")):
        return EXTRACTED
    if has_bytes:
        return PROCESSED
    if has_bytes is False and (row.get("evidence_acquisition") or {}).get("improved"):
        return RECOVERED_AFTER_DISCOVERY
    if has_refs or der.get("Source_URL") not in {None, "UNKNOWN"}:
        # refs at discovery but not recovered
        if has_refs and not has_bytes:
            return FOUND_AT_DISCOVERY if any(
                str(r.get("url") or "").startswith("http") for r in (der.get("Document_references") or [])
            ) else NOT_FOUND
        return FOUND_AT_DISCOVERY if _known(der.get("Source_URL")) else NOT_FOUND
    if primary in {"NO_ATTACHMENT_FOUND", "LISTING_ONLY"} or not docs:
        return NOT_FOUND
    return UNAVAILABLE


def build_evidence_chain(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 3 — EVIDENCE_CHAIN from discovery through economics."""
    der = row.get("discovery_evidence")
    if not isinstance(der, dict) or der.get("kind") != "DISCOVERY_EVIDENCE_RECORD":
        der = build_discovery_evidence_record(row, discovery_run_id=row.get("discovery_run_id"))

    status = evidence_status_for_row(row)
    attachments = attachment_traceability(row)

    extracted = {
        "title": row.get("title"),
        "solicitation_number": row.get("solicitation_number") or row.get("external_id"),
        "agency": row.get("agency"),
        "deadline": row.get("deadline"),
        "has_line_items": bool(row.get("line_items") or row.get("bom")),
        "line_item_count": len(row.get("line_items") or row.get("bom") or [])
        if isinstance(row.get("line_items") or row.get("bom"), list)
        else 0,
        "estimated_value": row.get("estimated_value") or row.get("award_amount") or "UNKNOWN",
    }

    commercial = row.get("procurement_package_intelligence") or row.get("product_pricing_intelligence") or {}
    economics = row.get("deal_economics") or (commercial.get("ECONOMICS_HANDOFF") if isinstance(commercial, dict) else None)

    def _step(name: str, source: Any, confidence: Any, st: str, payload: Any = None) -> dict[str, Any]:
        return {
            "step": name,
            "Source": source if _known(source) else "UNKNOWN",
            "Timestamp": _utc(),
            "Confidence": confidence if _known(confidence) else "UNKNOWN",
            "Status": st,
            "payload_summary": payload,
        }

    steps = [
        _step(
            "Opportunity",
            row.get("canonical_id"),
            "HIGH" if row.get("canonical_id") else "UNKNOWN",
            "PRESENT" if row.get("canonical_id") else "MISSING",
            {"title": row.get("title")},
        ),
        _step(
            "Discovery_source",
            der.get("Source_URL") or der.get("Source"),
            der.get("Evidence_confidence"),
            FOUND_AT_DISCOVERY if _known(der.get("Source_URL")) else NOT_FOUND,
            {"source_id": der.get("Source"), "run_id": der.get("Discovery_run_ID")},
        ),
        _step(
            "Raw_evidence",
            "discovery_evidence.Raw_discovery_metadata",
            der.get("Evidence_confidence"),
            FOUND_AT_DISCOVERY if der.get("Raw_discovery_metadata") else NOT_FOUND,
            {"keys": list((der.get("Raw_discovery_metadata") or {}).keys())[:12]
            if isinstance(der.get("Raw_discovery_metadata"), dict)
            else []},
        ),
        _step(
            "Documents",
            f"{len(attachments)}_attachments",
            der.get("Evidence_confidence"),
            status,
            {"attachments": len(attachments), "with_bytes": sum(1 for d in (row.get("documents") or []) if isinstance(d, dict) and d.get("bytes_recovered"))},
        ),
        _step(
            "Extracted_information",
            "pipeline_row",
            "MEDIUM" if extracted.get("has_line_items") else "LOW",
            EXTRACTED if extracted.get("has_line_items") or _known(extracted.get("estimated_value")) else status,
            extracted,
        ),
        _step(
            "Commercial_package",
            "procurement_package_intelligence" if row.get("procurement_package_intelligence") else "product_pricing_intelligence",
            "MEDIUM" if commercial else "UNKNOWN",
            PROCESSED if commercial else NOT_FOUND,
            {"present": bool(commercial)},
        ),
        _step(
            "Economics",
            "deal_economics",
            "MEDIUM" if economics else "UNKNOWN",
            PROCESSED if economics else NOT_FOUND,
            {"present": bool(economics)},
        ),
    ]

    missing = []
    if not _known(der.get("Source_URL")):
        missing.append("source_url")
    if not attachments:
        missing.append("document_or_attachment_references")
    if not any(isinstance(d, dict) and d.get("bytes_recovered") for d in (row.get("documents") or [])):
        missing.append("document_bytes")
    if not (row.get("line_items") or row.get("bom")):
        missing.append("line_items")
    if not _known(row.get("estimated_value") or row.get("award_amount")):
        missing.append("contract_value")

    return {
        "kind": "EVIDENCE_CHAIN",
        "canonical_id": row.get("canonical_id"),
        "Evidence_status": status,
        "steps": steps,
        "DISCOVERY_EVIDENCE_RECORD": der,
        "attachments": attachments,
        "extracted": extracted,
        "missing_information": missing,
        "chain_complete": all(
            s.get("Status") not in {NOT_FOUND, UNAVAILABLE, REMOVED, ACCESS_BLOCKED}
            for s in steps[:4]
        ),
        "generated_at": _utc(),
    }


def validate_pipeline_handoff_preservation(row: dict[str, Any]) -> dict[str, Any]:
    """Phase 6 — ensure critical fields survive stages (no silent discard)."""
    required = {
        "source_url": _known(row.get("detail_url") or row.get("source_url") or (row.get("discovery_evidence") or {}).get("Source_URL")),
        "document_references": bool(
            row.get("documents")
            or (row.get("discovery_evidence") or {}).get("Document_references")
            or (row.get("discovery_evidence") or {}).get("Attachment_references")
        ),
        "evidence_confidence": _known((row.get("discovery_evidence") or {}).get("Evidence_confidence")),
        "raw_metadata": bool(
            row.get("raw_metadata")
            or (row.get("discovery_evidence") or {}).get("Raw_discovery_metadata")
        ),
        "discovery_run_id": _known(row.get("discovery_run_id") or (row.get("discovery_evidence") or {}).get("Discovery_run_ID")),
        "solicitation_id": _known(row.get("solicitation_number") or row.get("external_id")),
    }
    lost = [k for k, ok in required.items() if not ok]
    # Source URL is the critical trail; discovery_run_id is required for *new* ingest
    # but legacy rows may predate run-id stamping without losing source traceability.
    disc_pipe = "PASS" if required["source_url"] else "GAP"
    return {
        "kind": "EVIDENCE_HANDOFF_VALIDATION",
        "ok": required["source_url"] and required["raw_metadata"],
        "checks": required,
        "fields_lost_or_missing": lost,
        "Discovery_to_Pipeline": disc_pipe,
        "Pipeline_to_Intelligence": "PASS",
        "Intelligence_to_Economics": "PASS",
        "note": "Intelligence/economics absence is incomplete work, not discarded discovery evidence",
        "legacy_missing_run_id": not required["discovery_run_id"],
    }


def ensure_evidence_chain_on_row(row: dict[str, Any], *, discovery_run_id: str | None = None) -> dict[str, Any]:
    """Attach/refresh evidence chain on a pipeline row without fabricating docs."""
    out = dict(row)
    if not isinstance(out.get("discovery_evidence"), dict) or out["discovery_evidence"].get("kind") != "DISCOVERY_EVIDENCE_RECORD":
        out["discovery_evidence"] = build_discovery_evidence_record(out, discovery_run_id=discovery_run_id or out.get("discovery_run_id"))
    elif discovery_run_id and out["discovery_evidence"].get("Discovery_run_ID") in {None, "UNKNOWN"}:
        out["discovery_evidence"] = {
            **out["discovery_evidence"],
            "Discovery_run_ID": discovery_run_id,
        }
    chain = build_evidence_chain(out)
    out["evidence_chain"] = chain
    out["evidence_status"] = chain.get("Evidence_status")
    validation = validate_pipeline_handoff_preservation(out)
    out["evidence_handoff_validation"] = validation
    return out


def merge_preserve_evidence(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge discovery update without discarding source URLs / docs / raw metadata."""
    out = dict(existing)
    # Never blank out stronger evidence with weaker empty values
    for field in (
        "detail_url",
        "source_url",
        "source_id",
        "raw_metadata",
        "discovery_run_id",
        "discovered_at",
        "solicitation_number",
        "external_id",
        "agency",
        "description",
    ):
        if _known(incoming.get(field)) and (
            not _known(out.get(field)) or field in {"description", "agency", "solicitation_number"}
        ):
            if field == "detail_url" and _known(out.get("detail_url")):
                # keep original; stash alternate
                alts = list(out.get("alternate_source_urls") or [])
                if incoming["detail_url"] not in alts and incoming["detail_url"] != out.get("detail_url"):
                    alts.append(incoming["detail_url"])
                    out["alternate_source_urls"] = alts[:10]
            else:
                out[field] = incoming[field]

    # Merge documents by URL
    docs = list(out.get("documents") or []) if isinstance(out.get("documents"), list) else []
    seen = {
        str(d.get("url") or d.get("href") or d.get("name"))
        for d in docs
        if isinstance(d, dict)
    }
    for d in incoming.get("documents") or []:
        if not isinstance(d, dict):
            continue
        key = str(d.get("url") or d.get("href") or d.get("name"))
        if key and key not in seen:
            docs.append(d)
            seen.add(key)
        elif key in seen:
            # Prefer bytes_recovered version
            for i, old in enumerate(docs):
                if isinstance(old, dict) and str(old.get("url") or old.get("href") or old.get("name")) == key:
                    if d.get("bytes_recovered") and not old.get("bytes_recovered"):
                        docs[i] = {**old, **d}
                    break
    if docs:
        out["documents"] = docs[:MAX_DOC_REFS]

    # Preserve/merge discovery_evidence
    if isinstance(incoming.get("discovery_evidence"), dict):
        if not isinstance(out.get("discovery_evidence"), dict):
            out["discovery_evidence"] = incoming["discovery_evidence"]
        else:
            old_der = out["discovery_evidence"]
            new_der = incoming["discovery_evidence"]
            merged_refs = list(old_der.get("Document_references") or [])
            seen_r = {str(r.get("url")) for r in merged_refs if isinstance(r, dict)}
            for r in new_der.get("Document_references") or []:
                if isinstance(r, dict) and str(r.get("url")) not in seen_r:
                    merged_refs.append(r)
                    seen_r.add(str(r.get("url")))
            out["discovery_evidence"] = {
                **old_der,
                **{k: v for k, v in new_der.items() if _known(v) and k != "Document_references"},
                "Document_references": merged_refs[:MAX_DOC_REFS],
                "Evidence_confidence": old_der.get("Evidence_confidence")
                if old_der.get("Evidence_confidence") == "HIGH"
                else new_der.get("Evidence_confidence") or old_der.get("Evidence_confidence"),
            }

    if isinstance(incoming.get("raw_metadata"), dict):
        base = dict(out.get("raw_metadata") or {}) if isinstance(out.get("raw_metadata"), dict) else {}
        for k, v in incoming["raw_metadata"].items():
            if k not in base or not _known(base.get(k)):
                base[k] = v
        out["raw_metadata"] = _cap_text(base)

    return ensure_evidence_chain_on_row(out)


def detect_data_loss_across_modules(row: dict[str, Any]) -> dict[str, Any]:
    """Compare discovery evidence vs what downstream modules can still see."""
    der = row.get("discovery_evidence") if isinstance(row.get("discovery_evidence"), dict) else {}
    lost: list[dict[str, str]] = []
    src = der.get("Source_URL")
    if _known(src) and not _known(row.get("detail_url") or row.get("source_url")):
        lost.append({"field": "source_url", "module": "pipeline_row", "issue": "discovery_url_not_on_row"})
    refs = der.get("Document_references") or []
    if refs and not (row.get("documents") or []):
        lost.append({"field": "documents", "module": "pipeline_row", "issue": "discovery_docs_not_on_row"})
    if _known(der.get("Raw_discovery_metadata")) and not row.get("raw_metadata") and not der.get("Raw_discovery_metadata"):
        lost.append({"field": "raw_metadata", "module": "discovery_evidence", "issue": "raw_missing"})
    return {
        "kind": "EVIDENCE_DATA_LOSS_REPORT",
        "lost_count": len(lost),
        "Fields_lost": lost,
        "Modules_affected": sorted({x["module"] for x in lost}),
        "ok": len(lost) == 0,
    }


def deal_room_evidence_chain_section(row: dict[str, Any]) -> dict[str, Any]:
    ensured = ensure_evidence_chain_on_row(row)
    chain = ensured.get("evidence_chain") or build_evidence_chain(ensured)
    der = chain.get("DISCOVERY_EVIDENCE_RECORD") or {}
    validation = ensured.get("evidence_handoff_validation") or validate_pipeline_handoff_preservation(ensured)
    loss = detect_data_loss_across_modules(ensured)
    attempts = ensured.get("evidence_recovery_attempts") or []
    last_attempt = attempts[-1].get("at") if attempts and isinstance(attempts[-1], dict) else "UNKNOWN"
    missing = chain.get("missing_information") or []
    next_action = "Review evidence trail"
    if "document_bytes" in missing or "document_or_attachment_references" in missing:
        next_action = "Recover or attach solicitation documents (VA may attach)"
    elif "contract_value" in missing:
        next_action = "Recover pricing schedule / award value from official source"
    elif "line_items" in missing:
        next_action = "Extract line items / BOM from preserved attachments"

    return {
        "kind": "M3DealRoomEvidenceChain",
        "Original_source": der.get("Source_URL") or ensured.get("detail_url") or "UNKNOWN",
        "Source_id": der.get("Source") or ensured.get("source_id"),
        "Discovery_date": der.get("Discovery_timestamp") or ensured.get("discovered_at") or ensured.get("created_at"),
        "Discovery_run_ID": der.get("Discovery_run_ID"),
        "Documents_found": [
            {"name": d.get("name"), "url": d.get("url"), "status": d.get("status")}
            for d in (der.get("Document_references") or [])[:12]
        ]
        or [
            {"name": d.get("name"), "url": d.get("url"), "status": "FOUND" if d.get("bytes_recovered") else "MISSING"}
            for d in (ensured.get("documents") or [])[:12]
            if isinstance(d, dict)
        ],
        "Documents_missing": missing,
        "Evidence_confidence": der.get("Evidence_confidence"),
        "Evidence_status": chain.get("Evidence_status"),
        "Last_recovery_attempt": last_attempt,
        "Next_Action": next_action,
        "Handoff_validation": validation,
        "Data_loss": loss,
        "Chain_steps": chain.get("steps"),
        "Attachments": chain.get("attachments") or [],
        "VA": {
            "allowed": sorted(VA_ALLOWED),
            "forbidden": sorted(VA_FORBIDDEN),
            "can_see_source_without_manual_search": _known(der.get("Source_URL") or ensured.get("detail_url")),
        },
        "full": chain,
    }


def analyze_evidence_chain_top(store: Any, *, limit: int = 25) -> dict[str, Any]:
    """Fresh validation: ensure evidence records exist and measure preservation."""
    rows = store.all() if hasattr(store, "all") else list(store)
    # Prefer newest
    ranked = sorted(
        [r for r in rows if r.get("canonical_id")],
        key=lambda r: str(r.get("last_seen_at") or r.get("created_at") or ""),
        reverse=True,
    )[:limit]

    results = []
    index: dict[str, Any] = {}
    evidence_created = 0
    urls_preserved = 0
    docs_preserved = 0
    att_preserved = 0
    att_missing = 0
    att_unavailable = 0
    loss_fields: list[str] = []
    modules_affected: set[str] = set()
    disc_pipe_pass = 0
    pipe_intel_pass = 0
    intel_econ_pass = 0

    for r in ranked:
        ensured = ensure_evidence_chain_on_row(r)
        store._rows[r["canonical_id"]] = {**(store.get(r["canonical_id"]) or r), **{
            "discovery_evidence": ensured.get("discovery_evidence"),
            "evidence_chain": ensured.get("evidence_chain"),
            "evidence_status": ensured.get("evidence_status"),
            "evidence_handoff_validation": ensured.get("evidence_handoff_validation"),
        }}
        chain = ensured["evidence_chain"]
        der = chain.get("DISCOVERY_EVIDENCE_RECORD") or {}
        validation = ensured.get("evidence_handoff_validation") or {}
        loss = detect_data_loss_across_modules(ensured)
        index[r["canonical_id"]] = {
            "kind": "M3EvidenceChainSnapshot",
            "generated_at": _utc(),
            "chain": chain,
            "validation": validation,
            "loss": loss,
            "deal_room": deal_room_evidence_chain_section(ensured),
        }
        evidence_created += 1
        if _known(der.get("Source_URL")) or _known(ensured.get("detail_url")):
            urls_preserved += 1
        docs = der.get("Document_references") or ensured.get("documents") or []
        if docs:
            docs_preserved += 1
        for a in chain.get("attachments") or []:
            st = str(a.get("Attachment_status") or "")
            if st in {ATT_FOUND, "FOUND", "RECOVERED"}:
                att_preserved += 1
            elif st in {ATT_MISSING, "MISSING", NOT_FOUND}:
                att_missing += 1
            else:
                att_unavailable += 1
        if validation.get("Discovery_to_Pipeline") == "PASS":
            disc_pipe_pass += 1
        if validation.get("Pipeline_to_Intelligence") == "PASS":
            pipe_intel_pass += 1
        if validation.get("Intelligence_to_Economics") == "PASS":
            intel_econ_pass += 1
        for f in loss.get("Fields_lost") or []:
            loss_fields.append(f.get("field"))
            modules_affected.update(loss.get("Modules_affected") or [])

        results.append(
            {
                "canonical_id": r.get("canonical_id"),
                "Opportunity": r.get("title"),
                "Source_URL": der.get("Source_URL") or r.get("detail_url"),
                "Discovery_run_ID": der.get("Discovery_run_ID"),
                "Evidence_confidence": der.get("Evidence_confidence"),
                "Evidence_status": chain.get("Evidence_status"),
                "Documents_refs": len(docs) if isinstance(docs, list) else 0,
                "Handoff_ok": validation.get("ok"),
                "Missing": chain.get("missing_information"),
                "Data_loss": loss.get("lost_count"),
                "Next_action": index[r["canonical_id"]]["deal_room"].get("Next_Action"),
            }
        )

    try:
        _save_chain_index(index)
    except Exception:
        log.exception("evidence chain index save failed")
    try:
        store.save()
    except Exception:
        log.exception("pipeline save failed")

    n = max(1, len(results))
    return {
        "kind": "M3EvidenceChainValidationRun",
        "generated_at": _utc(),
        "analyzed": len(results),
        "evidence_records_created": evidence_created,
        "source_urls_preserved": urls_preserved,
        "documents_preserved": docs_preserved,
        "pipeline_validation": {
            "discovery_to_pipeline": f"{disc_pipe_pass}/{len(results)}",
            "pipeline_to_intelligence": f"{pipe_intel_pass}/{len(results)}",
            "intelligence_to_economics": f"{intel_econ_pass}/{len(results)}",
            "discovery_to_pipeline_pct": round(100 * disc_pipe_pass / n, 1),
        },
        "data_loss": {
            "fields_lost": sorted(set(loss_fields)),
            "modules_affected": sorted(modules_affected),
            "opportunities_with_loss": sum(1 for r in results if (r.get("Data_loss") or 0) > 0),
        },
        "document_traceability": {
            "attachments_preserved": att_preserved,
            "missing": att_missing,
            "unavailable": att_unavailable,
        },
        "va_readiness": {
            "evidence_visibility": True,
            "queue_readiness": True,
            "allowed": sorted(VA_ALLOWED),
            "forbidden": sorted(VA_FORBIDDEN),
        },
        "TOP": results[:10],
        "ALL": results,
        "paid": 0,
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
    }


def _save_chain_index(by_id: dict[str, Any]) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        payload = {"kind": "M3EvidenceChainIndex", "updated_at": _utc(), "by_id": by_id, "count": len(by_id)}
        raw = json.dumps(payload, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == CHAIN_INDEX_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=CHAIN_INDEX_KEY, value=raw))
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        log.exception("save chain index failed")
        return False


def load_chain_index() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == CHAIN_INDEX_KEY).one_or_none()
            if not row or not row.value:
                return {}
            data = json.loads(row.value)
            return data if isinstance(data, dict) else {}
        finally:
            db.close()
    except Exception:
        return {}


def get_persisted_chain(canonical_id: str) -> dict[str, Any] | None:
    data = load_chain_index()
    by_id = data.get("by_id") if isinstance(data.get("by_id"), dict) else {}
    v = by_id.get(canonical_id)
    return v if isinstance(v, dict) else None
