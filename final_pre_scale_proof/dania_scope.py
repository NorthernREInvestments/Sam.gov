"""Phase 2 — Dania line / revenue alignment (product vs install vs unallocated)."""

from __future__ import annotations

import json
import re
from typing import Any

from final_pre_scale_proof.models import (
    DANIA_OID,
    INSTALL_SCOPE,
    MIXED_SCOPE,
    PRIOR_MLR_CK,
    PRODUCT_SCOPE,
    SERVICE_SCOPE,
    UNALLOCATED,
)
from m3_data_root import data_path

_INSTALL = re.compile(
    r"\b("
    r"construct(?:ing|ion)?|install(?:ing|ation)?|excavation|trench|inlet|"
    r"labor|furnishing and installing|demobilization|mobilization|"
    r"planting|landscap|concrete|paving|grading"
    r")\b",
    re.I,
)
_PRODUCT = re.compile(
    r"\b("
    r"model(?:\s+number)?\s*:|dumor|elkay|exofit|pw\s+athletic|"
    r"leg-press|chest press|fitness bike|bottle fill|bench|picnic|"
    r"basketball|rim with nylon"
    r")\b",
    re.I,
)
_SERVICE = re.compile(r"\b(maintenance|warranty\s+service|training)\b", re.I)
_FAKE_IDENTITY = re.compile(r"planting shall be clear|exfiltration trench|type c inlet|ft deep\)", re.I)


def _load_mlr_lines(oid: str) -> list[dict[str, Any]]:
    p = data_path(PRIOR_MLR_CK)
    if not p.exists():
        return []
    ck = json.loads(p.read_text(encoding="utf-8"))
    return ((ck.get("opportunities") or {}).get(oid) or {}).get("lines") or []


def _scope_of(line: dict[str, Any]) -> str:
    desc = str(line.get("description") or "")
    mfr = str(line.get("manufacturer") or "")
    blob = f"{mfr} {desc}"
    if _FAKE_IDENTITY.search(desc) and not (line.get("mpn") or line.get("model")):
        return INSTALL_SCOPE
    if _INSTALL.search(blob) and not _PRODUCT.search(blob):
        return INSTALL_SCOPE
    if _SERVICE.search(blob) and not _PRODUCT.search(blob):
        return SERVICE_SCOPE
    if line.get("mpn") or line.get("model") or _PRODUCT.search(blob):
        # Elkay outdoor station / DuMor / Exofit gear — product, but bid is furnish+install
        if re.search(r"furnish(?:ing)?\s+and\s+install", desc, re.I):
            return MIXED_SCOPE
        return PRODUCT_SCOPE
    if _INSTALL.search(blob):
        return INSTALL_SCOPE
    return UNALLOCATED


def align_dania_lines(*, revenue_validation: dict[str, Any]) -> dict[str, Any]:
    lines = _load_mlr_lines(DANIA_OID)
    rows = []
    product_n = install_n = service_n = mixed_n = unalloc_n = 0
    product_quote_ready = 0

    for ln in lines:
        scope = _scope_of(ln)
        if scope == PRODUCT_SCOPE:
            product_n += 1
            if ln.get("identity_usable") and (ln.get("mpn") or ln.get("model")):
                product_quote_ready += 1
        elif scope == INSTALL_SCOPE:
            install_n += 1
        elif scope == SERVICE_SCOPE:
            service_n += 1
        elif scope == MIXED_SCOPE:
            mixed_n += 1
        else:
            unalloc_n += 1

        rows.append(
            {
                "line": ln.get("line_id"),
                "description": (ln.get("description") or "")[:180],
                "qty": ln.get("quantity"),
                "uom": ln.get("uom"),
                "product_install_service": scope,
                "manufacturer": ln.get("manufacturer"),
                "mpn_or_model": ln.get("mpn") or ln.get("model"),
                "identity_usable": bool(ln.get("identity_usable")),
                "source_document": Path_name(ln.get("source_document")),
                "revenue_allocation_evidence": None,  # no line-level $ allocation in package
                "clin": ln.get("clin"),
            }
        )

    program_value = (revenue_validation.get("corrected") or {}).get("value")
    # Cannot allocate program funding to product vs install without schedule of values
    return {
        "opportunity_id": DANIA_OID,
        "material_lines": len(lines),
        "lines": rows,
        "counts": {
            "PRODUCT": product_n,
            "INSTALL": install_n,
            "SERVICE": service_n,
            "MIXED": mixed_n,
            "UNALLOCATED": unalloc_n,
            "product_quote_ready_with_mpn": product_quote_ready,
        },
        "PRODUCT_SCOPE_VALUE": None,  # unknown — no allocation evidence
        "INSTALL_SCOPE_VALUE": None,
        "UNALLOCATED_VALUE": program_value,  # entire $400K unallocated across scopes
        "Total_program_project_value": program_value,
        "assumption_rejected": "Do not assume full $400K applies to quote-ready product lines",
        "confidence": "HIGH_ON_CLASSIFICATION_LOW_ON_DOLLAR_ALLOCATION",
        "economics_target_ready": False,
        "reason": "Grant/program funding cannot be separated into product vs install without schedule of values or bid schedule dollar columns",
    }


def Path_name(p: Any) -> str | None:
    if not p:
        return None
    s = str(p).replace("\\", "/")
    return s.rsplit("/", 1)[-1]
