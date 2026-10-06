"""Phases 3/8/9 — Quote packet source traceability + quantity conservation audit."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from final_pre_scale_proof.dania_scope import Path_name, _scope_of
from final_pre_scale_proof.models import (
    DANIA_OID,
    PACKET_FAIL,
    PACKET_PASS,
    PRIOR_DC_PACKETS,
    PRIOR_MLR_CK,
)
from m3_data_root import data_path

_REQUIRED_TRACE = [
    "opportunity",
    "source_document",
    "page_sheet_cell",
    "clin_line_id",
    "raw_description",
    "normalized_identity",
    "qty",
    "uom",
    "pack",
    "manufacturer",
    "mpn_model",
    "condition",
    "delivery_requirement",
]


def _load_packets() -> list[dict[str, Any]]:
    p = data_path(PRIOR_DC_PACKETS)
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("packets") or []


def _load_mlr_index() -> dict[str, dict[str, Any]]:
    p = data_path(PRIOR_MLR_CK)
    if not p.exists():
        return {}
    ck = json.loads(p.read_text(encoding="utf-8"))
    idx: dict[str, dict[str, Any]] = {}
    for oid, od in (ck.get("opportunities") or {}).items():
        for ln in od.get("lines") or []:
            lid = ln.get("line_id")
            if lid:
                idx[str(lid)] = ln
    return idx


def _enrich_line(packet: dict[str, Any], pline: dict[str, Any], src: dict[str, Any] | None) -> dict[str, Any]:
    src = src or {}
    source_doc = Path_name(src.get("source_document")) or Path_name(pline.get("source_document"))
    page = src.get("page") or src.get("sheet") or pline.get("page")
    cell = src.get("cell")
    page_sheet_cell = None
    if page is not None or cell is not None:
        page_sheet_cell = f"page={page};cell={cell}" if cell else f"page={page}"
    # Plans PDFs often lack page metadata in extraction — mark UNKNOWN not invent
    if source_doc and page_sheet_cell is None:
        page_sheet_cell = "UNKNOWN_PAGE"  # still fails hard page requirement

    mpn = pline.get("mpn") or src.get("mpn") or src.get("model") or pline.get("model")
    mfr = pline.get("manufacturer") or src.get("manufacturer")
    qty = pline.get("qty") if pline.get("qty") is not None else src.get("quantity")
    uom = pline.get("uom") or src.get("uom") or "EA"
    pack = pline.get("pack") or src.get("pack") or 1
    raw = pline.get("description") or src.get("description") or ""
    delivery = (packet.get("request") or {}).get("delivery_destination")

    missing = []
    fields = {
        "opportunity": packet.get("opportunity_id"),
        "source_document": source_doc,
        "page_sheet_cell": page_sheet_cell,
        "clin_line_id": pline.get("line_id") or pline.get("clin") or src.get("clin"),
        "raw_description": raw,
        "normalized_identity": f"{mfr or ''}::{mpn or ''}".strip(":"),
        "qty": qty,
        "uom": uom,
        "pack": pack,
        "manufacturer": mfr,
        "mpn_model": mpn,
        "condition": pline.get("condition") or "NEW",
        "delivery_requirement": delivery,
    }
    for k in _REQUIRED_TRACE:
        v = fields.get(k)
        if v is None or v == "" or v == "UNKNOWN_PAGE" or v == "::":
            missing.append(k)

    # Soft: delivery may be unknown at packet prep — still flag
    # Soft: manufacturer may be null if MPN-only DuMor models — allow if mpn present
    if "manufacturer" in missing and fields.get("mpn_model"):
        missing = [m for m in missing if m != "manufacturer"]
    if "normalized_identity" in missing and fields.get("mpn_model"):
        missing = [m for m in missing if m != "normalized_identity"]

    return {
        **fields,
        "line_id": pline.get("line_id"),
        "clin": pline.get("clin") or src.get("clin"),
        "scope": _scope_of(src) if src else None,
        "missing_trace_fields": missing,
        "traceable": len(missing) == 0,
        "source_enriched": bool(src),
    }


def audit_packets(*, dania_only: bool = False) -> dict[str, Any]:
    packets = _load_packets()
    mlr = _load_mlr_index()
    audited = []
    passed = failed = 0
    lines_traceable = lines_untraceable = 0
    duplicate_line_ids: list[str] = []
    all_line_ids: list[str] = []
    qty_by_opp_source: dict[str, float] = defaultdict(float)
    qty_by_opp_packet: dict[str, float] = defaultdict(float)

    # Source quantity totals from MLR (identity-usable quote candidates)
    mlr_ck_path = data_path(PRIOR_MLR_CK)
    mlr_ck = json.loads(mlr_ck_path.read_text(encoding="utf-8")) if mlr_ck_path.exists() else {}
    for oid, od in (mlr_ck.get("opportunities") or {}).items():
        for ln in od.get("lines") or []:
            if not ln.get("identity_usable"):
                continue
            q = float(ln.get("quantity") or 0)
            qty_by_opp_source[oid] += q

    for pkt in packets:
        oid = pkt.get("opportunity_id")
        if dania_only and oid != DANIA_OID:
            continue
        enriched_lines = []
        pkt_missing = []
        for pl in pkt.get("lines") or []:
            lid = str(pl.get("line_id") or "")
            all_line_ids.append(lid)
            src = mlr.get(lid)
            el = _enrich_line(pkt, pl, src)
            enriched_lines.append(el)
            q = float(el.get("qty") or 0)
            qty_by_opp_packet[oid] += q
            if el["traceable"]:
                lines_traceable += 1
            else:
                lines_untraceable += 1
                pkt_missing.extend(el["missing_trace_fields"])

        # Packet-level checks (Phase 3 Dania / Phase 8 all)
        packet_checks = {
            "manufacturer_present_on_lines": all(
                bool(l.get("manufacturer") or l.get("mpn_model")) for l in enriched_lines
            ),
            "mpn_or_model_present": all(bool(l.get("mpn_model")) for l in enriched_lines) if enriched_lines else False,
            "qty_present": all(l.get("qty") is not None for l in enriched_lines),
            "uom_present": all(bool(l.get("uom")) for l in enriched_lines),
            "pack_present": all(l.get("pack") is not None for l in enriched_lines),
            "condition_NEW": all(str(l.get("condition") or "").upper() == "NEW" for l in enriched_lines),
            "delivery_location": bool((pkt.get("request") or {}).get("delivery_destination")),
            "delivery_deadline": bool(pkt.get("deadline") or pkt.get("requested_quote_return_date")),
            "brand_equal_rule": "NOT_CAPTURED_IN_PACKET",
            "required_accessories": "NOT_CAPTURED_IN_PACKET",
            "installation_responsibility": "NOT_CAPTURED_IN_PACKET",
            "freight_treatment": (pkt.get("request") or {}).get("freight_treatment"),
            "normalized_field_only": False,  # enriched from MLR when available
            "source_traceable_all_lines": all(l["traceable"] for l in enriched_lines) if enriched_lines else False,
        }

        # Contaminated construction lines in Dania "quote" packets
        install_in_packet = [l for l in enriched_lines if l.get("scope") == "INSTALL"]
        status = PACKET_PASS
        fail_reasons = []
        if not packet_checks["source_traceable_all_lines"]:
            status = PACKET_FAIL
            fail_reasons.append("missing_source_traceability")
        if install_in_packet and oid == DANIA_OID:
            status = PACKET_FAIL
            fail_reasons.append("install_construction_lines_in_quote_packet")
        if not packet_checks["mpn_or_model_present"]:
            status = PACKET_FAIL
            fail_reasons.append("missing_mpn_model")
        if not packet_checks["delivery_location"]:
            fail_reasons.append("delivery_location_missing")
            # soft fail for outreach prep — still PACKET_FAIL for owner-ready
            status = PACKET_FAIL

        if status == PACKET_PASS:
            passed += 1
        else:
            failed += 1

        audited.append(
            {
                "packet_id": pkt.get("packet_id"),
                "opportunity_id": oid,
                "supplier": (pkt.get("supplier") or {}).get("supplier_name"),
                "domain": (pkt.get("supplier") or {}).get("domain"),
                "line_count": len(enriched_lines),
                "lines": enriched_lines,
                "checks": packet_checks,
                "install_lines_in_packet": len(install_in_packet),
                "status": status,
                "fail_reasons": fail_reasons,
                "prior_ready_flag": pkt.get("ready_for_owner_quote_outreach"),
                "owner_ready_after_audit": status == PACKET_PASS,
            }
        )

    # Duplicates
    counts = Counter([x for x in all_line_ids if x])
    duplicate_line_ids = [lid for lid, c in counts.items() if c > 1]

    # Quantity conservation per opportunity
    conservation = []
    total_diff = 0.0
    for oid in sorted(set(list(qty_by_opp_source) + list(qty_by_opp_packet))):
        src_t = qty_by_opp_source.get(oid, 0.0)
        pkt_t = qty_by_opp_packet.get(oid, 0.0)
        # Packet totals only cover quote-packeted lines; compare packet vs sum of its source lines
        diff = round(pkt_t - src_t, 4)
        # For conservation of packeted lines only: recompute from audited
        conservation.append(
            {
                "opportunity_id": oid,
                "SOURCE_QUANTITY_TOTAL": src_t,
                "QUOTE_PACKET_QUANTITY_TOTAL": pkt_t,
                "DIFF": diff,
                "note": "DIFF!=0 may mean non-quoteable usable lines excluded from packets OR duplicates/over-inclusion",
            }
        )
        total_diff += abs(diff)

    # Stricter conservation: for each packet line, qty must match source line qty
    line_qty_mismatches = []
    for a in audited:
        for l in a["lines"]:
            src = mlr.get(str(l.get("line_id") or ""))
            if not src:
                continue
            sq = float(src.get("quantity") or 0)
            pq = float(l.get("qty") or 0)
            if abs(sq - pq) > 1e-6:
                line_qty_mismatches.append(
                    {"line_id": l.get("line_id"), "source_qty": sq, "packet_qty": pq}
                )

    dania_packets = [a for a in audited if a["opportunity_id"] == DANIA_OID]
    dania_pass = all(a["status"] == PACKET_PASS for a in dania_packets) and bool(dania_packets)

    return {
        "total_packets": len(audited),
        "passed": passed,
        "failed": failed,
        "lines_traceable": lines_traceable,
        "lines_untraceable": lines_untraceable,
        "quantity_diff_abs_sum": round(total_diff, 4),
        "duplicate_lines": duplicate_line_ids,
        "duplicate_count": len(duplicate_line_ids),
        "line_qty_mismatches": line_qty_mismatches,
        "conservation_by_opportunity": conservation,
        "packets": audited,
        "DANIA_QUOTE_PACKETS": {
            "Packets": len(dania_packets),
            "Lines": sum(a["line_count"] for a in dania_packets),
            "Suppliers": sorted({a.get("domain") or a.get("supplier") for a in dania_packets}),
            "Coverage": None,  # filled by sweep from prior
            "Source-traceable": all(
                all(l["traceable"] for l in a["lines"]) for a in dania_packets
            )
            if dania_packets
            else False,
            "Quantity conservation": all(
                abs(c["DIFF"]) < 1e-6
                for c in conservation
                if c["opportunity_id"] == DANIA_OID
            ),
            "PASS_FAIL": "PASS" if dania_pass else "FAIL",
            "fail_summary": [a["fail_reasons"] for a in dania_packets],
        },
    }
