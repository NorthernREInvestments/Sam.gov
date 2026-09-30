"""R4 field mapping — ResponseFieldMap from R2/R3. Never invent N/A."""

from __future__ import annotations

from typing import Any

from response_engine.models import new_id
from response_engine.r4_constants import (
    BLOCKED,
    CAGE_REQUIRED_UNRESOLVED,
    NOT_APPLICABLE,
    OWNER_ATTESTATION_REQUIRED,
    OWNER_INPUT_REQUIRED_FIELD,
    OWNER_SIGNATURE_REQUIRED_FIELD,
    POPULATED,
    READY,
    UNKNOWN,
)


def _is_unknown(v: Any) -> bool:
    return v in (None, "", "UNKNOWN", UNKNOWN)


def new_field_map(**fields: Any) -> dict[str, Any]:
    return {
        "kind": "ResponseFieldMap",
        "field_map_id": new_id("RFM"),
        "source_requirement": fields.get("source_requirement"),
        "target_document": fields.get("target_document"),
        "target_page_or_sheet": fields.get("target_page_or_sheet"),
        "target_field_or_cell": fields.get("target_field_or_cell"),
        "target_type": fields.get("target_type"),
        "required": bool(fields.get("required", True)),
        "source_value": fields.get("source_value"),
        "evidence_source": fields.get("evidence_source"),
        "generation_status": fields.get("generation_status") or UNKNOWN,
        "owner_confirmation_required": bool(fields.get("owner_confirmation_required")),
        "notes": fields.get("notes"),
    }


