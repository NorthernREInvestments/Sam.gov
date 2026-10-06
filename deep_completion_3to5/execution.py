"""Delivery / execution risk review for deep-completion candidates."""

from __future__ import annotations

import re
from typing import Any

from deep_completion_3to5.models import COLLIER_OID, EXECUTION_BLOCKED, EXECUTION_COMPLEX, LOWER_PRIORITY

_INSTALL = re.compile(
    r"\b(furnish\s+and\s+install|install(?:ation)?|removal|grading|tilling|labor|"
    r"site\s+work|construction|trench|exfiltration|concrete|paving)\b",
    re.I,
)
_SERVICE = re.compile(r"\b(service|maintenance|repair|warranty\s+work|professional\s+services)\b", re.I)


def review_execution(opportunity_id: str, lines: list[dict[str, Any]], corpus_item: dict[str, Any] | None = None) -> dict[str, Any]:
    product = []
    install = []
    service = []
    for ln in lines:
        desc = str(ln.get("description") or "")
        mfr = str(ln.get("manufacturer") or "")
        blob = f"{desc} {mfr}"
        if _INSTALL.search(blob) or mfr.upper() in {
            "SITE WORK",
            "CONDUIT INSTALLATION",
            "FENCE INSTALLATION",
            "LATERAL PIPING",
            "WATER SOURCE",
            "BUBBLERS",
        }:
            # Still product if has commercial MPN from known OEM
            if ln.get("mpn") or ln.get("model") and ln.get("manufacturer") in {"Hunter", "Netafim", "Cummins", "Elkay", "DuMor", "EXOFIT", "DUMOR, INC.", "ELKAY"}:
                product.append(ln)
            elif ln.get("identity_usable") and (ln.get("mpn") or ln.get("model")):
                product.append(ln)
            else:
                install.append(ln)
        elif _SERVICE.search(blob):
            service.append(ln)
        else:
            product.append(ln)

    install_heavy = len(install) + len(service) >= max(3, int(0.4 * max(1, len(lines))))
    fatal = None
    status = "PASS"
    delivery_risk = "STANDARD"
    oem_auth = "UNKNOWN"
    other: list[str] = []

    # go-metro Cummins parts — OEM authorization may matter
    if opportunity_id.endswith("298984"):
        oem_auth = "LIKELY_REQUIRED_CUMMINS"
        other.append("Confirm Cummins parts authorization / genuine parts channel")
        delivery_risk = "STANDARD_PARTS_SHIP"
    if opportunity_id.endswith("286698"):
        other.append("EMS/medical supplies — licensing and cold-chain/controlled items may apply")
        delivery_risk = "SPECIALTY_MEDICAL"
        oem_auth = "DISTRIBUTOR_ACCOUNT_LIKELY"
    if opportunity_id.endswith("284328"):
        install_n = sum(1 for l in lines if _INSTALL.search(str(l.get("description") or "")))
        if install_n >= 5:
            delivery_risk = "PARK_IMPROVEMENT_INSTALL"
            other.append("Park construction/install scope mixed with product lines")
    if opportunity_id.endswith("299806"):
        delivery_risk = "HVAC_UNIT_SITE"
        other.append("Verify roof/unit access, crane, and installation responsibility")
        oem_auth = "UNKNOWN"

    if opportunity_id == COLLIER_OID or install_heavy:
        status = "FAIL" if len(product) < 3 else "PASS_WITH_COMPLEXITY"
        fatal = None if status != "FAIL" else "INSTALL_HEAVY_SCOPE"
        delivery_risk = "HIGH_INSTALL_LABOR"
        complexity = EXECUTION_COMPLEX
        priority = LOWER_PRIORITY
        if len(product) == 0:
            status = "FAIL"
            fatal = EXECUTION_BLOCKED
    else:
        complexity = None
        priority = None

    return {
        "opportunity_id": opportunity_id,
        "fatal_blocker": fatal,
        "delivery_risk": delivery_risk,
        "install_labor": len(install),
        "service_lines": len(service),
        "product_lines": len(product),
        "oem_authorization": oem_auth,
        "other": other,
        "destination": None,
        "fob": None,
        "bonding_insurance": "REVIEW_REQUIRED",
        "liquidated_damages": "UNKNOWN",
        "complexity": complexity,
        "priority": priority,
        "PASS_FAIL": "PASS" if status.startswith("PASS") else "FAIL",
        "status": status,
        "product_line_ids": [l.get("line_id") for l in product],
        "install_line_ids": [l.get("line_id") for l in install],
    }
