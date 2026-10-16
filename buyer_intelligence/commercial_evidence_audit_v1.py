"""Read-only commercial evidence audit for the live FSPF job.

Diagnostic only — does not mutate queues, auth, or purchases.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path

BUILD = "20261016-m3-commercial-evidence-audit-v1"
FAMILY = "full_scale_production_funnel_v1"
JOB_DEFAULT = "full_scale_production_funnel_v1-20261015T015645Z-23533d4d"
OUT = "_og_commercial_research_pipeline_out"
RFQ_REL = f"{OUT}/m3_fspf_v1_rfq_queue.json"
AUDIT_REL = f"{OUT}/m3_commercial_evidence_audit_v1.json"

_STOP = {
    "and",
    "or",
    "the",
    "a",
    "an",
    "for",
    "with",
    "to",
    "of",
    "in",
    "on",
    "at",
    "by",
    "from",
    "as",
    "is",
    "are",
    "be",
    "this",
    "that",
    "it",
    "w/",
    "w",
}


def _utc() -> str:
    return now_utc().isoformat()


def _line_fields(li: dict[str, Any]) -> dict[str, Any]:
    desc = (
        li.get("description")
        or li.get("raw_description")
        or li.get("product_description")
        or li.get("line_description")
        or li.get("item_description")
        or ""
    )
    mpn = (
        li.get("mpn")
        or li.get("part_number")
        or li.get("manufacturer_part_number")
        or li.get("model")
        or li.get("sku")
        or ""
    )
    mfr = li.get("manufacturer") or li.get("brand") or li.get("mfr") or ""
    qty = li.get("qty") or li.get("quantity") or li.get("qty_required")
    uom = li.get("uom") or li.get("unit") or li.get("unit_of_measure") or ""
    pack = li.get("pack_qty") or li.get("case_qty") or li.get("packaging") or ""
    return {
        "description": str(desc)[:300],
        "manufacturer": str(mfr)[:120] or None,
        "mpn": str(mpn)[:80] or None,
        "qty": qty,
        "uom": str(uom)[:40] or None,
        "packaging": str(pack)[:80] or None,
        "restrictions": li.get("restrictions") or li.get("notes"),
    }


def classify_line(fields: dict[str, Any]) -> str:
    desc = (fields.get("description") or "").strip()
    mpn = (fields.get("mpn") or "").strip()
    mfr = (fields.get("manufacturer") or "").strip()
    if not desc or len(desc) < 8:
        return "INVALID_EXTRACTION"
    if mpn and (len(mpn) < 3 or mpn.lower() in _STOP):
        mpn = ""
    low = desc.lower()
    restricted = any(
        x in low
        for x in (
            "sole source",
            "proprietary",
            "must be oem",
            "brand name only",
            "no substitutions",
            "authorized dealer only",
        )
    )
    if mfr and mpn and re.search(r"[0-9]", mpn):
        return "EXACT_PRODUCT_IDENTIFIED"
    if mpn and re.search(r"[A-Za-z].*[0-9]|[0-9].*[A-Za-z]", mpn) and len(mpn) >= 4:
        return "EXACT_PRODUCT_IDENTIFIED"
    if len(desc) >= 40 and (
        any(
            x in low
            for x in (
                "copy paper",
                "toner",
                "ink cartridge",
                "gloves",
                "cleaner",
                "battery",
                "lamp",
                "filter",
                "cable",
                "screw",
                "bolt",
                "nut",
                "washer",
                "tape",
                "bag",
                "box",
                "envelope",
            )
        )
        or (fields.get("uom") and fields.get("qty") not in (None, ""))
    ):
        return "COMMERCIALLY_SOURCEABLE_GENERIC"
    if len(desc) >= 24 and (mfr or "size" in low or "spec" in low or re.search(r"\d", desc)):
        return "SUPPLIER_CLARIFICATION_REQUIRED"
    if restricted:
        return "RESTRICTED_OR_UNSOURCEABLE"
    if len(desc) < 24:
        return "SPECIFICATION_INCOMPLETE"
    return "SUPPLIER_CLARIFICATION_REQUIRED"


def classify_rfq(entry: dict[str, Any]) -> str:
    rfq = entry.get("rfq") if isinstance(entry.get("rfq"), dict) else entry
    lines = list(rfq.get("line_items") or entry.get("line_items") or [])
    title = str(entry.get("title") or rfq.get("title") or "")
    buyer = entry.get("buyer") or rfq.get("buyer")
    deadline = entry.get("deadline") or rfq.get("deadline")
    supplier = (
        rfq.get("supplier")
        or rfq.get("supplier_name")
        or entry.get("supplier")
        or ((rfq.get("distributors") or [None])[0])
    )
    qty_ok = any(
        isinstance(li, dict) and (li.get("qty") or li.get("quantity")) not in (None, "")
        for li in lines
    )
    desc_ok = any(
        isinstance(li, dict)
        and len(str(li.get("description") or li.get("raw_description") or "")) >= 16
        for li in lines
    )
    if not lines or not desc_ok:
        return "NEEDS_SPECIFICATION_REPAIR"
    if not supplier:
        return "NEEDS_SUPPLIER_TARGET"
    if not (title and buyer):
        return "NEEDS_AWARD_SCOPE_VERIFICATION"
    if deadline and str(deadline)[:10] < "2026-10-16":
        return "EXPIRED"
    if qty_ok and desc_ok and supplier:
        return "READY_FOR_OWNER_APPROVAL"
    if not qty_ok:
        return "NEEDS_SPECIFICATION_REPAIR"
    return "NEEDS_AWARD_SCOPE_VERIFICATION"


def run_commercial_evidence_audit(*, job_id: str | None = None) -> dict[str, Any]:
    from m3_jobs.store import JobStore
    from buyer_intelligence.product_line_store_v1 import get_lines, load_store

    jid = (job_id or JOB_DEFAULT).strip()
    store = JobStore(FAMILY)
    items = store.list_items(jid) or []
    pls = load_store(force=True)
    by_opp = pls.get("by_opportunity") or {}

    line_rows: list[dict[str, Any]] = []
    class_counts: Counter[str] = Counter()
    items_with_fully: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        fully = int(it.get("fully_specified_product_lines") or 0)
        complete = bool(it.get("complete_spec"))
        docs = int(it.get("documents_downloaded") or it.get("document_count") or 0)
        oid = str(it.get("OPPORTUNITY_ID") or it.get("opportunity_id") or "")
        if not oid:
            continue
        if fully > 0:
            items_with_fully.append(oid)
        if fully <= 0 and not complete and docs <= 0:
            continue
        lines = list(it.get("line_items") or []) or list(get_lines(oid) or [])
        for idx, li in enumerate(lines):
            if not isinstance(li, dict):
                continue
            fields = _line_fields(li)
            cat = classify_line(fields)
            class_counts[cat] += 1
            line_rows.append(
                {
                    "opportunity_id": oid,
                    "buyer": it.get("buyer") or it.get("BUYER"),
                    "title": (it.get("title") or it.get("TITLE") or "")[:160],
                    "deadline": it.get("deadline") or it.get("DEADLINE"),
                    "documents_downloaded": docs,
                    "fully_specified_product_lines": fully,
                    "complete_spec": complete,
                    "public_unit_price": it.get("public_unit_price"),
                    "terminal_blocker": it.get("terminal_blocker"),
                    "RESULT": it.get("RESULT"),
                    "purchasing_group_id": it.get("purchasing_group_id"),
                    "authoritative_url": (it.get("authoritative_url") or "")[:180] or None,
                    "line_index": idx + 1,
                    "classification": cat,
                    **fields,
                    "distributors": (it.get("distributors") or [])[:5],
                }
            )

    # Prefer the scorecard "68" universe: lines belonging to items with fully>0
    fully_set = set(items_with_fully)
    fully_line_rows = [r for r in line_rows if r["opportunity_id"] in fully_set]
    if not fully_line_rows:
        fully_line_rows = line_rows

    fully_class: Counter[str] = Counter(r["classification"] for r in fully_line_rows)

    rfq_path = data_path(RFQ_REL)
    rfq_blob: dict[str, Any] = {}
    try:
        from pathlib import Path

        p = Path(rfq_path)
        if p.exists():
            rfq_blob = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        rfq_blob = {}
    rfq_items = list(rfq_blob.get("items") or rfq_blob.get("rows") or [])
    rfq_rows: list[dict[str, Any]] = []
    rfq_counts: Counter[str] = Counter()
    for entry in rfq_items:
        if not isinstance(entry, dict):
            continue
        cat = classify_rfq(entry)
        rfq_counts[cat] += 1
        rfq = entry.get("rfq") if isinstance(entry.get("rfq"), dict) else entry
        lines = list(rfq.get("line_items") or entry.get("line_items") or [])
        rfq_rows.append(
            {
                "opportunity_id": entry.get("opportunity_id") or rfq.get("opportunity_id"),
                "title": (entry.get("title") or rfq.get("title") or "")[:160],
                "buyer": entry.get("buyer") or rfq.get("buyer"),
                "deadline": entry.get("deadline") or rfq.get("deadline"),
                "status": rfq.get("status") or entry.get("status"),
                "classification": cat,
                "line_count": len(lines),
                "public_unit_price": entry.get("public_unit_price")
                or rfq.get("public_unit_price"),
                "supplier": rfq.get("supplier")
                or rfq.get("supplier_name")
                or entry.get("supplier"),
                "delivery_location": rfq.get("delivery_location") or rfq.get("ship_to"),
                "has_qty": any(
                    isinstance(li, dict)
                    and (li.get("qty") or li.get("quantity")) not in (None, "")
                    for li in lines
                ),
                "sample_descriptions": [
                    str((li.get("description") or li.get("raw_description") or "")[:120])
                    for li in lines[:3]
                    if isinstance(li, dict)
                ],
            }
        )

    access: Counter[str] = Counter()
    access_samples: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        oid = str(it.get("OPPORTUNITY_ID") or it.get("opportunity_id") or "")
        if not oid:
            continue
        blocker = str(it.get("terminal_blocker") or "")
        docs = int(it.get("documents_downloaded") or it.get("document_count") or 0)
        if blocker.startswith("PAID_ENTITLEMENT") or "CB_SW_" in blocker:
            cat = "paid_entitlement_required"
        elif blocker in {
            "AUTH_FAILED",
            "AUTH_REJECTED",
            "BROWSER_SLOT_BUSY",
            "BROWSER_STARTUP_FAILED",
            "BROWSER_CONTEXT_INVALID",
            "SESSION_EXPIRED",
            "LOGIN_REQUIRED",
        } or "browser_" in str(it.get("fetch_via") or ""):
            cat = "authentication_defect"
        elif "OWNER" in blocker or blocker == "OWNER_ACTION_REQUIRED":
            cat = "owner_action"
        elif "PURCHASING_GROUP" in blocker or blocker == "AWAITING_PURCHASING_GROUP_REGISTRATION":
            cat = "free_pg_registration_unresolved"
        elif docs > 0 or it.get("package_status") in {
            "DOCUMENTS_RECOVERED",
            "FREE_PACKAGE_FOUND",
            "AUTHENTICATED_PACKAGE_DOCS",
        }:
            cat = "verified_accessible"
        else:
            cat = "unknown_access"
        access[cat] += 1
        access_samples.setdefault(cat, [])
        if len(access_samples[cat]) < 10:
            access_samples[cat].append(
                {
                    "opportunity_id": oid,
                    "blocker": blocker[:140],
                    "docs": docs,
                    "fully": it.get("fully_specified_product_lines"),
                    "title": (it.get("title") or it.get("TITLE") or "")[:100] or None,
                }
            )

    metric_fully = sum(
        int(it.get("fully_specified_product_lines") or 0)
        for it in items
        if isinstance(it, dict)
    )

    report = {
        "ok": True,
        "build": BUILD,
        "at": _utc(),
        "job_id": jid,
        "scorecard_crosscheck": {
            "sum_fully_specified_product_lines_on_items": metric_fully,
            "items_with_fully_gt_0": len(items_with_fully),
            "items_with_fully_ids": items_with_fully,
            "product_line_store_opps": len(by_opp),
            "product_line_store_lines": int(pls.get("line_count") or 0),
            "job_item_count": len(items),
            "rfq_queue_count": len(rfq_items),
            "audited_line_rows_from_fully_items": len(fully_line_rows),
            "audited_line_rows_all_recovered": len(line_rows),
        },
        "line_classification_counts": dict(fully_class),
        "line_classification_counts_all_recovered": dict(class_counts),
        "lines": fully_line_rows,
        "lines_all_recovered": line_rows if len(line_rows) != len(fully_line_rows) else None,
        "rfq_classification_counts": dict(rfq_counts),
        "rfqs": rfq_rows,
        "access_unique_opportunity_counts": dict(access),
        "access_samples": access_samples,
    }
    try:
        from pathlib import Path
        from m3_jobs.store import _atomic_write

        _atomic_write(Path(data_path(AUDIT_REL)), report)
        report["persisted_to"] = AUDIT_REL
    except Exception as exc:
        report["persist_error"] = type(exc).__name__
    return report