def build_field_maps(
    project: dict[str, Any],
    *,
    profile: dict[str, Any] | None = None,
    selected_scenario: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build deterministic field maps from company + lines + R3 + pricing."""
    maps: list[dict[str, Any]] = []
    profile = profile or {}
    r3 = project.get("r3_analysis") or {}
    profile_sum = r3.get("profile_summary") or {}

    def company_field(name: str, value: Any, *, target: str = "BIDDER_INFO", cell: str | None = None):
        if _is_unknown(value):
            status = BLOCKED if name.upper() == "CAGE" and _cage_required(project) else OWNER_INPUT_REQUIRED_FIELD
            display = CAGE_REQUIRED_UNRESOLVED if name.upper() == "CAGE" and status == BLOCKED else None
            maps.append(
                new_field_map(
                    source_requirement=f"company.{name}",
                    target_document=target,
                    target_field_or_cell=cell or name,
                    target_type="TEXT",
                    source_value=display,
                    evidence_source="R3_company_profile",
                    generation_status=status,
                    notes="Unknown company fact — not invented",
                )
            )
        else:
            maps.append(
                new_field_map(
                    source_requirement=f"company.{name}",
                    target_document=target,
                    target_field_or_cell=cell or name,
                    target_type="TEXT",
                    source_value=value,
                    evidence_source="R3_company_profile",
                    generation_status=READY,
                )
            )

    company_field("legal_name", profile.get("legal_name") or profile_sum.get("legal_name"))
    company_field("UEI", profile.get("UEI") or profile_sum.get("UEI"))
    company_field("CAGE", profile.get("CAGE") or profile_sum.get("CAGE"))
    company_field("principal_business_address", profile.get("principal_business_address"))
    company_field("SAM_registration_status", profile.get("SAM_registration_status") or profile_sum.get("SAM"))

    # Pricing lines from selected scenario + line items
    scenario = selected_scenario or _selected_scenario(project)
    lines = project.get("line_items") or []
    for li in lines:
        tmap = li.get("template_map") or {}
        unit_price = _line_unit_price(li, scenario, project)
        cell = tmap.get("unit_price_cell")
        sheet = tmap.get("sheet")
        if unit_price is None:
            maps.append(
                new_field_map(
                    source_requirement=f"line.{li.get('line_item_id')}.unit_price",
                    target_document=tmap.get("template_document_id") or "PRICING",
                    target_page_or_sheet=sheet,
                    target_field_or_cell=cell or "unit_price",
                    target_type="CURRENCY",
                    required=True,
                    source_value=None,
                    evidence_source="R2_pricing_scenario",
                    generation_status=BLOCKED if not scenario else OWNER_INPUT_REQUIRED_FIELD,
                    notes="No selected/approved unit price — not guessed",
                )
            )
        elif not cell and tmap:
            maps.append(
                new_field_map(
                    source_requirement=f"line.{li.get('line_item_id')}.unit_price",
                    target_document=tmap.get("template_document_id") or "PRICING",
                    target_page_or_sheet=sheet,
                    target_field_or_cell=None,
                    target_type="CURRENCY",
                    source_value=unit_price,
                    evidence_source=f"scenario:{scenario.get('scenario_id') if scenario else None}",
                    generation_status="FORM_MAPPING_REQUIRED",
                    notes="Price known but buyer cell mapping missing — do not guess cell",
                )
            )
        else:
            maps.append(
                new_field_map(
                    source_requirement=f"line.{li.get('line_item_id')}.unit_price",
                    target_document=tmap.get("template_document_id") or "PRICING_SCHEDULE",
                    target_page_or_sheet=sheet,
                    target_field_or_cell=cell or f"line_{li.get('CLIN') or li.get('buyer_line_number')}_unit",
                    target_type="CURRENCY",
                    source_value=str(unit_price),
                    evidence_source=f"scenario:{scenario.get('scenario_id') if scenario else 'schedule'}",
                    generation_status=READY,
                )
            )
        # Quantity consistency
        qty = li.get("quantity") or li.get("normalized_quantity")
        if _is_unknown(qty):
            maps.append(
                new_field_map(
                    source_requirement=f"line.{li.get('line_item_id')}.quantity",
                    target_document="PRICING",
                    target_type="NUMBER",
                    generation_status=BLOCKED,
                    notes="Quantity unknown — generation blocked",
                )
            )

    # R3 representations / attestations
    for att in project.get("owner_attestations") or []:
        if att.get("owner_confirmed") and att.get("answer") in ("YES", "NO"):
            maps.append(
                new_field_map(
                    source_requirement=att.get("requirement_id") or att.get("topic"),
                    target_document="CERTIFICATION",
                    target_field_or_cell=att.get("topic") or "attestation",
                    target_type="CHECKBOX_OR_YESNO",
                    source_value=att.get("answer"),
                    evidence_source=f"owner:{att.get('confirmed_by')}@{att.get('confirmed_at')}",
                    generation_status=READY,
                    owner_confirmation_required=False,
                )
            )
        else:
            maps.append(
                new_field_map(
                    source_requirement=att.get("requirement_id") or att.get("topic"),
                    target_document="CERTIFICATION",
                    target_field_or_cell=att.get("topic") or "attestation",
                    target_type="CHECKBOX_OR_YESNO",
                    source_value=None,
                    evidence_source="R3_owner_attestation",
                    generation_status=OWNER_ATTESTATION_REQUIRED,
                    owner_confirmation_required=True,
                    notes=(att.get("question") or "")[:200],
                )
            )

    # Signature blocks — never populate
    maps.append(
        new_field_map(
            source_requirement="signature.owner",
            target_document="OFFER_FORMS",
            target_field_or_cell="signature",
            target_type="SIGNATURE",
            source_value=None,
            evidence_source="R4_policy",
            generation_status=OWNER_SIGNATURE_REQUIRED_FIELD,
            notes="Never auto-sign",
        )
    )

    return maps


def summarize_field_maps(maps: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for m in maps:
        st = m.get("generation_status") or UNKNOWN
        counts[st] = counts.get(st, 0) + 1
    return {
        "total": len(maps),
        "by_status": counts,
        "ready": counts.get(READY, 0) + counts.get(POPULATED, 0),
        "blocked": counts.get(BLOCKED, 0),
        "owner_attestation": counts.get(OWNER_ATTESTATION_REQUIRED, 0),
        "owner_signature": counts.get(OWNER_SIGNATURE_REQUIRED_FIELD, 0),
        "unresolved": counts.get(UNKNOWN, 0) + counts.get(OWNER_INPUT_REQUIRED_FIELD, 0) + counts.get(BLOCKED, 0),
    }


def _selected_scenario(project: dict[str, Any]) -> dict[str, Any] | None:
    sel = project.get("selected_bid_price_scenario_id")
    scenarios = project.get("pricing_scenarios") or []
    for s in scenarios:
        if sel and s.get("scenario_id") == sel and s.get("scenario_status") == "ACTIVE":
            return s
        if s.get("selected_for_draft") and s.get("scenario_status") == "ACTIVE":
            return s
    # Do NOT auto-pick a scenario for final population without selection —
    # but allow draft schedule generation from last ACTIVE if owner marked draft_ok
    for s in reversed(scenarios):
        if s.get("scenario_status") == "ACTIVE" and s.get("approved_for_r4_draft"):
            return s
    return None


def _line_unit_price(li: dict[str, Any], scenario: dict[str, Any] | None, project: dict[str, Any]) -> Any:
    if li.get("bid_unit_price") is not None:
        return li.get("bid_unit_price")
    if scenario and scenario.get("line_unit_prices"):
        return (scenario.get("line_unit_prices") or {}).get(li.get("line_item_id"))
    # Never invent from supplier cost
    return None


def _cage_required(project: dict[str, Any]) -> bool:
    juris = (project.get("jurisdiction") or "").lower()
    sub = (project.get("submission_system") or "").upper()
    return juris in ("federal", "dod", "dla") or sub in {"DIBBS", "PIEE", "SAM"}
