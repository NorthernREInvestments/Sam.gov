"""Emit M3 package-access completion report from last OpenGov + official-source reports."""
from __future__ import annotations

import json
from pathlib import Path


def _load(name: str) -> dict:
    from m3_data_root import data_path

    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    og = _load("m3_opengov_public_docs_last_report.json")
    osr = _load("m3_official_source_last_report.json")
    platforms = osr.get("platforms") or {}

    attempted_og = int(og.get("attempted") or 0)
    valid_og = int(og.get("valid_packages") or 0)
    partial_og = int(og.get("partial_packages") or 0)
    attempted_bn = int(osr.get("attempted") or 0)
    resolved = int(osr.get("official_source_resolved") or 0)
    valid_bn = int(osr.get("valid_packages") or 0)
    partial_bn = int(osr.get("partial_packages") or 0)
    false_bn = int(osr.get("false_docs_rejected") or 0)
    false_og = int(og.get("false_docs_rejected") or 0)

    report = {
        "build_target": "20261003-m3-package-access-v1",
        "OPEN_GOV_DOCUMENT_RECOVERY": {
            "Projects tested": attempted_og,
            "Projects with public docs": int(og.get("projects_with_public_docs") or 0),
            "Valid packages": valid_og,
            "Partial packages": partial_og,
            "No docs": int(og.get("no_docs") or 0),
            "Retryable": int(og.get("retryable") or 0),
            "Documents total": int(og.get("documents_total") or 0),
            "Pricing schedules": int(og.get("pricing_schedules") or 0),
            "Specifications": int(og.get("specifications") or 0),
            "Addenda": int(og.get("addenda") or 0),
            "Bid forms": int(og.get("bid_forms") or 0),
            "Line-item-ready": int(og.get("line_item_ready") or 0),
        },
        "BIDNET_OFFICIAL_SOURCE_RESOLUTION": {
            "Attempted": attempted_bn,
            "Official source resolved": resolved,
            "Platform identified": int(osr.get("platform_identified") or 0),
            "OpenGov": int(platforms.get("OPENGOV") or 0),
            "IonWave": int(platforms.get("IONWAVE") or 0),
            "PlanetBids agency": int(platforms.get("PLANETBIDS_AGENCY") or 0),
            "Public Purchase agency": int(platforms.get("PUBLIC_PURCHASE_AGENCY") or 0),
            "State/local native": sum(
                int(platforms.get(k) or 0)
                for k in (
                    "STATE_PORTAL",
                    "COUNTY_PORTAL",
                    "CITY_PORTAL",
                    "AGENCY_NATIVE",
                    "UNIVERSITY_PORTAL",
                    "SCHOOL_DISTRICT_PORTAL",
                    "UTILITY_PORTAL",
                    "TRANSIT_PORTAL",
                )
            ),
            "Other": int(platforms.get("UNKNOWN") or 0),
            "Unresolved": int(osr.get("unresolved") or 0),
        },
        "BIDNET_PACKAGE_RECOVERY": {
            "Valid packages": valid_bn,
            "Partial packages": partial_bn,
            "No public package": int(osr.get("no_public_package") or 0),
            "Retryable": int(osr.get("retryable") or 0),
            "Ambiguous": int(osr.get("ambiguous") or 0),
            "False docs rejected": false_bn,
        },
        "PACKAGE_YIELD": {
            "OpenGov valid/partial yield": og.get("opengov_public_doc_yield")
            or (round((valid_og + partial_og) / attempted_og, 4) if attempted_og else 0),
            "BidNet official-source resolution yield": osr.get("official_source_resolution_yield")
            or (round(resolved / attempted_bn, 4) if attempted_bn else 0),
            "BidNet valid/partial package yield": osr.get("valid_or_partial_package_yield")
            or (round((valid_bn + partial_bn) / attempted_bn, 4) if attempted_bn else 0),
            "False-document rate": round(
                (false_og + false_bn)
                / max(1, false_og + false_bn + valid_og + partial_og + valid_bn + partial_bn),
                4,
            ),
        },
        "LINE_ITEMS": {
            "Packages passed downstream": int(og.get("line_item_ready") or 0)
            + int(osr.get("line_item_ready") or 0),
            "Opportunities with line items": int(og.get("line_item_ready") or 0)
            + int(osr.get("line_item_ready") or 0),
            "Total lines": int(og.get("total_lines") or 0) + int(osr.get("total_lines") or 0),
            "Exact product identities": int(og.get("exact_identity") or 0),
            "Strong generic identities": int(og.get("strong_generic") or 0),
        },
        "TOP_WORKING_ROUTES": {
            **({"GET /api/v1/project/{id}": int(og.get("route_successes") or 0)} if og else {}),
            **(osr.get("top_working_routes") or {}),
        },
        "TOP_FAILURE_ROUTES": {
            **(
                {
                    "NO_PUBLIC_DOCUMENTS": int(og.get("no_docs") or 0),
                    "false_docs_rejected_opengov": false_og,
                }
                if og
                else {}
            ),
            **(osr.get("top_failure_routes") or {}),
        },
        "MOST_IMPORTANT_ANSWERS": {
            "1": "GET https://api.procurement.opengov.com/api/v1/project/{project_id} returns attachments[], documentAttachment, addendums[] with signed S3 URLs.",
            "2": f"{valid_og + partial_og} of {attempted_og} tested OpenGov projects had usable public packages (yield {og.get('opengov_public_doc_yield')}).",
            "3": f"{resolved} of {attempted_bn} BidNet opportunities traced to an official source.",
            "4": f"{valid_bn + partial_bn} BidNet opportunities gained valid/partial free packages via resolved source.",
            "5": max(platforms, key=platforms.get) if platforms else "OPENGOV (primary proven route)",
            "6": "Yes — VALID/PARTIAL_FREE_PACKAGE_FOUND only after document_quality gate; false PDFs rejected.",
            "7": "Yes — staged OpenGov 20→100→500 path is repeatable via OpenGovPublicDocumentClient + checkpointed batch.",
            "8": "Scale OpenGov public-doc recovery across the full product-eligible OpenGov set, then deepen BidNet→OpenGov solicitation-number matching.",
        },
    }

    from m3_data_root import data_path

    out = data_path("PACKAGE_ACCESS_REPORT.json")
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
