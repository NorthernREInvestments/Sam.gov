"""Phase 5 + 10 + 14 — Target economics, quote request text, outreach re-rank."""

from __future__ import annotations

import json
from typing import Any

from final_pre_scale_proof.models import (
    DANIA_OID,
    FINANCING_RESERVE_PCT,
    FREIGHT_RESERVE_PCT,
    INSTALL_RESERVE_PCT,
    PRIOR_DC_PACKETS,
)
from m3_data_root import data_path


def dania_target_economics(*, revenue_validation: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    """Only defensible envelopes — NOT_READY if product-scope revenue cannot be separated."""
    if not scope.get("economics_target_ready"):
        return {
            "opportunity_id": DANIA_OID,
            "MAX_PRODUCT_ACQUISITION_FOR_$5K": None,
            "MAX_PRODUCT_ACQUISITION_FOR_$10K": None,
            "MAX_PRODUCT_ACQUISITION_FOR_15_PERCENT": None,
            "MAX_PRODUCT_ACQUISITION_FOR_20_PERCENT": None,
            "Freight_reserve": FREIGHT_RESERVE_PCT,
            "Financing_reserve": FINANCING_RESERVE_PCT,
            "Install_reserve_if_applicable": INSTALL_RESERVE_PCT,
            "Economics_target_valid": "NO",
            "reason": scope.get("reason") or "product-scope revenue not separable from grant total",
            "program_funding_noted": (revenue_validation.get("corrected") or {}).get("value"),
            "prior_invalid_ceilings_discarded": {
                "5k": 349557.52,
                "10k": 345132.74,
                "note": "Prior deep-completion ceilings assumed full $400K product revenue — INVALID",
            },
        }

    # Unreachable until scope separates product value
    rev = float(scope["PRODUCT_SCOPE_VALUE"])
    freight = FREIGHT_RESERVE_PCT
    fin = FINANCING_RESERVE_PCT
    install = INSTALL_RESERVE_PCT if revenue_validation.get("install_service_included") else 0.0
    reserve = freight + fin + install

    def max_acq(profit: float | None = None, margin: float | None = None) -> float:
        if margin is not None:
            # revenue * (1-margin) * (1-reserve) roughly: keep acquisition under net after margin+reserves
            return round(rev * (1.0 - margin) * (1.0 - reserve), 2)
        assert profit is not None
        return round((rev - profit) * (1.0 - reserve), 2)

    return {
        "opportunity_id": DANIA_OID,
        "MAX_PRODUCT_ACQUISITION_FOR_$5K": max_acq(profit=5000),
        "MAX_PRODUCT_ACQUISITION_FOR_$10K": max_acq(profit=10000),
        "MAX_PRODUCT_ACQUISITION_FOR_15_PERCENT": max_acq(margin=0.15),
        "MAX_PRODUCT_ACQUISITION_FOR_20_PERCENT": max_acq(margin=0.20),
        "Freight_reserve": freight,
        "Financing_reserve": fin,
        "Install_reserve_if_applicable": install,
        "Economics_target_valid": "YES",
    }


def generate_quote_request_texts() -> dict[str, Any]:
    packets = json.loads(data_path(PRIOR_DC_PACKETS).read_text(encoding="utf-8")).get("packets") or []
    texts = []
    for p in packets:
        supplier = (p.get("supplier") or {}).get("supplier_name") or "Supplier"
        oid = p.get("opportunity_id") or ""
        sol = p.get("solicitation_id") or oid.split(":")[-1]
        buyer = p.get("buyer") or ""
        dest = (p.get("request") or {}).get("delivery_destination") or "[DELIVERY DESTINATION TBD]"
        deadline = p.get("requested_quote_return_date") or p.get("deadline") or "[RETURN DATE TBD — please confirm]"
        lines = p.get("lines") or []
        line_block = []
        for i, ln in enumerate(lines, 1):
            mfr = ln.get("manufacturer") or ""
            mpn = ln.get("mpn") or ""
            desc = (ln.get("description") or "")[:120]
            qty = ln.get("qty")
            uom = ln.get("uom") or "EA"
            line_block.append(
                f"{i}. {mfr} {mpn} — {desc}\n   Qty: {qty} {uom} | Condition: NEW | Pack: {ln.get('pack') or 1}"
            )
        body = (
            f"Subject: Quote request — {buyer} solicitation {sol} / packet {p.get('packet_id')}\n\n"
            f"Hello {supplier},\n\n"
            f"Please provide a firm quote for the following NEW products for solicitation {sol} "
            f"({buyer}).\n\n"
            f"Delivery destination: {dest}\n"
            f"Requested quote return date: {deadline}\n\n"
            f"Items:\n" + "\n".join(line_block) + "\n\n"
            "Please include for each line:\n"
            "- Unit price\n"
            "- Extended price\n"
            "- Availability\n"
            "- Lead time\n"
            "- Freight (separate line preferred)\n"
            "- Quote validity period\n"
            "- Payment terms\n\n"
            "Thank you.\n"
        )
        texts.append(
            {
                "packet_id": p.get("packet_id"),
                "opportunity_id": oid,
                "supplier": supplier,
                "domain": (p.get("supplier") or {}).get("domain"),
                "line_count": len(lines),
                "copy_ready_text": body,
                "do_not_send_automatically": True,
            }
        )
    return {"count": len(texts), "requests": texts}


def rerank_outreach(
    *,
    packet_audit: dict[str, Any],
    dania_rev: dict[str, Any],
    bridgeport_rev: dict[str, Any],
    go_metro_rev: dict[str, Any],
    dekalb_rev: dict[str, Any],
    dania_economics: dict[str, Any],
) -> list[dict[str, Any]]:
    """Re-rank from evidence — not prior assumptions."""
    audited = {a["packet_id"]: a for a in packet_audit.get("packets") or []}
    packets = json.loads(data_path(PRIOR_DC_PACKETS).read_text(encoding="utf-8")).get("packets") or []

    def rev_conf(oid: str) -> tuple[str, float]:
        if oid == DANIA_OID:
            # Grant total exists but product scope not ready
            return ("LOW_PROGRAM_FUNDING_ONLY", 0.2 if dania_rev.get("PASS_FAIL") == "FAIL" else 0.5)
        if oid == "opengov:bridgeportct:299806":
            ok = bridgeport_rev.get("PASS_FAIL") == "PASS"
            return ("HIGH" if ok else "NONE", 0.8 if ok else 0.0)
        if oid == "opengov:go-metro:298984":
            ok = go_metro_rev.get("PASS_FAIL") == "PASS"
            return ("MEDIUM" if ok else "CHANNEL_ONLY", 0.5 if ok else 0.15)
        if oid == "opengov:dekalbcountyga:286698":
            ok = dekalb_rev.get("PASS_FAIL") == "PASS"
            return ("MEDIUM" if ok else "CHANNEL_ONLY", 0.5 if ok else 0.15)
        return ("LOW", 0.1)

    def simplicity(oid: str, line_count: int) -> float:
        if oid.endswith("298984"):
            return 1.0  # single Cummins packet
        if oid.endswith("286698"):
            return 0.85  # consolidated EMS
        if oid.endswith("299806"):
            return 0.6
        if oid.endswith("284328"):
            return 0.4 if dania_economics.get("Economics_target_valid") != "YES" else 0.7
        return 0.2

    ranked = []
    for p in packets:
        oid = p.get("opportunity_id") or ""
        if oid.endswith("295143"):
            continue  # Collier parked
        aud = audited.get(p.get("packet_id") or "") or {}
        conf_label, conf_score = rev_conf(oid)
        lines_n = int(p.get("line_count") or 0)
        simp = simplicity(oid, lines_n)
        owner_ready = bool(aud.get("owner_ready_after_audit"))
        # Score: revenue * simplicity * coverage impact * readiness
        score = conf_score * 40 + simp * 30 + min(lines_n, 50) * 0.4 + (20 if owner_ready else 0)
        # Channel-proof still useful but lower than revenue-ready
        if conf_label == "CHANNEL_ONLY":
            role = "SUPPLIER_CHANNEL_TEST"
        elif conf_label == "NONE":
            role = "HOLD_REVENUE_NOT_READY"
        elif conf_label.startswith("LOW"):
            role = "HOLD_PRODUCT_REVENUE_NOT_SEPARABLE"
        else:
            role = "PRIMARY_OUTREACH"

        status = "READY" if owner_ready and conf_score >= 0.5 else "NOT_READY"
        if conf_label == "CHANNEL_ONLY" and owner_ready:
            status = "READY_AS_CHANNEL_TEST"

        ranked.append(
            {
                "score": round(score, 2),
                "Priority": None,  # filled after sort
                "Opportunity": oid,
                "Supplier": (p.get("supplier") or {}).get("supplier_name"),
                "packet_id": p.get("packet_id"),
                "Lines": lines_n,
                "Revenue_confidence": conf_label,
                "Expected_impact": role,
                "Deadline": p.get("deadline") or "UNKNOWN",
                "READY_NOT_READY": status,
                "audit_status": aud.get("status"),
                "do_not_send_automatically": True,
            }
        )

    ranked.sort(key=lambda r: -r["score"])
    for i, r in enumerate(ranked, 1):
        if r["READY_NOT_READY"] == "READY":
            r["Priority"] = "P0"
        elif r["READY_NOT_READY"] == "READY_AS_CHANNEL_TEST":
            r["Priority"] = "P1"
        else:
            r["Priority"] = "P2" if i <= 8 else "P3"
    return ranked
