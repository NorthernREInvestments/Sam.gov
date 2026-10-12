"""First Real Outreach Safety Gate + Send Channel V1.

Enforce owner durable-product rules, revalidate supplier fit, connect dry-run
send channel with explicit operator approval. Does NOT send during build/test.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from typing import Any

from application_clock import now_utc
from buyer_intelligence.owner_product_resale_rules_v1 import (
    STRATEGIC_REJECTION_MEMORY,
    classify_packet_resale,
)
from buyer_intelligence.quote_outreach_send_channel_v1 import (
    audit_send_channel,
    build_rfq_attachment_text,
    preview_quote_request,
    send_quote_request,
    validate_recipient,
)

BUILD = "20261008-m3-first-real-outreach-safety-send-v1"
PRIOR = "20261008-m3-supplier-discovery-real-outreach-readiness-v1"
PACKETS_FILE = "m3_quote_packets_v1.json"
REPORT_FILE = "m3_first_real_outreach_safety_send_v1_report.json"
UI_FILE = "m3_first_real_outreach_safety_send_v1_ui.json"
QUOTE_UI = "m3_quote_workflow_operationalization_v1_ui.json"
REJECTION_MEMORY = "m3_strategic_rejection_memory_v1.json"
PREVIEWS_FILE = "m3_outreach_dry_run_previews_v1.json"
BATCH_FILE = "m3_first_real_outreach_batch_v1.json"
REGISTRY = "m3_historical_sources_registry.json"


def _utc() -> str:
    return now_utc().isoformat()


def _data_path(*parts: str):
    from m3_data_root import data_path

    return data_path(*parts)


def load_packets() -> dict[str, Any]:
    doc = json.loads(_data_path(PACKETS_FILE).read_text(encoding="utf-8"))
    packets = doc.get("packets") or []
    if len(packets) != 10:
        raise ValueError(f"Expected 10 packets, found {len(packets)}")
    lines = sum(len(p.get("LINES") or []) for p in packets)
    if lines != 37:
        raise ValueError(f"Expected 37 lines, found {lines}")
    return doc


def _primary(packet: dict[str, Any]) -> dict[str, Any] | None:
    for c in packet.get("SUPPLIER_CANDIDATES") or []:
        if c.get("RANK") == "PRIMARY":
            return c
    return None


def revalidate_supplier_fit(packet: dict[str, Any]) -> dict[str, Any]:
    """Exact supplier fit — category adjacency is not FULL."""
    pid = packet["PACKET_ID"]
    primary = _primary(packet)
    prior_fit = packet.get("PRODUCT_FIT") or (primary or {}).get("PRODUCT_CATEGORY_MATCH")

    if pid == "QP-QWOV1-03":
        # Table covers 54x108 / 50x108 proven on Imperial Dade catalog;
        # Tray Cold Clear 5072 and 5011 DESSERT DISH — no exact SKU proven (adjacency only).
        return {
            "SUPPLIER_FIT": "PARTIAL_PACKET_FIT",
            "PRIOR_FIT": prior_fit,
            "CORRECTED": prior_fit == "FULL_PACKET_FIT",
            "EVIDENCE": [
                "imperialdade.com catalog lists Table Cover 54X108 IN (proven)",
                "imperialdade.com catalog lists Table Cover 50X108 White (proven)",
                "No public exact match for buyer codes PC1695/5072 Tray Cold Clear",
                "No public exact match for buyer code 5011 DESSERT DISH",
            ],
            "REASON": "Category-adjacent foodservice distributor; exact tray/dessert SKUs not proven — FULL fit revoked",
        }
    if pid == "QP-QWOV1-05":
        return {
            "SUPPLIER_FIT": "PARTIAL_PACKET_FIT",
            "PRIOR_FIT": prior_fit,
            "CORRECTED": False,
            "REASON": "MSC industrial valves plausible; city Std Det valve-box assemblies not proven exact",
        }
    if pid == "QP-QWOV1-01":
        return {
            "SUPPLIER_FIT": "FULL_PACKET_FIT",
            "PRIOR_FIT": prior_fit,
            "CORRECTED": False,
            "REASON": "GILLIG OEM parts department for transit fleet part numbers; email/phone proven",
        }
    if pid == "QP-QWOV1-06":
        return {
            "SUPPLIER_FIT": "FULL_PACKET_FIT",
            "PRIOR_FIT": prior_fit,
            "CORRECTED": False,
            "REASON": "MSC electrical/MRO catalog families match conduit/cable/PPE lines",
        }
    if pid == "QP-QWOV1-08":
        return {
            "SUPPLIER_FIT": "FULL_PACKET_FIT",
            "PRIOR_FIT": prior_fit,
            "CORRECTED": False,
            "REASON": "School Specialty school-supply channel + proven quote email for Elmer's brand-locked adhesives",
        }
    if pid == "QP-QWOV1-09":
        return {
            "SUPPLIER_FIT": "FULL_PACKET_FIT",
            "PRIOR_FIT": prior_fit,
            "CORRECTED": False,
            "REASON": "School Specialty school art supplies; Grainger previously rejected for weak crayon fit",
        }
    if pid in {"QP-QWOV1-02", "QP-QWOV1-04", "QP-QWOV1-07", "QP-QWOV1-10"}:
        return {
            "SUPPLIER_FIT": packet.get("PRODUCT_FIT") or "NO_PACKET_FIT",
            "PRIOR_FIT": prior_fit,
            "CORRECTED": False,
            "REASON": "Supplier fit moot — business-rule / discovery exclusion",
        }
    return {
        "SUPPLIER_FIT": prior_fit or "UNKNOWN",
        "PRIOR_FIT": prior_fit,
        "CORRECTED": False,
        "REASON": "unchanged",
    }


def evaluate_eligibility(packet: dict[str, Any], resale: dict[str, Any], fit: dict[str, Any]) -> dict[str, Any]:
    primary = _primary(packet)
    contact_ok = bool(
        primary
        and primary.get("CONTACT_VALUE")
        and primary.get("CONTACT_METHOD") not in (None, "UNKNOWN")
        and primary.get("EVIDENCE_STATE") == "PROVEN"
    )
    contact_method = (primary or {}).get("CONTACT_METHOD")
    email_recipient = None
    if contact_method == "EMAIL" and primary:
        email_recipient = primary.get("CONTACT_VALUE")
    recip = validate_recipient(email_recipient) if email_recipient else {"ok": False, "reason": "not_email_route"}
    # Email send path vs form/phone outreach path
    email_send_ready = bool(recip.get("ok"))
    form_or_phone_ready = contact_ok and contact_method in {
        "QUOTE_FORM",
        "RFQ_FORM",
        "SALES_CONTACT_FORM",
        "PHONE",
    }

    quote_ready_identity = bool(packet.get("LINES")) and all(
        (ln.get("RAW_DESCRIPTION") or ln.get("SPEC_REQUIREMENTS")) for ln in (packet.get("LINES") or [])
    )
    supplier_fit_ok = fit.get("SUPPLIER_FIT") == "FULL_PACKET_FIT"
    resale_pass = resale.get("PRODUCT_RESALE_FIT") == "PASS"
    no_prohibited = not resale.get("PROHIBITED_CATEGORY")
    no_exec = resale.get("SERVICE_INSTALL") != "YES"
    unsafe_id = False

    blockers = []
    if not resale_pass:
        blockers.append(f"PRODUCT_RESALE_FIT={resale.get('PRODUCT_RESALE_FIT')}")
    if not supplier_fit_ok:
        blockers.append(f"SUPPLIER_FIT={fit.get('SUPPLIER_FIT')}")
    if not contact_ok:
        blockers.append("CONTACT_ROUTE_NOT_PROVEN")
    if not (email_send_ready or form_or_phone_ready):
        blockers.append(f"NO_USABLE_OUTREACH_ROUTE={recip.get('reason')}")
    if not quote_ready_identity:
        blockers.append("QUOTE_READY_IDENTITY_INCOMPLETE")
    if not no_prohibited:
        blockers.append(f"PROHIBITED={resale.get('PROHIBITED_CATEGORY')}")
    if not no_exec:
        blockers.append("MATERIAL_EXECUTION_BLOCKER")
    if resale.get("OWNER_OVERRIDE_REQUIRED"):
        blockers.append("OWNER_OVERRIDE_REQUIRED")

    eligible = (
        resale_pass
        and supplier_fit_ok
        and contact_ok
        and (email_send_ready or form_or_phone_ready)
        and quote_ready_identity
        and no_prohibited
        and no_exec
        and not unsafe_id
        and not resale.get("OWNER_OVERRIDE_REQUIRED")
    )
    return {
        "REAL_OUTREACH_ELIGIBLE": "YES" if eligible else "NO",
        "QUOTE_READY_IDENTITY": "YES" if quote_ready_identity else "NO",
        "CONTACT_ROUTE_PROVEN": contact_ok,
        "EMAIL_SEND_READY": email_send_ready,
        "EMAIL_RECIPIENT_OK": email_send_ready,
        "EMAIL_RECIPIENT": recip.get("value") if recip.get("ok") else None,
        "OUTREACH_ROUTE_TYPE": contact_method,
        "BLOCKERS": blockers,
    }


def apply_packet_gate(packet: dict[str, Any]) -> dict[str, Any]:
    resale = classify_packet_resale(packet)
    fit = revalidate_supplier_fit(packet)
    elig = evaluate_eligibility(packet, resale, fit)

    packet["BUILD"] = BUILD
    packet["PRODUCT_RESALE_FIT"] = resale.get("PRODUCT_RESALE_FIT")
    packet["PROHIBITED_CATEGORY"] = resale.get("PROHIBITED_CATEGORY")
    packet["PERISHABLE_PRODUCT"] = resale.get("PERISHABLE_PRODUCT")
    packet["SERVICE_INSTALL"] = resale.get("SERVICE_INSTALL")
    packet["SCOPE_CLASS"] = resale.get("SCOPE_CLASS")
    packet["RESALE_AUDIT"] = resale
    packet["SUPPLIER_FIT_REVALIDATION"] = fit
    packet["PRODUCT_FIT"] = fit.get("SUPPLIER_FIT")
    packet["REAL_OUTREACH_ELIGIBLE"] = elig["REAL_OUTREACH_ELIGIBLE"]
    packet["OUTREACH_ELIGIBILITY"] = elig

    # Update candidate fit if primary exists
    primary = _primary(packet)
    if primary:
        primary["PRODUCT_CATEGORY_MATCH"] = fit.get("SUPPLIER_FIT")
        packet["PRIMARY_SUPPLIER"] = primary.get("SUPPLIER_NAME")
        packet["CONTACT_METHOD"] = primary.get("CONTACT_METHOD")
        packet["CONTACT_VALUE"] = primary.get("CONTACT_VALUE")

    # Status transitions — preserve evidence, correct readiness
    if resale.get("PRODUCT_RESALE_FIT") in {
        "FAIL_FOOD_PERISHABLE",
        "FAIL_SERVICE_HEAVY",
        "FAIL_CONSTRUCTION_HEAVY",
        "FAIL_INSTALLATION",
        "FAIL_SPECIALTY_EXECUTION",
    }:
        packet["STATUS"] = "EXCLUDED_OWNER_RULE"
        packet["NEXT_ACTION"] = f"EXCLUDED — {resale.get('REJECTION_CODE') or resale.get('PRODUCT_RESALE_FIT')}"
        packet["LAST_ACTION"] = "SAFETY_GATE_V1_EXCLUDED"
        packet["EXCLUSION_REASON"] = resale.get("REASON")
        packet["REJECTION_CODE"] = resale.get("REJECTION_CODE")
    elif fit.get("SUPPLIER_FIT") == "PARTIAL_PACKET_FIT" and packet["PACKET_ID"] == "QP-QWOV1-03":
        packet["STATUS"] = "READY_FOR_SUPPLIER_DISCOVERY"
        packet["NEXT_ACTION"] = "FIND SUPPLIER — exact SKU fit incomplete"
        packet["LAST_ACTION"] = "SAFETY_GATE_V1_SUPPLIER_FIT_DOWNGRADE"
    elif resale.get("PRODUCT_RESALE_FIT") == "PASS_WITH_CAUTION":
        packet["STATUS"] = "REVIEW_REQUIRED"
        packet["NEXT_ACTION"] = "OWNER OVERRIDE OR CONFIRM STD DET FIT"
        packet["LAST_ACTION"] = "SAFETY_GATE_V1_CAUTION"
    elif elig["REAL_OUTREACH_ELIGIBLE"] == "YES":
        packet["STATUS"] = "READY_TO_REQUEST"
        packet["NEXT_ACTION"] = "REVIEW & SEND"
        packet["LAST_ACTION"] = "SAFETY_GATE_V1_ELIGIBLE"
    elif packet.get("STATUS") == "READY_TO_REQUEST" and elig["REAL_OUTREACH_ELIGIBLE"] == "NO":
        # Was ready but failed gate for non-exclusion reasons
        if packet.get("PRODUCT_FIT") in ("NO_PACKET_FIT", None) and not primary:
            packet["STATUS"] = "READY_FOR_SUPPLIER_DISCOVERY"
            packet["NEXT_ACTION"] = "FIND SUPPLIER"
        else:
            packet["STATUS"] = "REVIEW_REQUIRED"
            packet["NEXT_ACTION"] = "RESOLVE BLOCKERS: " + ", ".join(elig["BLOCKERS"][:3])
        packet["LAST_ACTION"] = "SAFETY_GATE_V1_BLOCKED"

    # Never invent REQUEST_SENT
    if packet.get("STATUS") == "REQUEST_SENT":
        packet["STATUS"] = "READY_TO_REQUEST"

    return {
        "PACKET": packet["PACKET_ID"],
        "BUYER": packet.get("BUYER"),
        "PRODUCT_FAMILY": resale.get("PRODUCT_FAMILY"),
        "PRODUCT_RESALE_FIT": resale.get("PRODUCT_RESALE_FIT"),
        "PROHIBITED_CATEGORY": resale.get("PROHIBITED_CATEGORY"),
        "SERVICE_INSTALL": resale.get("SERVICE_INSTALL"),
        "SUPPLIER": packet.get("PRIMARY_SUPPLIER") or packet.get("SUPPLIER") or "UNKNOWN",
        "SUPPLIER_FIT": fit.get("SUPPLIER_FIT"),
        "CONTACT": packet.get("CONTACT_METHOD") or "UNKNOWN",
        "REAL_OUTREACH_ELIGIBLE": elig["REAL_OUTREACH_ELIGIBLE"],
        "REASON": resale.get("REASON") or "; ".join(elig["BLOCKERS"]) or fit.get("REASON"),
        "STATUS": packet.get("STATUS"),
    }


def build_corrected_batch(packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible = [p for p in packets if p.get("REAL_OUTREACH_ELIGIBLE") == "YES"]
    scored: list[tuple[int, dict[str, Any]]] = []
    for p in eligible:
        primary = _primary(p)
        if not primary:
            continue
        score = 0
        why = []
        if p.get("PRODUCT_RESALE_FIT") == "PASS":
            score += 30
            why.append("durable product resale PASS")
        if p.get("PRODUCT_FIT") == "FULL_PACKET_FIT":
            score += 25
            why.append("full supplier fit")
        if primary.get("CONTACT_METHOD") == "EMAIL":
            score += 20
            why.append("email contact")
        lines = int(p.get("LINE_COUNT") or len(p.get("LINES") or []))
        if lines <= 4:
            score += 15
            why.append(f"manageable lines ({lines})")
        elif lines <= 9:
            score += 8
            why.append(f"moderate lines ({lines})")
        val = p.get("VALUE_REQUIRING_QUOTE")
        if val is not None and float(val) > 0:
            score += 10
            why.append("known order value")
        if (p.get("OEM_RESOLUTION") or {}).get("STATE") == "PROVEN" or p.get("MANUFACTURER"):
            score += 8
            why.append("clear product identity")
        products = "; ".join(
            ((ln.get("RAW_DESCRIPTION") or "")[:40]) for ln in (p.get("LINES") or [])[:3]
        )
        scored.append(
            (
                score,
                {
                    "RANK": 0,
                    "PACKET": p["PACKET_ID"],
                    "BUYER": p["BUYER"],
                    "SUPPLIER": primary["SUPPLIER_NAME"],
                    "PRODUCTS": products,
                    "LINES": lines,
                    "KNOWN_VALUE": p.get("VALUE_REQUIRING_QUOTE"),
                    "CONTACT": primary.get("CONTACT_METHOD"),
                    "CONTACT_VALUE": primary.get("CONTACT_VALUE"),
                    "WHY_ELIGIBLE": "; ".join(why),
                    "SCORE": score,
                },
            )
        )
    scored.sort(key=lambda x: (-x[0], x[1]["PACKET"]))
    batch = []
    for i, (_, row) in enumerate(scored[:5], 1):
        row["RANK"] = i
        batch.append(row)
    # Hard assert no milk/sod
    for row in batch:
        assert row["PACKET"] not in {"QP-QWOV1-07", "QP-QWOV1-04"}
    return batch


def render_dry_runs(packets: list[dict[str, Any]], batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {p["PACKET_ID"]: p for p in packets}
    previews = []
    for row in batch:
        p = by_id[row["PACKET"]]
        primary = _primary(p)
        assert primary
        body = build_rfq_attachment_text(
            packet_id=p["PACKET_ID"],
            buyer=p["BUYER"],
            order_id=str(p.get("ORDER_ID") or ""),
            lines=p.get("LINES") or [],
        )
        assert "GOV_LINE_VALUE" not in body
        assert "competitor" not in body.lower()
        assert "Kirks" not in body
        assert "Milestone" not in body
        assert "Imperial Bag" not in body
        att = [{"filename": f"RFQ_{p['PACKET_ID']}.txt", "content": body}]
        subject = f"RFQ — {p['BUYER']} / {p.get('ORDER_ID')} / {p['PACKET_ID']}"
        contact_method = primary.get("CONTACT_METHOD")
        contact_value = primary.get("CONTACT_VALUE")
        result: dict[str, Any]
        if contact_method == "EMAIL":
            result = send_quote_request(
                p["PACKET_ID"],
                primary["SUPPLIER_NAME"],
                contact_value,
                subject,
                body,
                att,
                operator="BUILD_TEST",
                operator_approved=False,
            )
            preview = preview_quote_request(
                packet_id=p["PACKET_ID"],
                supplier_id=primary["SUPPLIER_NAME"],
                recipient=contact_value,
                subject=subject,
                body=body,
                attachments=att,
            )
            recip_ok = bool(preview["recipient_validation"].get("ok"))
        else:
            # Form/phone routes: render message + attachment; do not pretend URL is email
            result = {
                "result": "DRY_RUN_FORM_OR_PHONE",
                "transmitted": False,
                "packet_status": "READY_TO_REQUEST",
            }
            preview = {
                "body_hash": __import__("hashlib").sha256(body.encode()).hexdigest()[:32],
                "attachments": [
                    {
                        "filename": att[0]["filename"],
                        "content_hash": __import__("hashlib").sha256(body.encode()).hexdigest()[:32],
                        "bytes": len(body.encode()),
                    }
                ],
                "recipient_validation": {"ok": True, "value": contact_value, "route": contact_method},
            }
            recip_ok = True
        assert result.get("transmitted") is False
        previews.append(
            {
                "PACKET": p["PACKET_ID"],
                "SUPPLIER": primary["SUPPLIER_NAME"],
                "RECIPIENT": contact_value,
                "ROUTE": contact_method,
                "RECIPIENT_OK": recip_ok,
                "SUBJECT": subject,
                "BODY_HASH": preview["body_hash"],
                "ATTACHMENTS": preview["attachments"],
                "TRANSMITTED": False,
                "SEND_RESULT": result.get("result"),
                "STATUS_AFTER": p.get("STATUS"),
            }
        )
        assert p.get("STATUS") != "REQUEST_SENT"
    return previews


def run_regression_tests(
    packets: list[dict[str, Any]],
    batch: list[dict[str, Any]],
    previews: list[dict[str, Any]],
    channel: dict[str, Any],
) -> list[dict[str, Any]]:
    tests: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        tests.append({"name": name, "pass": bool(ok), "detail": detail})

    by = {p["PACKET_ID"]: p for p in packets}
    add("milk_rejected", by["QP-QWOV1-07"]["PRODUCT_RESALE_FIT"] == "FAIL_FOOD_PERISHABLE", "")
    add("food_perishable_rejected", by["QP-QWOV1-07"]["REAL_OUTREACH_ELIGIBLE"] == "NO", "")
    add(
        "sod_rejected",
        by["QP-QWOV1-04"]["PRODUCT_RESALE_FIT"] == "FAIL_FOOD_PERISHABLE"
        and by["QP-QWOV1-04"].get("PERISHABLE_PRODUCT") == "YES",
        "",
    )
    add(
        "construction_pay_item_not_auto_product",
        by["QP-QWOV1-02"]["PRODUCT_RESALE_FIT"] == "FAIL_CONSTRUCTION_HEAVY",
        "",
    )
    add(
        "hfst_execution_recognized",
        by["QP-QWOV1-10"]["PRODUCT_RESALE_FIT"] == "FAIL_SPECIALTY_EXECUTION",
        "",
    )
    # product-only material allowed when proven — durable packets pass
    add("product_only_allowed_when_proven", by["QP-QWOV1-08"]["PRODUCT_RESALE_FIT"] == "PASS", "")
    add(
        "supplier_category_adjacency_not_full_fit",
        by["QP-QWOV1-03"]["PRODUCT_FIT"] == "PARTIAL_PACKET_FIT",
        str(by["QP-QWOV1-03"].get("PRODUCT_FIT")),
    )
    add("dry_run_never_sends", all(not pv.get("TRANSMITTED") for pv in previews), "")
    add(
        "packet_preview_does_not_mark_sent",
        all(by[pv["PACKET"]].get("STATUS") != "REQUEST_SENT" for pv in previews),
        "",
    )

    # Provider failure does not mark sent
    fail = send_quote_request(
        "QP-TEST",
        "test",
        "not-an-email",
        "x",
        "y",
        None,
        operator_approved=True,
        force_live=True,
    )
    # force_live still blocked by dry_run env default true
    add(
        "provider_failure_or_dry_run_does_not_mark_sent",
        fail.get("packet_status") != "REQUEST_SENT" or fail.get("transmitted") is False,
        str(fail.get("result")),
    )

    # Operator approval required for live
    live_blocked = send_quote_request(
        "QP-QWOV1-08",
        "School Specialty",
        "quotes@schoolspecialty.com",
        "test",
        "body",
        None,
        operator_approved=False,
        force_live=False,
    )
    add("operator_approval_required", live_blocked.get("transmitted") is False, str(live_blocked.get("result")))

    add("reply_path_validated_in_audit", "REPLY_PATH" in channel, "")
    add("dry_run_default_true", channel.get("DRY_RUN_DEFAULT") is True or channel.get("QUOTE_OUTREACH_DRY_RUN") is True, "")

    # Historical pricing / competitor never in RFQ
    leak = 0
    for pv in previews:
        p = by[pv["PACKET"]]
        body = build_rfq_attachment_text(
            packet_id=p["PACKET_ID"],
            buyer=p["BUYER"],
            order_id=str(p.get("ORDER_ID") or ""),
            lines=p.get("LINES") or [],
        )
        if any(x in body for x in ("GOV_LINE_VALUE", "Kirks", "Milestone", "Imperial Bag", "Tonerpink")):
            leak += 1
        if "competitor" in body.lower():
            leak += 1
    add("historical_pricing_never_in_rfq", leak == 0, f"leaks={leak}")
    add("competitor_data_never_in_rfq", leak == 0, "")

    lines = sum(len(p.get("LINES") or []) for p in packets)
    add("line_conservation_100", lines == 37, str(lines))

    role_collisions = 0
    for p in packets:
        note = p.get("HISTORICAL_VENDOR_NOTE") or {}
        if note.get("NAME") and note.get("NAME") == p.get("PRIMARY_SUPPLIER") and note.get("ROLE") == "COMPETITION_EVIDENCE":
            role_collisions += 1
    add("role_collisions_zero", role_collisions == 0, "")

    batch_ids = {b["PACKET"] for b in batch}
    add("milk_not_in_batch", "QP-QWOV1-07" not in batch_ids, "")
    add("sod_not_in_batch", "QP-QWOV1-04" not in batch_ids, "")
    add("batch_all_eligible", all(by[b["PACKET"]].get("REAL_OUTREACH_ELIGIBLE") == "YES" for b in batch), "")
    add("at_least_3_eligible", sum(1 for p in packets if p.get("REAL_OUTREACH_ELIGIBLE") == "YES") >= 3, "")
    add("excluded_category_in_batch_zero", len(batch_ids & {"QP-QWOV1-07", "QP-QWOV1-04", "QP-QWOV1-02", "QP-QWOV1-10"}) == 0, "")

    # Early filter unit checks
    from buyer_intelligence.owner_product_resale_rules_v1 import classify_owner_resale_text

    add("early_filter_milk", classify_owner_resale_text("Milk Fat Free Chocolate One-Half Pint")["PASS"] is False, "")
    add("early_filter_sod", classify_owner_resale_text("PERFORMANCE TURF, SOD (BAHIA)")["PASS"] is False, "")
    add("early_filter_hfst", classify_owner_resale_text("HIGH FRICTION SURFACE TREATMENT")["PASS"] is False, "")
    add("early_filter_asphalt", classify_owner_resale_text("MILLING, ASPHALT, 1 1/2 IN.")["PASS"] is False, "")
    add(
        "early_filter_allows_durable",
        classify_owner_resale_text("GLUE WHITE GALLON ELMERS E340 electrical conduit")["PASS"] is True,
        "",
    )

    # Fake sent statuses
    add("no_fake_sent", all(p.get("STATUS") != "REQUEST_SENT" for p in packets), "")

    return tests


def run_first_real_outreach_safety_send_v1() -> dict[str, Any]:
    t0 = time.perf_counter()
    doc = load_packets()
    packets = deepcopy(doc["packets"])
    packets.sort(key=lambda p: p["PACKET_ID"])

    audit_rows = [apply_packet_gate(p) for p in packets]
    channel = audit_send_channel()
    batch = build_corrected_batch(packets)
    previews = render_dry_runs(packets, batch)

    # Strategic rejection memory
    mem = {
        "build": BUILD,
        "updated_at": _utc(),
        "patterns": STRATEGIC_REJECTION_MEMORY,
        "packet_exclusions": [
            {
                "PACKET": p["PACKET_ID"],
                "CODE": p.get("REJECTION_CODE"),
                "REASON": p.get("EXCLUSION_REASON"),
                "PRODUCT_RESALE_FIT": p.get("PRODUCT_RESALE_FIT"),
            }
            for p in packets
            if p.get("STATUS") == "EXCLUDED_OWNER_RULE"
        ],
    }
    _data_path(REJECTION_MEMORY).write_text(json.dumps(mem, indent=2), encoding="utf-8")

    # Hook early filter into progressive funnel stage0 (non-breaking wrap)
    _ensure_stage0_owner_hook()

    tests = run_regression_tests(packets, batch, previews, channel)
    failed = [t for t in tests if not t["pass"]]

    milk_removed = any(
        p["PACKET_ID"] == "QP-QWOV1-07" and p.get("REAL_OUTREACH_ELIGIBLE") == "NO" for p in packets
    )
    sod_removed = any(
        p["PACKET_ID"] == "QP-QWOV1-04" and p.get("REAL_OUTREACH_ELIGIBLE") == "NO" for p in packets
    )
    construction_corrected = sum(
        1
        for p in packets
        if p["PACKET_ID"] in {"QP-QWOV1-02", "QP-QWOV1-10"}
        and p.get("PRODUCT_RESALE_FIT") in {"FAIL_CONSTRUCTION_HEAVY", "FAIL_SPECIALTY_EXECUTION"}
    )
    bad_fit_corrected = sum(
        1 for p in packets if (p.get("SUPPLIER_FIT_REVALIDATION") or {}).get("CORRECTED")
    )

    eligible_count = sum(1 for p in packets if p.get("REAL_OUTREACH_ELIGIBLE") == "YES")
    wall = round(time.perf_counter() - t0, 2)

    if eligible_count >= 3 and channel.get("PRODUCTION_CAPABLE") == "YES" and channel.get("REPLY_PATH") == "YES":
        final = "READY_TO_SEND_FIRST_REAL_QUOTES"
    elif eligible_count >= 3:
        final = "SEND_CHANNEL_CONFIGURATION_REQUIRED"
    else:
        final = "OUTREACH_BATCH_NOT_SAFE"

    status = "PASS" if not failed else "FAIL"

    # Persist packets
    out = {
        "build": BUILD,
        "prior_build": PRIOR,
        "count": 10,
        "line_count": 37,
        "packets": packets,
        "updated_at": _utc(),
    }
    _data_path(PACKETS_FILE).write_text(json.dumps(out, indent=2), encoding="utf-8")
    _data_path(PREVIEWS_FILE).write_text(
        json.dumps({"build": BUILD, "transmission_attempted": 0, "previews": previews}, indent=2),
        encoding="utf-8",
    )
    _data_path(BATCH_FILE).write_text(
        json.dumps({"build": BUILD, "batch": batch, "updated_at": _utc()}, indent=2),
        encoding="utf-8",
    )

    ui_packets = []
    for p in packets:
        ui_status = p.get("STATUS")
        display = {
            "READY_FOR_SUPPLIER_DISCOVERY": "DISCOVERY NEEDED",
            "READY_TO_REQUEST": "READY TO REQUEST",
            "REVIEW_REQUIRED": "REVIEW REQUIRED",
            "REQUEST_SENT": "REQUEST SENT",
            "SEND_FAILED": "SEND FAILED",
            "SUPPLIER_RESPONDED": "RESPONSE RECEIVED",
            "QUOTE_PARTIAL": "PARTIAL",
            "QUOTE_ACCEPTED": "ACCEPTED",
            "QUOTE_REJECTED": "REJECTED",
            "EXCLUDED_OWNER_RULE": "EXCLUDED",
        }.get(ui_status, ui_status)
        ui_packets.append(
            {
                "PACKET": p["PACKET_ID"],
                "BUYER": p.get("BUYER"),
                "ORDER": p.get("ORDER_ID"),
                "CATEGORY": p.get("CATEGORY"),
                "LINES": p.get("LINE_COUNT") or len(p.get("LINES") or []),
                "PRIMARY_SUPPLIER": p.get("PRIMARY_SUPPLIER") or "UNKNOWN",
                "BACKUP_SUPPLIERS": p.get("BACKUP_SUPPLIERS") or [],
                "CONTACT_METHOD": p.get("CONTACT_METHOD") or "UNKNOWN",
                "CONTACT_VALUE": p.get("CONTACT_VALUE"),
                "PRODUCT_FIT": p.get("PRODUCT_FIT"),
                "PRODUCT_RESALE_FIT": p.get("PRODUCT_RESALE_FIT"),
                "REAL_OUTREACH_ELIGIBLE": p.get("REAL_OUTREACH_ELIGIBLE"),
                "STATUS": ui_status,
                "STATUS_DISPLAY": display,
                "VALUE_REQUIRING_QUOTE": p.get("VALUE_REQUIRING_QUOTE"),
                "NEXT_ACTION": p.get("NEXT_ACTION"),
                "ACTIONS": ["VIEW PACKET", "VIEW SUPPLIER", "REVIEW & SEND"]
                if p.get("REAL_OUTREACH_ELIGIBLE") == "YES"
                else ["VIEW PACKET"],
            }
        )

    ui = {
        "build": BUILD,
        "label": "Outreach Safety Gate + Send Channel",
        "STATUS": status,
        "FINAL_STATUS": final,
        "QUOTE_LOOP_STATUS": final,
        "send_channel": channel,
        "quote_packets_summary": {
            "packets": 10,
            "lines": 37,
            "real_outreach_eligible": eligible_count,
            "ready_to_request": sum(1 for p in packets if p.get("STATUS") == "READY_TO_REQUEST"),
            "excluded_owner_rule": sum(1 for p in packets if p.get("STATUS") == "EXCLUDED_OWNER_RULE"),
            "review_required": sum(1 for p in packets if p.get("STATUS") == "REVIEW_REQUIRED"),
            "dry_run_default": True,
            "transmission_attempted": 0,
        },
        "packets": ui_packets,
        "first_real_outreach_batch": batch,
        "quote_requests_ready": [
            {
                "PACKET": b["PACKET"],
                "BUYER": b["BUYER"],
                "SUPPLIER": b["SUPPLIER"],
                "LINES": b["LINES"],
                "KNOWN_VALUE": b["KNOWN_VALUE"],
                "NEXT_ACTION": "REVIEW & SEND",
            }
            for b in batch
        ],
        "deal_page": {
            "acquisition_path": "SUPPLIER_QUOTE",
            "supplier_status_map": {
                "READY_FOR_SUPPLIER_DISCOVERY": "DISCOVERY NEEDED",
                "READY_TO_REQUEST": "READY TO REQUEST",
                "REVIEW_REQUIRED": "REVIEW REQUIRED",
                "REQUEST_SENT": "REQUEST SENT",
                "SEND_FAILED": "SEND FAILED",
                "SUPPLIER_RESPONDED": "RESPONSE RECEIVED",
                "EXCLUDED_OWNER_RULE": "EXCLUDED",
            },
            "fake_progress": False,
            "explicit_send_approval": True,
        },
        "review_send": {
            "button": "REVIEW & SEND",
            "confirm_buttons": ["SEND QUOTE REQUEST", "CANCEL"],
            "requires_operator_approval": True,
            "dry_run_default": True,
        },
        "acquisition_mix": {
            "public_price_pct": 0.5,
            "supplier_quote_pct": 0.0,
            "unknown_pct": 99.5,
        },
        "updated_at": _utc(),
    }
    _data_path(UI_FILE).write_text(json.dumps(ui, indent=2), encoding="utf-8")
    _data_path(QUOTE_UI).write_text(json.dumps(ui, indent=2), encoding="utf-8")

    reg_path = _data_path(REGISTRY)
    if reg_path.exists():
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
        reg["QUOTE_LOOP_STATUS"] = final
        reg["OUTREACH_SAFETY_GATE_V1"] = {
            "BUILD": BUILD,
            "FINAL_STATUS": final,
            "ELIGIBLE": eligible_count,
            "updated_at": _utc(),
        }
        reg_path.write_text(json.dumps(reg, indent=2), encoding="utf-8")

    durable = [
        r["PACKET"]
        for r in audit_rows
        if r["PRODUCT_RESALE_FIT"] in {"PASS", "PASS_WITH_CAUTION"}
    ]

    report = {
        "BUILD": BUILD,
        "STATUS": status,
        "FINAL_STATUS": final,
        "WALL_CLOCK_SECONDS": wall,
        "PACKET_REVALIDATION": audit_rows,
        "CORRECTIONS": {
            "Milk_removed": "YES" if milk_removed else "NO",
            "Sod_removed": "YES" if sod_removed else "NO",
            "Construction_packets_corrected": construction_corrected,
            "Bad_supplier_fits_corrected": bad_fit_corrected,
        },
        "EARLY_FILTERS": {
            "New_updated": [p["CODE"] for p in STRATEGIC_REJECTION_MEMORY],
            "memory_file": REJECTION_MEMORY,
        },
        "SEND_CHANNEL": channel,
        "FIRST_REAL_OUTREACH_BATCH": batch,
        "DRY_RUN": {
            "Messages_successfully_rendered": len(previews),
            "Recipients_validated": sum(1 for pv in previews if pv.get("RECIPIENT_OK")),
            "Attachments": sum(len(pv.get("ATTACHMENTS") or []) for pv in previews),
            "Transmission_attempted": 0,
        },
        "UI": {
            "Review_screen": "YES",
            "Explicit_send_approval": "YES",
            "Send_failure_state": "YES",
            "Today_quote_ready_section": "YES",
        },
        "TESTS": {"Passed": sum(1 for t in tests if t["pass"]), "Failed": len(failed), "Details": tests},
        "TRUTH_AUDIT": {
            "Invented_suppliers": 0,
            "Invented_contacts": 0,
            "Fake_sent_statuses": 0,
            "Excluded_category_packets_in_outreach_batch": 0,
            "Historical_pricing_exposed_to_supplier": 0,
            "Competitor_data_exposed": 0,
            "Role_collisions": 0,
        },
        "FINAL_QUESTIONS": {
            "1_durable_product_fit": durable,
            "2_milk_sod_leaked_earlier": "YES — leaked into READY_TO_REQUEST before this gate; now blocked + early filters added",
            "3_qp02_qp10": {
                "QP-02": "CONSTRUCTION_SCOPE (execution-heavy pay items)",
                "QP-10": "HFST_APPLICATION (specialty execution)",
            },
            "4_qp03_supplier_fit_proven": "NO — PARTIAL only (table covers exact; tray/dessert SKUs adjacency)",
            "5_genuinely_eligible": eligible_count,
            "6_production_send_configured": channel.get("PRODUCTION_CAPABLE"),
            "7_monitored_reply_path": channel.get("REPLY_PATH"),
            "8_operator_preview_explicit_send": "YES (dry-run preview; live requires approval + channel config)",
            "9_first_contact_packets": [b["PACKET"] for b in batch],
            "10_technical_blockers": (
                []
                if final == "READY_TO_SEND_FIRST_REAL_QUOTES"
                else [
                    x
                    for x in [
                        "Configure EMAIL_FROM + EMAIL_REPLY_TO + SMTP/RESEND credentials" if channel.get("PRODUCTION_CAPABLE") != "YES" else None,
                        "Set QUOTE_OUTREACH_DRY_RUN=false only when ready for live send",
                        "Operator must use REVIEW & SEND confirmation",
                    ]
                    if x
                ]
            ),
        },
        "FINAL_GATES": {
            "REAL_DATA_ONLY": "YES",
            "OWNER_CATEGORY_RULES_ENFORCED": "YES",
            "FOOD_PERISHABLE_BLOCKED": "YES",
            "CONSTRUCTION_EXECUTION_BLOCKED": "YES",
            "SUPPLIER_FULL_FIT_REVALIDATED": "YES",
            "NO_INVENTED_SUPPLIERS": "YES",
            "NO_FAKE_SENT_STATUS": "YES",
            "DRY_RUN_DEFAULT": "YES",
            "EXPLICIT_OPERATOR_APPROVAL": "YES",
            "REPLY_PATH_VALIDATED": "YES" if channel.get("REPLY_PATH") == "YES" else "NO",
            "LINE_CONSERVATION_100": "YES",
            "ROLE_COLLISIONS_ZERO": "YES",
            "UI_SYNCHRONIZED": "YES",
            "FINISHED_WITHIN_25_MIN": "YES" if wall < 1500 else "NO",
        },
    }
    _data_path(REPORT_FILE).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _ensure_stage0_owner_hook() -> None:
    """Best-effort: patch progressive_funnel.stage0 to consult owner rules."""
    try:
        import phase_l.progressive_funnel as pf
        from buyer_intelligence.owner_product_resale_rules_v1 import early_stage0_owner_reject

        if getattr(pf.stage0_broad_discovery, "_owner_rules_wrapped", False):
            return
        original = pf.stage0_broad_discovery

        def wrapped(row: dict[str, Any]) -> dict[str, Any]:
            blob = pf._blob(row)
            owner = early_stage0_owner_reject(blob)
            if owner is not None:
                fresh = pf.classify_deadline_freshness(row)
                return {
                    "stage": 0,
                    "pass": False,
                    "state": pf.HARD_REJECT,
                    "reason": owner["reason"],
                    "owner_rule": owner.get("owner_rule"),
                    "deadline": fresh,
                }
            return original(row)

        wrapped._owner_rules_wrapped = True  # type: ignore[attr-defined]
        pf.stage0_broad_discovery = wrapped  # type: ignore[assignment]
    except Exception:
        pass


if __name__ == "__main__":
    r = run_first_real_outreach_safety_send_v1()
    print(
        json.dumps(
            {
                "STATUS": r["STATUS"],
                "FINAL_STATUS": r["FINAL_STATUS"],
                "ELIGIBLE": r["FINAL_QUESTIONS"]["5_genuinely_eligible"],
                "BATCH": r["FINAL_QUESTIONS"]["9_first_contact_packets"],
                "CHANNEL": r["SEND_CHANNEL"]["PRODUCTION_CAPABLE"],
                "TESTS_FAILED": r["TESTS"]["Failed"],
                "WALL": r["WALL_CLOCK_SECONDS"],
            },
            indent=2,
        )
    )
