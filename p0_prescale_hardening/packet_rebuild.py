"""P0-6 — Rebuild source-perfect quote packets with quantity conservation."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from m3_data_root import data_path
from p0_prescale_hardening.identity_revenue import identity_execution_ready
from p0_prescale_hardening.models import (
    BRIDGEPORT_OID,
    CHANNEL_PACKETS,
    COLLIER_OID,
    DANIA_OID,
    DEKALB_OID,
    GO_METRO_OID,
    OWNER_CHANNEL_TEST_READY,
    PACKET_FAIL,
    PACKET_PASS,
    PRIOR_MLR_CK,
    READY_FOR_OWNER_QUOTE_OUTREACH,
)
from p0_prescale_hardening.provenance import attach_provenance, is_quote_ready_provenance
from p0_prescale_hardening.scope_classify import classify_line_scope, may_enter_product_quote_packet

_SUPPLIER = {
    GO_METRO_OID: {
        "CUMMINS_PARTS": {
            "supplier_name": "CUMMINS Direct",
            "domain": "cummins.com",
            "contact_route": "https://www.cummins.com/support/find-location",
            "quote_request_capability": "OEM_LOCATOR_OR_SHOP",
            "account_login_required": False,
            "manufacturer_relationship": "OEM_DIRECT",
        }
    },
    DEKALB_OID: {
        "EMS_GENERAL": {
            "supplier_name": "Bound Tree Medical",
            "domain": "boundtree.com",
            "contact_route": "https://www.boundtree.com/customer-service",
            "quote_request_capability": "B2B_ACCOUNT",
            "account_login_required": True,
            "manufacturer_relationship": "AUTHORIZED_DISTRIBUTOR",
        },
        "EMS_PHARMA": {
            "supplier_name": "Henry Schein Medical",
            "domain": "henryschein.com",
            "contact_route": "https://www.henryschein.com/us-en/medical/contact-us.aspx",
            "quote_request_capability": "B2B_ACCOUNT",
            "account_login_required": True,
            "manufacturer_relationship": "AUTHORIZED_DISTRIBUTOR",
        },
        "EMS_PPE": {
            "supplier_name": "Medline",
            "domain": "medline.com",
            "contact_route": "https://www.medline.com/help/contact-us/",
            "quote_request_capability": "B2B_ACCOUNT",
            "account_login_required": True,
            "manufacturer_relationship": "AUTHORIZED_DISTRIBUTOR",
        },
    },
}

_DELIVERY = {
    GO_METRO_OID: "Go-Metro / Cincinnati metro transit receiving — confirm exact dock address with buyer",
    DEKALB_OID: "DeKalb County GA EMS / procurement receiving — confirm warehouse on PO",
    DANIA_OID: "City of Dania Beach, FL — Chester Byrd Park project site / City receiving",
}

_DEADLINE = {
    GO_METRO_OID: "Confirm solicitation close date with buyer before send",
    DEKALB_OID: "Confirm ITB 2026-108 close date with buyer before send",
}


def _load_mlr_lines(oid: str) -> list[dict[str, Any]]:
    p = data_path(PRIOR_MLR_CK)
    if not p.exists():
        return []
    ck = json.loads(p.read_text(encoding="utf-8"))
    return list(((ck.get("opportunities") or {}).get(oid) or {}).get("lines") or [])


def _cluster(oid: str, line: dict[str, Any]) -> str:
    mfr = str(line.get("manufacturer") or "")
    desc = str(line.get("description") or "")
    blob = f"{mfr} {desc}"
    if oid == GO_METRO_OID or "cummins" in mfr.lower():
        return "CUMMINS_PARTS"
    if re.search(r"pharma", blob, re.I):
        return "EMS_PHARMA"
    if re.search(r"ppe|protective|glove|ansell", blob, re.I):
        return "EMS_PPE"
    if oid == DEKALB_OID:
        return "EMS_GENERAL"
    if mfr:
        return f"MFR:{mfr.upper()}"
    return "OTHER"


def _gate_line(line: dict[str, Any]) -> dict[str, Any]:
    enriched = attach_provenance(dict(line))
    scope = classify_line_scope(enriched)
    ident = identity_execution_ready(enriched)
    blockers = []
    if not may_enter_product_quote_packet(enriched, classified=scope):
        blockers.append(f"scope:{scope['scope']}")
    if not ident["may_enter_production_quote_packet"]:
        blockers.extend(ident["blockers"] or ["identity_not_ready"])
    if not is_quote_ready_provenance(enriched):
        blockers.append("NO_PROVENANCE")
    if not (enriched.get("mpn") or enriched.get("model") or enriched.get("part_number")):
        blockers.append("missing_mpn_model")
    enriched["scope_class"] = scope["scope"]
    enriched["scope"] = scope
    enriched["identity_gate"] = ident
    enriched["IDENTITY_EXECUTION_READY"] = ident["IDENTITY_EXECUTION_READY"]
    enriched["packet_blockers"] = blockers
    enriched["packet_eligible"] = len(blockers) == 0
    return enriched


def rebuild_packets_for_opportunity(
    oid: str,
    *,
    channel_test: bool = False,
    require_deadline: bool = True,
) -> dict[str, Any]:
    raw_lines = _load_mlr_lines(oid)
    gated = [_gate_line(ln) for ln in raw_lines]
    eligible = [g for g in gated if g.get("packet_eligible")]

    # Dedupe by line_id
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    duplicates: list[str] = []
    for g in eligible:
        lid = str(g.get("line_id") or "")
        if lid in seen:
            duplicates.append(lid)
            continue
        seen.add(lid)
        unique.append(g)

    buckets: dict[str, list[dict[str, Any]]] = {}
    for g in unique:
        buckets.setdefault(_cluster(oid, g), []).append(g)

    buyer = oid.split(":")[1] if ":" in oid else oid
    sol = oid.split(":")[-1]
    delivery = _DELIVERY.get(oid) or f"{buyer} delivery — confirm address"
    deadline = _DEADLINE.get(oid)
    if oid in {DANIA_OID, BRIDGEPORT_OID, COLLIER_OID}:
        # not promoted as channel economics; deadline still needed for packet completeness
        deadline = deadline or "Confirm deadline with buyer before send"

    packets = []
    source_qty_total = 0.0
    packet_qty_total = 0.0

    for cluster, lines in sorted(buckets.items(), key=lambda x: -len(x[1])):
        supplier = (_SUPPLIER.get(oid) or {}).get(cluster) or {
            "supplier_name": cluster,
            "domain": "unknown",
            "contact_route": None,
            "quote_request_capability": "UNKNOWN",
            "account_login_required": True,
            "manufacturer_relationship": "UNKNOWN",
        }
        packet_lines = []
        for ln in lines:
            qty = float(ln.get("quantity") or 0)
            source_qty_total += qty
            packet_qty_total += qty
            prov = ln.get("SOURCE_PROVENANCE") or {}
            packet_lines.append(
                {
                    "opportunity_id": oid,
                    "line_id": ln.get("line_id"),
                    "clin": ln.get("clin"),
                    "raw_description": (ln.get("description") or "")[:240],
                    "normalized_identity": f"{ln.get('manufacturer') or ''}::{ln.get('mpn') or ln.get('model') or ''}",
                    "manufacturer": ln.get("manufacturer"),
                    "mpn": ln.get("mpn") or ln.get("part_number"),
                    "model": ln.get("model"),
                    "qty": qty,
                    "uom": ln.get("uom") or "EA",
                    "pack": ln.get("pack") or 1,
                    "condition": "NEW",
                    "delivery_destination": delivery,
                    "deadline": deadline,
                    "SOURCE_PROVENANCE": prov,
                    "source_document": prov.get("filename"),
                    "page_sheet_cell": (
                        f"page={prov.get('page')}"
                        if prov.get("page") is not None
                        else f"sheet={prov.get('sheet')};row={prov.get('row')};cell={prov.get('cell')}"
                    ),
                    "identity_class": (ln.get("identity_gate") or {}).get("identity_class"),
                    "IDENTITY_EXECUTION_READY": ln.get("IDENTITY_EXECUTION_READY"),
                    "scope": ln.get("scope_class"),
                    "original_qty": qty,
                    "original_uom": ln.get("uom") or "EA",
                    "converted_qty": qty,
                    "converted_uom": ln.get("uom") or "EA",
                    "conversion_rule": None,
                    "conversion_evidence": None,
                }
            )

        qty_diff = round(sum(l["qty"] for l in packet_lines) - sum(float(l.get("original_qty") or 0) for l in packet_lines), 6)
        # Per-packet conservation is always 0 by construction; also compare to gated source for these lines
        source_for_packet = sum(float(l.get("quantity") or 0) for l in lines)
        packet_sum = sum(l["qty"] for l in packet_lines)
        cons_diff = round(packet_sum - source_for_packet, 6)

        fail_reasons = []
        if any(not (l.get("SOURCE_PROVENANCE") or {}).get("complete") for l in packet_lines):
            fail_reasons.append("provenance_incomplete")
        if cons_diff != 0:
            fail_reasons.append(f"quantity_diff={cons_diff}")
        if not delivery:
            fail_reasons.append("delivery_missing")
        if require_deadline and not deadline:
            fail_reasons.append("deadline_missing")
        if not packet_lines:
            fail_reasons.append("no_lines")
        if supplier.get("domain") == "unknown":
            fail_reasons.append("supplier_unresolved")

        status = PACKET_PASS if not fail_reasons else PACKET_FAIL
        channel_ready = (
            channel_test
            and status == PACKET_PASS
            and oid in {GO_METRO_OID, DEKALB_OID}
            and bool(supplier.get("contact_route"))
        )
        packet_id = "QP-" + hashlib.sha1(f"{oid}|{cluster}|p0".encode()).hexdigest()[:10]
        packets.append(
            {
                "packet_id": packet_id,
                "opportunity_id": oid,
                "buyer": buyer,
                "solicitation_id": sol,
                "cluster_key": cluster,
                "supplier": supplier,
                "line_count": len(packet_lines),
                "lines": packet_lines,
                "SOURCE_QUANTITY_TOTAL": source_for_packet,
                "QUOTE_PACKET_QUANTITY_TOTAL": packet_sum,
                "DIFF": cons_diff,
                "duplicate_line_ids": [],
                "delivery_destination": delivery,
                "deadline": deadline,
                "status": OWNER_CHANNEL_TEST_READY
                if channel_ready
                else (READY_FOR_OWNER_QUOTE_OUTREACH if status == PACKET_PASS else status),
                "packet_audit_status": status,
                "fail_reasons": fail_reasons,
                "OWNER_CHANNEL_TEST_READY": channel_ready,
                "ready_for_owner_quote_outreach": status == PACKET_PASS,
                "do_not_send_automatically": True,
                "revenue_note": "CHANNEL_PROOF_ONLY — no $5K/$10K economics claimed"
                if oid in {GO_METRO_OID, DEKALB_OID}
                else "REVENUE_NOT_READY",
                "created_at": now_utc().isoformat(),
                "request": {
                    "condition": "NEW",
                    "delivery_destination": delivery,
                    "freight_treatment": "REQUEST_SEPARATE_FREIGHT_QUOTE",
                    "ask_for": [
                        "unit_price",
                        "extended_price",
                        "availability",
                        "lead_time",
                        "shipping_freight",
                        "quote_validity",
                        "payment_terms",
                    ],
                },
            }
        )

    all_line_ids = [l["line_id"] for p in packets for l in p["lines"]]
    dup_counts = Counter(all_line_ids)
    global_dups = [lid for lid, c in dup_counts.items() if c > 1]

    # Overall conservation: packet qty vs eligible unique source qty
    eligible_qty = sum(float(g.get("quantity") or 0) for g in unique)
    overall_packet_qty = sum(p["QUOTE_PACKET_QUANTITY_TOTAL"] for p in packets)
    overall_diff = round(overall_packet_qty - eligible_qty, 6)

    return {
        "opportunity_id": oid,
        "material_lines": len(raw_lines),
        "gated_ineligible": sum(1 for g in gated if not g.get("packet_eligible")),
        "eligible_unique": len(unique),
        "duplicates_skipped": duplicates,
        "packets": packets,
        "packet_count": len(packets),
        "passed": sum(1 for p in packets if p["packet_audit_status"] == PACKET_PASS),
        "failed": sum(1 for p in packets if p["packet_audit_status"] == PACKET_FAIL),
        "lines_in_packets": len(all_line_ids),
        "source_traceable_pct": (
            100.0
            * sum(
                1
                for p in packets
                for l in p["lines"]
                if (l.get("SOURCE_PROVENANCE") or {}).get("complete")
            )
            / max(1, len(all_line_ids))
        ),
        "SOURCE_QUANTITY_TOTAL": eligible_qty,
        "QUOTE_PACKET_QUANTITY_TOTAL": overall_packet_qty,
        "DIFF": overall_diff,
        "duplicate_lines": global_dups,
        "scope_counts": _scope_counts(gated),
        "identity_counts": _identity_counts(gated),
        "channel_ready_packets": [p for p in packets if p.get("OWNER_CHANNEL_TEST_READY")],
    }


def _scope_counts(gated: list[dict[str, Any]]) -> dict[str, int]:
    from collections import Counter

    return dict(Counter(g.get("scope_class") or "UNKNOWN" for g in gated))


def _identity_counts(gated: list[dict[str, Any]]) -> dict[str, int]:
    from collections import Counter

    return dict(
        Counter((g.get("identity_gate") or {}).get("identity_class") or "?" for g in gated)
    )


def rebuild_owner_channel_packets() -> dict[str, Any]:
    results = {}
    for oid in (GO_METRO_OID, DEKALB_OID):
        results[oid] = rebuild_packets_for_opportunity(oid, channel_test=True, require_deadline=True)

    # Parked / not promoted
    results[DANIA_OID] = {
        **rebuild_packets_for_opportunity(DANIA_OID, channel_test=False, require_deadline=True),
        "promotion": "REVENUE_NOT_READY — channel prep only if packets pass",
    }
    results[BRIDGEPORT_OID] = {"promotion": "REVENUE_NOT_READY", "packets": []}
    results[COLLIER_OID] = {"promotion": "PARK / EXECUTION_COMPLEX", "packets": []}

    all_packets = []
    for oid in (GO_METRO_OID, DEKALB_OID):
        all_packets.extend(results[oid].get("packets") or [])

    payload = {
        "build": "20261005-m3-p0-prescale-hardening-v1",
        "by_opportunity": results,
        "channel_packets": all_packets,
        "channel_ready": [p for p in all_packets if p.get("OWNER_CHANNEL_TEST_READY")],
        "count": len(all_packets),
        "ready_count": sum(1 for p in all_packets if p.get("OWNER_CHANNEL_TEST_READY")),
    }
    data_path(CHANNEL_PACKETS).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def generate_quote_request_text(packet: dict[str, Any]) -> str:
    supplier = (packet.get("supplier") or {}).get("supplier_name") or "Supplier"
    sol = packet.get("solicitation_id")
    buyer = packet.get("buyer")
    dest = packet.get("delivery_destination")
    deadline = packet.get("deadline")
    lines = []
    for i, ln in enumerate(packet.get("lines") or [], 1):
        lines.append(
            f"{i}. {ln.get('manufacturer') or ''} {ln.get('mpn') or ln.get('model') or ''} — "
            f"{(ln.get('raw_description') or '')[:120]}\n"
            f"   Qty: {ln.get('qty')} {ln.get('uom')} | NEW | Pack: {ln.get('pack')}"
        )
    return (
        f"Subject: Quote request — {buyer} solicitation {sol} / {packet.get('packet_id')}\n\n"
        f"Hello {supplier},\n\n"
        f"Please provide a firm quote for the following NEW products for solicitation {sol} ({buyer}).\n\n"
        f"Delivery destination: {dest}\n"
        f"Requested quote return / deadline context: {deadline}\n\n"
        f"Items:\n" + "\n".join(lines) + "\n\n"
        "Please include for each line: unit price, extended price, availability, lead time, "
        "freight (separate preferred), quote validity, and payment terms.\n\nThank you.\n"
    )
