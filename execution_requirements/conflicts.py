"""Conflict detection and amendment re-evaluation."""

from __future__ import annotations

from typing import Any

from execution_requirements.constants import CONFLICT_FLAG, ST_BLOCKED, ST_REQUIRES_ACTION
from execution_requirements.extract import build_execution_requirements
from execution_requirements.models import make_requirement


def detect_requirement_conflicts(requirements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Detect material conflicts across requirements.
    Returns conflict records; also mutates conflicting reqs to BLOCKED when material.
    """
    conflicts: list[dict[str, Any]] = []
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for r in requirements:
        by_cat.setdefault(str(r.get("category")), []).append(r)

    # FOB origin vs destination
    fobs = by_cat.get("FOB") or []
    origins = [f for f in fobs if "ORIGIN" in str(f.get("captured_value") or f.get("normalized_requirement") or "").upper()]
    dests = [f for f in fobs if "DESTINATION" in str(f.get("captured_value") or f.get("normalized_requirement") or "").upper()]
    if origins and dests:
        c = {
            "kind": CONFLICT_FLAG,
            "category": "FOB",
            "message": "Conflicting FOB terms (ORIGIN vs DESTINATION) found across sources.",
            "requirement_ids": [x.get("requirement_id") for x in origins + dests],
            "material": True,
            "resolution": "Prefer latest amendment when determinable; otherwise block readiness.",
        }
        conflicts.append(c)
        for x in origins + dests:
            x["status"] = ST_BLOCKED
            x["blocking"] = True
            x["notes"] = (x.get("notes") or "") + f";{CONFLICT_FLAG}"

    # Brand-only vs brand-or-equal
    products = by_cat.get("PRODUCT") or []
    only = [p for p in products if "BRAND_ONLY" in str(p.get("subtype") or "") or "no substitut" in str(p.get("raw_text") or "").lower()]
    equal = [p for p in products if "BRAND_OR_EQUAL" in str(p.get("subtype") or "") or "or equal" in str(p.get("raw_text") or "").lower()]
    if only and equal:
        conflicts.append(
            {
                "kind": CONFLICT_FLAG,
                "category": "PRODUCT",
                "message": "Brand-name-only conflicts with brand-or-equal language.",
                "requirement_ids": [x.get("requirement_id") for x in only + equal],
                "material": True,
                "resolution": "Owner decision required; do not assume substitution is allowed.",
            }
        )
        for x in only + equal:
            x["status"] = ST_BLOCKED
            x["blocking"] = True
            x["assigned_actor"] = "OWNER"
            x["notes"] = (x.get("notes") or "") + f";{CONFLICT_FLAG}"

    # Commercial vs MIL-STD packaging both mandatory without resolution
    packs = by_cat.get("PACKAGING") or []
    mil = [p for p in packs if "MIL" in str(p.get("normalized_requirement") or "").upper() or "MIL" in str(p.get("raw_text") or "").upper()]
    com = [p for p in packs if "commercial" in str(p.get("normalized_requirement") or "").lower() or "commercial" in str(p.get("raw_text") or "").lower()]
    if mil and com and len(packs) >= 2:
        # Not always a conflict (commercial may be fallback) — flag for review, not auto-block
        conflicts.append(
            {
                "kind": CONFLICT_FLAG,
                "category": "PACKAGING",
                "message": "Both military and commercial packaging language present — confirm which governs.",
                "requirement_ids": [x.get("requirement_id") for x in mil + com],
                "material": False,
                "resolution": "Prefer amendment/SPI; ask supplier to confirm governing packaging standard.",
            }
        )

    # Quantity conflicts on same CLIN
    qtys = by_cat.get("QUANTITY_UOM") or []
    by_clin: dict[str, list[dict[str, Any]]] = {}
    for q in qtys:
        clin = str(q.get("clin") or "NONE")
        by_clin.setdefault(clin, []).append(q)
    for clin, items in by_clin.items():
        vals = []
        for it in items:
            cv = it.get("captured_value")
            if isinstance(cv, dict) and cv.get("quantity") is not None:
                vals.append((cv.get("quantity"), it))
            elif cv is not None and not isinstance(cv, dict):
                try:
                    vals.append((float(str(cv).replace(",", "")), it))
                except ValueError:
                    pass
        uniq = {v[0] for v in vals}
        if clin != "NONE" and len(uniq) > 1:
            conflicts.append(
                {
                    "kind": CONFLICT_FLAG,
                    "category": "QUANTITY_UOM",
                    "message": f"Conflicting quantities for CLIN {clin}: {sorted(uniq)}",
                    "requirement_ids": [v[1].get("requirement_id") for v in vals],
                    "material": True,
                    "resolution": "Latest amendment quantity takes priority when determinable.",
                }
            )
            for _, it in vals:
                it["status"] = ST_BLOCKED
                it["blocking"] = True
                it["notes"] = (it.get("notes") or "") + f";{CONFLICT_FLAG}"

    return conflicts


def apply_amendment_overlay(
    base_text: str,
    amendment_text: str,
    *,
    row: dict[str, Any] | None = None,
    amendment_document: str = "amendment",
) -> dict[str, Any]:
    """
    Re-extract from amendment and mark superseded categories.
    Latest authoritative amendment takes priority when categories overlap.
    """
    row = dict(row or {})
    base_reqs = build_execution_requirements(row, text=base_text, source_document="solicitation", include_supplier_seeds=False)
    amd_reqs = build_execution_requirements(row, text=amendment_text, source_document=amendment_document, include_supplier_seeds=False)

    changed_categories: set[str] = set()
    # Detect common change signals
    lower = (amendment_text or "").lower()
    if re_search_any(lower, ["quantity", "qty"]):
        changed_categories.add("QUANTITY_UOM")
    if re_search_any(lower, ["deadline", "due date", "closing"]):
        changed_categories.add("SUBMISSION")
    if re_search_any(lower, ["packag", "mil-std", "mil std"]):
        changed_categories.add("PACKAGING")
    if re_search_any(lower, ["deliver", "ship", "fob"]):
        changed_categories.update({"DELIVERY", "FOB", "SHIPPING"})
    if re_search_any(lower, ["spec", "part number", "nsn", "model"]):
        changed_categories.add("PRODUCT")

    amd_cats = {r.get("category") for r in amd_reqs}
    changed_categories |= {c for c in amd_cats if c}

    merged: list[dict[str, Any]] = []
    for r in base_reqs:
        if r.get("category") in changed_categories:
            r = dict(r)
            r["status"] = ST_REQUIRES_ACTION
            r["notes"] = (r.get("notes") or "") + ";superseded_pending_amendment_review"
            r["blocking"] = True if r.get("mandatory") else r.get("blocking")
            # Keep for audit but mark superseded
            r["authority_state"] = "SUPERSEDED_PENDING_AMENDMENT"
        merged.append(r)

    for r in amd_reqs:
        r = dict(r)
        r["notes"] = (r.get("notes") or "") + ";from_amendment"
        r["authority_state"] = "GOVERNING_AMENDMENT"
        if r.get("mandatory"):
            r["blocking"] = True
            r["status"] = ST_REQUIRES_ACTION
        merged.append(r)

    # Explicit notice requirement
    merged.append(
        make_requirement(
            category="SUBMISSION",
            subtype="AMENDMENT_ACK",
            normalized="Acknowledge amendment and re-validate affected requirements",
            raw_text=(amendment_text or "")[:200],
            source_document=amendment_document,
            mandatory=True,
            blocking=True,
            status=ST_REQUIRES_ACTION,
            plain_english_action="Upload signed amendment acknowledgment and re-check quantity, deadline, packaging, and delivery.",
            confidence="DERIVED",
        )
    )

    conflicts = detect_requirement_conflicts(merged)
    return {
        "kind": "AmendmentOverlayResult",
        "changed_categories": sorted(changed_categories),
        "requirements": merged,
        "conflicts": conflicts,
        "note": "Original requirements are not assumed valid for changed categories.",
    }


def re_search_any(text: str, needles: list[str]) -> bool:
    return any(n in text for n in needles)
