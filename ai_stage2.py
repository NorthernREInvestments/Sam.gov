"""Stage 2 — evidence / requirements extraction (Luna, no web, no inventing economics).

Schema: stage2-evidence-v2 — sparse facts array (omit unknowns).
Python organizes / validates / decides. Model extracts evidence only.

NO live API calls from tests — all OpenAI traffic via openai_runtime.create_response.
"""

from __future__ import annotations
from application_clock import now_utc

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from ai_analysis_cache import PROMPT_SCHEMA_VERSION, hash_source_material
from ai_funnel import stage0_evaluate
from ai_model_router import FunnelStage, ModelTier, max_input_chars_for_stage, model_for_tier
from data_integrity import (
    STATUS_ASSESSMENT,
    STATUS_UNKNOWN,
    STATUS_VERIFIED,
    assessment_fact,
    parse_money,
    parse_quantity,
    unknown_fact,
    verified_fact,
)
from economic_integrity import (
    COST_NOT_APPLICABLE,
    COST_REQUIRED_UNKNOWN,
    cost_item,
    infer_required_cost_categories,
)

logger = logging.getLogger("govtracker.ai.stage2")

STAGE2_SCHEMA_VERSION = "stage2-evidence-v2"
STAGE2_INSTRUCTION_VERSION = "stage2-instr-v2"
# Ceiling from measured local fixtures + headroom (see measure_stage2_output_fixtures).
STAGE2_MAX_OUTPUT_TOKENS = int(os.getenv("AI_STAGE2_MAX_OUTPUT_TOKENS", "4400"))
STAGE2_TASK = "stage2_evidence_extract"

REASON_CODES = (
    "STAGE2_EVIDENCE_COMPLETE",
    "STAGE2_RESEARCH_REQUIRED",
    "STAGE2_FATAL_VERIFIED",
    "STAGE2_INSUFFICIENT_EVIDENCE_ADVANCE",
    "STAGE2_NEEDS_ADDITIONAL_DOCUMENT_PROCESSING",
    "STAGE2_PARSE_ERROR_ADVANCE",
    "STAGE2_GATE_BLOCKED",
    "STAGE1_CURRENT_RESULT_MISSING",
)

RESEARCH_NEED_CODES = (
    "SUPPLIER_QUOTE_REQUIRED",
    "FREIGHT_REQUIRED",
    "SUBCONTRACTOR_QUOTE_REQUIRED",
    "FINANCING_TERMS_REQUIRED",
    "NO_PG_VERIFICATION_REQUIRED",
    "ZERO_UPFRONT_VERIFICATION_REQUIRED",
    "MANUFACTURER_AUTHORIZATION_REQUIRED",
    "LICENSE_REQUIREMENT_UNCLEAR",
    "BOND_REQUIREMENT_UNCLEAR",
    "QUANTITY_UNCLEAR",
    "DELIVERY_REQUIREMENT_UNCLEAR",
    "OPTION_PERIOD_UNCLEAR",
    "HISTORICAL_PRICING_REQUIRED",
    "COMPETITION_HISTORY_REQUIRED",
    "INSTALLATION_COST_REQUIRED",
)

FACT_CODES: tuple[str, ...] = (
    "SOLICITATION_NUMBER",
    "NOTICE_ID",
    "AGENCY",
    "TITLE",
    "CATEGORY",
    "NAICS",
    "PSC",
    "SET_ASIDE",
    "CONTRACT_TYPE",
    "EVALUATION_METHOD",
    "AWARD_BASIS",
    "RESPONSE_DEADLINE",
    "SITE_VISIT_DATE",
    "QUESTION_DEADLINE",
    "PERFORMANCE_START",
    "PERFORMANCE_END",
    "DELIVERY_DEADLINE",
    "SCOPE_SUMMARY",
    "PRODUCTS_SERVICES",
    "QUANTITY",
    "UNIT_OF_MEASURE",
    "EXACT_MODEL",
    "BRAND_NAME_OR_EQUAL",
    "SALIENT_CHARACTERISTICS",
    "DELIVERY_LOCATION",
    "PLACE_OF_PERFORMANCE",
    "INSTALLATION_REQUIRED",
    "REMOVAL_DISPOSAL_REQUIRED",
    "TRAINING_REQUIRED",
    "MAINTENANCE_REQUIRED",
    "SUBCONTRACTING_ALLOWED",
    "SITE_ACCESS",
    "STAFFING_LABOR",
    "SCHEDULE_FREQUENCY",
    "BOND_REQUIREMENT",
    "INSURANCE_REQUIREMENT",
    "LICENSES",
    "CERTIFICATIONS",
    "SECURITY_BACKGROUND",
    "WAGE_DETERMINATION",
    "DOMESTIC_SOURCING",
    "NMR_RELEVANCE",
    "MANUFACTURER_AUTHORIZATION",
    "MANDATORY_REGISTRATIONS",
    "SPECIAL_CLAUSES",
    "SUBMISSION_METHOD",
    "REQUIRED_FORMS",
    "TECHNICAL_PROPOSAL_REQUIRED",
    "PAST_PERFORMANCE_REQUIRED",
    "PRICING_FORMAT",
    "REPS_AND_CERTS",
    "PAGE_LIMITS",
    "MANDATORY_ATTACHMENTS",
    "STATED_VALUE",
    "FUNDING_CEILING",
    "GOVERNMENT_ESTIMATE",
    "OPTION_PERIODS",
)

FACT_CODE_TO_PATH: dict[str, tuple[str, str]] = {
    "SOLICITATION_NUMBER": ("identity", "solicitation_number"),
    "NOTICE_ID": ("identity", "notice_id"),
    "AGENCY": ("identity", "agency"),
    "TITLE": ("identity", "title"),
    "CATEGORY": ("procurement", "category"),
    "NAICS": ("procurement", "naics"),
    "PSC": ("procurement", "psc"),
    "SET_ASIDE": ("procurement", "set_aside"),
    "CONTRACT_TYPE": ("procurement", "contract_type"),
    "EVALUATION_METHOD": ("procurement", "evaluation_method"),
    "AWARD_BASIS": ("procurement", "award_basis"),
    "RESPONSE_DEADLINE": ("dates", "response_deadline"),
    "SITE_VISIT_DATE": ("dates", "site_visit_date"),
    "QUESTION_DEADLINE": ("dates", "question_deadline"),
    "PERFORMANCE_START": ("dates", "performance_start"),
    "PERFORMANCE_END": ("dates", "performance_end"),
    "DELIVERY_DEADLINE": ("dates", "delivery_deadline"),
    "SCOPE_SUMMARY": ("scope", "summary"),
    "PRODUCTS_SERVICES": ("scope", "products_services"),
    "QUANTITY": ("scope", "quantity"),
    "UNIT_OF_MEASURE": ("scope", "unit_of_measure"),
    "EXACT_MODEL": ("scope", "exact_model"),
    "BRAND_NAME_OR_EQUAL": ("scope", "brand_name_or_equal"),
    "SALIENT_CHARACTERISTICS": ("scope", "salient_characteristics"),
    "DELIVERY_LOCATION": ("execution", "delivery_location"),
    "PLACE_OF_PERFORMANCE": ("execution", "place_of_performance"),
    "INSTALLATION_REQUIRED": ("execution", "installation_required"),
    "REMOVAL_DISPOSAL_REQUIRED": ("execution", "removal_disposal_required"),
    "TRAINING_REQUIRED": ("execution", "training_required"),
    "MAINTENANCE_REQUIRED": ("execution", "maintenance_required"),
    "SUBCONTRACTING_ALLOWED": ("execution", "subcontracting_allowed"),
    "SITE_ACCESS": ("execution", "site_access_requirements"),
    "STAFFING_LABOR": ("execution", "staffing_labor_requirements"),
    "SCHEDULE_FREQUENCY": ("execution", "schedule_frequency"),
    "BOND_REQUIREMENT": ("compliance", "bond_requirement"),
    "INSURANCE_REQUIREMENT": ("compliance", "insurance_requirement"),
    "LICENSES": ("compliance", "licenses"),
    "CERTIFICATIONS": ("compliance", "certifications"),
    "SECURITY_BACKGROUND": ("compliance", "security_background"),
    "WAGE_DETERMINATION": ("compliance", "wage_determination"),
    "DOMESTIC_SOURCING": ("compliance", "domestic_sourcing"),
    "NMR_RELEVANCE": ("compliance", "nmr_relevance"),
    "MANUFACTURER_AUTHORIZATION": ("compliance", "manufacturer_authorization"),
    "MANDATORY_REGISTRATIONS": ("compliance", "mandatory_registrations"),
    "SPECIAL_CLAUSES": ("compliance", "special_clauses"),
    "SUBMISSION_METHOD": ("submission", "submission_method"),
    "REQUIRED_FORMS": ("submission", "required_forms"),
    "TECHNICAL_PROPOSAL_REQUIRED": ("submission", "technical_proposal_required"),
    "PAST_PERFORMANCE_REQUIRED": ("submission", "past_performance_required"),
    "PRICING_FORMAT": ("submission", "pricing_format"),
    "REPS_AND_CERTS": ("submission", "reps_and_certs"),
    "PAGE_LIMITS": ("submission", "page_limits"),
    "MANDATORY_ATTACHMENTS": ("submission", "mandatory_attachments"),
    "STATED_VALUE": ("economic_evidence", "stated_value"),
    "FUNDING_CEILING": ("economic_evidence", "funding_ceiling"),
    "GOVERNMENT_ESTIMATE": ("economic_evidence", "government_estimate"),
    "OPTION_PERIODS": ("economic_evidence", "option_periods"),
}

STAGE2_INSTRUCTIONS = f"""GovCon Stage-2 evidence extraction. Output ONLY the structured schema. No markdown.

DATA INTEGRITY — NON-NEGOTIABLE:
Never fabricate missing facts or numeric values.
If evidence does not establish a value, OMIT that fact (do not emit null placeholders).
Do not invent prices, freight, financing, margins, profit, bid prices, or supplier costs.
evidence_text MUST be a short verbatim substring of the supplied source text when present.
Building numbers, NAICS, ZIP codes, day counts are NOT quantities or money.

Rules:
- Emit only facts established by the supplied evidence package.
- Prefer the smallest evidence_text fragment that supports the value.
- fatal_candidates ONLY with evidence_text; otherwise omit.
- Do NOT emit advance, reason_code, research_needs, economic costs, or completeness.
Schema: {STAGE2_SCHEMA_VERSION} / {STAGE2_INSTRUCTION_VERSION}"""


def _nullable_string(max_len: int = 256) -> dict[str, Any]:
    return {"type": ["string", "null"], "maxLength": max_len}


def _fact_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "code": {"type": "string", "enum": list(FACT_CODES)},
            "value": {"type": ["string", "number", "boolean"]},
            "document_id": _nullable_string(64),
            "locator": _nullable_string(80),
            "evidence_text": _nullable_string(240),
            "confidence": {"type": ["string", "null"], "enum": ["HIGH", "MEDIUM", "LOW", None]},
        },
        "required": ["code", "value", "document_id", "locator", "evidence_text", "confidence"],
    }


def _fatal_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "code": {"type": "string", "maxLength": 64},
            "summary": {"type": "string", "maxLength": 160},
            "document_id": _nullable_string(64),
            "locator": _nullable_string(80),
            "evidence_text": {"type": "string", "maxLength": 240},
        },
        "required": ["code", "summary", "document_id", "locator", "evidence_text"],
    }


STAGE2_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "facts": {"type": "array", "items": _fact_item_schema(), "maxItems": 48},
        "fatal_candidates": {"type": "array", "items": _fatal_item_schema(), "maxItems": 8},
    },
    "required": ["facts", "fatal_candidates"],
}


def stage2_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "stage2_evidence",
        "strict": True,
        "schema": STAGE2_JSON_SCHEMA,
    }


def stage2_schema_fingerprint_component() -> str:
    return f"{PROMPT_SCHEMA_VERSION}:{STAGE2_SCHEMA_VERSION}:{STAGE2_INSTRUCTION_VERSION}"


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        for n in names:
            if n in obj and obj[n] is not None:
                return obj[n]
        return default
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    return default


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def content_hash_text(text: str | None) -> str | None:
    if text is None or not str(text).strip():
        return None
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def build_document_manifest(opportunity: Any) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    notice = str(_field(opportunity, "notice_id", default="") or "")
    analysis = _as_dict(_field(opportunity, "analysis", default={}))
    sam_raw = _as_dict(_field(opportunity, "sam_raw", "raw", default={}))

    structured_bits = {
        "title": _field(opportunity, "title", default=None),
        "agency": _field(opportunity, "agency", default=None),
        "naics": _field(opportunity, "naics_code", "naics", default=None),
        "set_aside": _field(opportunity, "set_aside", default=None),
        "due_date": str(_field(opportunity, "due_date", default="") or "") or None,
        "estimated_value": _field(opportunity, "estimated_value", default=None),
        "description": (_field(opportunity, "description", default=None) or "")[:2000] or None,
    }
    structured_text = json.dumps({k: v for k, v in structured_bits.items() if v is not None}, sort_keys=True)
    docs.append(
        {
            "document_id": "structured_opportunity",
            "filename": None,
            "title": "Structured opportunity fields",
            "document_type": "structured_fields",
            "source": "CONTRACT",
            "source_reference": notice or None,
            "page_count": None,
            "text_available": True,
            "retrieved_at": None,
            "content_hash": content_hash_text(structured_text),
            "duplicate_of": None,
            "amendment_status": sam_raw.get("amendment_number") or sam_raw.get("amendmentNumber"),
            "text": structured_text,
        }
    )

    desc = str(_field(opportunity, "description", default="") or "").strip()
    if not desc and sam_raw.get("descriptionText"):
        desc = str(sam_raw.get("descriptionText")).strip()
    if desc:
        docs.append(
            {
                "document_id": "solicitation_description",
                "filename": None,
                "title": "Solicitation description",
                "document_type": "description",
                "source": "SAM" if sam_raw else "CONTRACT",
                "source_reference": notice or None,
                "page_count": None,
                "text_available": True,
                "retrieved_at": None,
                "content_hash": content_hash_text(desc),
                "duplicate_of": None,
                "amendment_status": None,
                "text": desc,
            }
        )

    attachment_text = str(_field(opportunity, "attachment_text", default="") or "").strip()
    if attachment_text:
        docs.append(
            {
                "document_id": "attachment_text",
                "filename": None,
                "title": "Extracted attachment text",
                "document_type": "attachment_extract",
                "source": "ATTACHMENT_EXTRACT",
                "source_reference": notice or None,
                "page_count": None,
                "text_available": True,
                "retrieved_at": (
                    str(_field(opportunity, "attachment_text_extracted_at", default="") or "") or None
                ),
                "content_hash": content_hash_text(attachment_text),
                "duplicate_of": None,
                "amendment_status": None,
                "text": attachment_text,
            }
        )

    for key, dtype in (("pws_text", "pws_extract"), ("solicitation_text_extract", "solicitation_extract")):
        blob = analysis.get(key)
        if isinstance(blob, str) and blob.strip():
            docs.append(
                {
                    "document_id": key,
                    "filename": None,
                    "title": key,
                    "document_type": dtype,
                    "source": "ANALYSIS_EXTRACT",
                    "source_reference": notice or None,
                    "page_count": None,
                    "text_available": True,
                    "retrieved_at": None,
                    "content_hash": content_hash_text(blob),
                    "duplicate_of": None,
                    "amendment_status": None,
                    "text": blob,
                }
            )

    return dedupe_manifest(docs)


def dedupe_manifest(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for doc in docs:
        ch = doc.get("content_hash")
        if ch and ch in seen:
            clone = dict(doc)
            clone["duplicate_of"] = seen[ch]
            clone["text"] = None
            clone["text_available"] = False
            out.append(clone)
            continue
        if ch:
            seen[ch] = str(doc.get("document_id"))
        clone = dict(doc)
        clone.setdefault("duplicate_of", None)
        out.append(clone)
    return out


def _manifest_without_bodies(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in d.items() if k != "text"} for d in docs]


def build_stage2_evidence_corpus(manifest: list[dict[str, Any]]) -> dict[str, Any]:
    parts: list[str] = []
    included_ids: list[str] = []
    for doc in manifest:
        if doc.get("duplicate_of"):
            continue
        text = doc.get("text")
        if not text:
            continue
        did = str(doc.get("document_id"))
        included_ids.append(did)
        parts.append(f"=== DOCUMENT {did} ({doc.get('document_type')}) ===\n{text}")
    corpus = "\n\n".join(parts)
    return {
        "text": corpus,
        "included_document_ids": included_ids,
        "char_count": len(corpus),
        "content_hash": content_hash_text(corpus),
    }


def check_stage2_input_size(char_count: int) -> dict[str, Any]:
    limit = max_input_chars_for_stage(FunnelStage.STAGE_2) or int(
        os.getenv("AI_STAGE2_MAX_INPUT_CHARS", "80000")
    )
    if char_count > limit:
        return {
            "ok": False,
            "limit": limit,
            "chars": char_count,
            "reason_code": "STAGE2_NEEDS_ADDITIONAL_DOCUMENT_PROCESSING",
            "complete_review_claimed": False,
        }
    return {"ok": True, "limit": limit, "chars": char_count, "complete_review_claimed": True}


def can_run_stage2(
    opportunity: Any,
    *,
    stage0: dict[str, Any] | None = None,
    stage1: dict[str, Any] | None = None,
    stage1_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stage 2 requires Stage 0 ADVANCE + current Stage 1 HIT (not stale / not ORM)."""
    from ai_stage1 import resolve_current_stage1_result

    s0 = stage0 if stage0 is not None else stage0_evaluate(opportunity)
    if s0.get("decision") == "REJECT" or not s0.get("advance"):
        return {"allowed": False, "reason": "stage0_reject", "stage0": s0}

    status = str(_field(opportunity, "status", default="") or "").lower()
    if status in {"cancelled", "canceled", "expired", "deleted", "archived", "rejected"}:
        return {"allowed": False, "reason": "terminal_status", "status": status}

    resolution = stage1_resolution
    if resolution is None and stage1 is None:
        resolution = resolve_current_stage1_result(opportunity, stage0=s0)

    s1: dict[str, Any] | None = None
    if resolution is not None:
        if resolution.get("status") != "HIT" or not resolution.get("result"):
            return {
                "allowed": False,
                "reason": "STAGE1_CURRENT_RESULT_MISSING",
                "stage0": s0,
                "stage1_resolution": {
                    "status": resolution.get("status"),
                    "fingerprint": resolution.get("fingerprint"),
                    "historical_stale": resolution.get("historical_stale"),
                },
            }
        s1 = resolution["result"]
    elif stage1 is not None:
        if stage1.get("_fixture_authorize") is True:
            s1 = {k: v for k, v in stage1.items() if k != "_fixture_authorize"}
        else:
            return {
                "allowed": False,
                "reason": "STAGE1_CURRENT_RESULT_MISSING",
                "stage0": s0,
                "note": "Explicit stage1 without current resolution or fixture marker is not authorization",
            }
    else:
        return {"allowed": False, "reason": "STAGE1_CURRENT_RESULT_MISSING", "stage0": s0}

    if s1.get("advance") is False:
        return {"allowed": False, "reason": "stage1_reject", "stage1": s1}

    if s1.get("advance") is True or s1.get("reason_code") in {
        "NEEDS_STAGE2_DOCUMENT_REVIEW",
        "UNCERTAIN_ADVANCE",
        "FIT",
    }:
        return {"allowed": True, "reason": "ok", "stage0": s0, "stage1": s1, "stage1_resolution": resolution}

    if s1.get("pursue") is True:
        return {"allowed": True, "reason": "ok", "stage0": s0, "stage1": s1, "stage1_resolution": resolution}

    return {"allowed": False, "reason": "stage1_not_ready", "stage1": s1}


def evidence_text_in_source(evidence_text: str | None, corpus: str) -> bool:
    if not evidence_text or not str(evidence_text).strip():
        return False
    needle = re.sub(r"\s+", " ", str(evidence_text).strip())
    hay = re.sub(r"\s+", " ", corpus)
    return needle.lower() in hay.lower()


def validate_and_envelope_fact(
    raw_fact: Any,
    *,
    field: str,
    corpus: str,
    manifest_ids: set[str],
) -> dict[str, Any]:
    if not isinstance(raw_fact, dict):
        return unknown_fact(source_field=field)

    value = raw_fact.get("value")
    if value is None or value == "":
        return unknown_fact(source_field=field)

    if field.endswith("quantity") or field == "quantity" or field.endswith(".quantity"):
        if isinstance(value, str) and parse_quantity(value) is None and not re.fullmatch(
            r"\d+(\.\d+)?", str(value).strip()
        ):
            if re.search(r"building|zip|naics", str(value), re.I):
                return unknown_fact(source_field=field, notes="Rejected non-quantity text")
        if isinstance(value, (int, float)) and value <= 0:
            return unknown_fact(source_field=field)

    evidence = raw_fact.get("evidence_text")
    doc_id = raw_fact.get("document_id")
    locator = raw_fact.get("locator")
    conf = raw_fact.get("confidence") or "MEDIUM"

    if doc_id and str(doc_id) not in manifest_ids:
        return assessment_fact(
            value,
            source_type="AI",
            source_field=field,
            confidence="LOW",
            notes="document_id not in manifest — cannot verify",
        ) | {"needs_verification": True}

    if evidence_text_in_source(evidence, corpus):
        return verified_fact(
            value,
            source_type="SOLICITATION",
            source_field=field,
            source_reference=str(doc_id) if doc_id else None,
            confidence="HIGH" if str(conf).upper() == "HIGH" else "MEDIUM",
        ) | {
            "evidence_text": str(evidence)[:240],
            "document_id": doc_id,
            "locator": locator,
            "code": raw_fact.get("code"),
        }

    return assessment_fact(
        value,
        source_type="AI",
        source_field=field,
        confidence="LOW",
        notes="AI value without validated evidence_text in supplied corpus",
    ) | {
        "evidence_text": None,
        "document_id": doc_id,
        "locator": locator,
        "needs_verification": True,
        "code": raw_fact.get("code"),
    }


def validate_fatal_facts(
    fatal_facts: list[Any] | None,
    *,
    corpus: str,
    manifest_ids: set[str],
) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    if not fatal_facts:
        return validated
    for item in fatal_facts:
        if not isinstance(item, dict):
            continue
        evidence = item.get("evidence_text")
        doc_id = item.get("document_id")
        if doc_id and str(doc_id) not in manifest_ids:
            continue
        if not evidence_text_in_source(evidence, corpus):
            continue
        validated.append(
            {
                "code": str(item.get("code") or "FATAL")[:64],
                "summary": str(item.get("summary") or "")[:200],
                "evidence_text": str(evidence)[:240],
                "document_id": doc_id,
                "locator": item.get("locator"),
                "status": STATUS_VERIFIED,
                "source_type": "SOLICITATION",
            }
        )
    return validated


def empty_facts_tree() -> dict[str, Any]:
    sections = {
        "identity": ("solicitation_number", "notice_id", "agency", "title"),
        "procurement": (
            "category",
            "naics",
            "psc",
            "set_aside",
            "contract_type",
            "evaluation_method",
            "award_basis",
        ),
        "dates": (
            "response_deadline",
            "site_visit_date",
            "question_deadline",
            "performance_start",
            "performance_end",
            "delivery_deadline",
        ),
        "scope": (
            "summary",
            "products_services",
            "quantity",
            "unit_of_measure",
            "exact_model",
            "brand_name_or_equal",
            "salient_characteristics",
        ),
        "execution": (
            "delivery_location",
            "place_of_performance",
            "installation_required",
            "removal_disposal_required",
            "training_required",
            "maintenance_required",
            "subcontracting_allowed",
            "site_access_requirements",
            "staffing_labor_requirements",
            "schedule_frequency",
        ),
        "compliance": (
            "bond_requirement",
            "insurance_requirement",
            "licenses",
            "certifications",
            "security_background",
            "wage_determination",
            "domestic_sourcing",
            "nmr_relevance",
            "manufacturer_authorization",
            "mandatory_registrations",
            "special_clauses",
        ),
        "submission": (
            "submission_method",
            "required_forms",
            "technical_proposal_required",
            "past_performance_required",
            "pricing_format",
            "reps_and_certs",
            "page_limits",
            "mandatory_attachments",
        ),
        "economic_evidence": ("stated_value", "funding_ceiling", "government_estimate", "option_periods"),
    }
    tree: dict[str, Any] = {}
    for section, keys in sections.items():
        tree[section] = {k: unknown_fact(source_field=f"{section}.{k}") for k in keys}
    return tree


def seed_structured_facts(opportunity: Any) -> dict[str, Any]:
    sam_raw = _as_dict(_field(opportunity, "sam_raw", "raw", default={}))
    seeded: dict[str, Any] = {}

    def put(code: str, value: Any, *, source_field: str, source_type: str = "CONTRACT") -> None:
        if value is None or value == "":
            return
        seeded[code] = verified_fact(
            value,
            source_type=source_type,
            source_field=source_field,
            source_reference=str(_field(opportunity, "notice_id", default="") or "") or None,
            confidence="HIGH",
        ) | {"code": code, "document_id": "structured_opportunity", "evidence_text": None, "locator": None}

    put("NOTICE_ID", _field(opportunity, "notice_id", default=None), source_field="notice_id")
    put("TITLE", _field(opportunity, "title", default=None), source_field="title")
    put("AGENCY", _field(opportunity, "agency", default=None), source_field="agency")
    put(
        "NAICS",
        _field(opportunity, "naics_code", "naics", default=None) or sam_raw.get("naicsCode"),
        source_field="naics_code",
        source_type="SAM",
    )
    put("PSC", sam_raw.get("classificationCode"), source_field="sam_raw.classificationCode", source_type="SAM")
    put(
        "SET_ASIDE",
        _field(opportunity, "set_aside", default=None) or sam_raw.get("typeOfSetAsideDescription"),
        source_field="set_aside",
        source_type="SAM",
    )
    put("PLACE_OF_PERFORMANCE", _field(opportunity, "location", default=None), source_field="location")
    put(
        "SOLICITATION_NUMBER",
        sam_raw.get("solicitationNumber"),
        source_field="sam_raw.solicitationNumber",
        source_type="SAM",
    )

    award = sam_raw.get("award") if isinstance(sam_raw.get("award"), dict) else {}
    if award and award.get("amount") is not None:
        money = parse_money(award.get("amount"), allow_loose=False)
        if money is not None:
            put("STATED_VALUE", money, source_field="sam_raw.award.amount", source_type="SAM")

    due = _field(opportunity, "due_date", default=None) or sam_raw.get("responseDeadLine")
    if due:
        put("RESPONSE_DEADLINE", str(due), source_field="due_date", source_type="SAM")

    return seeded


def apply_facts_to_tree(tree: dict[str, Any], fact_by_code: dict[str, Any]) -> dict[str, Any]:
    for code, fact in fact_by_code.items():
        path = FACT_CODE_TO_PATH.get(code)
        if not path:
            continue
        section, key = path
        tree.setdefault(section, {})[key] = fact
    return tree


def build_research_needs(
    facts: dict[str, Any],
    *,
    economic_requirements: dict[str, Any],
) -> list[dict[str, Any]]:
    needs: list[dict[str, Any]] = []

    def add(code: str, reason: str, *, priority: str = "HIGH", blocking: bool = True, related: str = "") -> None:
        if code not in RESEARCH_NEED_CODES:
            return
        needs.append(
            {"code": code, "reason": reason, "priority": priority, "blocking": blocking, "related_field": related}
        )

    qty = (facts.get("scope") or {}).get("quantity") or {}
    if str(qty.get("status") or "") in {STATUS_UNKNOWN, ""} or qty.get("value") is None:
        add("QUANTITY_UNCLEAR", "Quantity not established by solicitation evidence", related="scope.quantity")

    bond = (facts.get("compliance") or {}).get("bond_requirement") or {}
    if str(bond.get("status") or "") in {STATUS_UNKNOWN, STATUS_ASSESSMENT} or bond.get("value") is None:
        add("BOND_REQUIREMENT_UNCLEAR", "Bond requirement unknown", related="compliance.bond_requirement")

    lic = (facts.get("compliance") or {}).get("licenses") or {}
    if str(lic.get("status") or "") in {STATUS_UNKNOWN, STATUS_ASSESSMENT} or lic.get("value") is None:
        add("LICENSE_REQUIREMENT_UNCLEAR", "License requirements unknown", related="compliance.licenses")

    auth = (facts.get("compliance") or {}).get("manufacturer_authorization") or {}
    if auth.get("value") in (True, "required", "REQUIRED") or (
        isinstance(auth.get("value"), str) and "authoriz" in str(auth.get("value")).lower()
    ):
        add(
            "MANUFACTURER_AUTHORIZATION_REQUIRED",
            "Manufacturer/dealer authorization appears relevant or unclear",
            related="compliance.manufacturer_authorization",
        )

    delivery = (facts.get("execution") or {}).get("delivery_location") or {}
    pop = (facts.get("execution") or {}).get("place_of_performance") or {}
    if delivery.get("value") is None and pop.get("value") is None:
        add(
            "DELIVERY_REQUIREMENT_UNCLEAR",
            "Delivery/place of performance incomplete",
            related="execution.delivery_location",
            priority="MEDIUM",
        )

    options = (facts.get("economic_evidence") or {}).get("option_periods") or {}
    if options.get("value") is None:
        add(
            "OPTION_PERIOD_UNCLEAR",
            "Option periods not established",
            related="economic_evidence.option_periods",
            priority="MEDIUM",
            blocking=False,
        )

    add(
        "HISTORICAL_PRICING_REQUIRED",
        "Historical pricing not established in Stage 2 (Stage 2 does not invent it)",
        priority="MEDIUM",
        blocking=False,
        related="historical",
    )
    add(
        "COMPETITION_HISTORY_REQUIRED",
        "Competition/bid history not established in Stage 2",
        priority="LOW",
        blocking=False,
        related="competition",
    )

    for cat, item in ((economic_requirements or {}).get("costs") or {}).items():
        if str(item.get("status") or "") == COST_REQUIRED_UNKNOWN:
            code_map = {
                "supplier": "SUPPLIER_QUOTE_REQUIRED",
                "freight": "FREIGHT_REQUIRED",
                "subcontract": "SUBCONTRACTOR_QUOTE_REQUIRED",
                "financing": "FINANCING_TERMS_REQUIRED",
                "installation": "INSTALLATION_COST_REQUIRED",
            }
            code = code_map.get(cat)
            if code:
                add(code, f"{cat} cost required but unknown", related=f"economic.{cat}")
            if cat == "financing":
                add("NO_PG_VERIFICATION_REQUIRED", "No-PG financing path not verified", related="economic.financing")
                add(
                    "ZERO_UPFRONT_VERIFICATION_REQUIRED",
                    "Zero-upfront financing path not verified",
                    related="economic.financing",
                )

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for n in needs:
        if n["code"] in seen:
            continue
        seen.add(n["code"])
        unique.append(n)
    return unique


def assign_economic_requirements(
    facts: dict[str, Any],
    *,
    stage0: dict[str, Any] | None = None,
    stage1: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map Stage 2 facts → cost requirements using CANONICAL execution class only."""
    from economic_integrity import (
        map_cost_requirements_for_canonical_class,
        resolve_canonical_execution_class,
    )

    cat_fact = (facts.get("procurement") or {}).get("category") or {}
    stage2_cat = cat_fact.get("value")

    install_fact = (facts.get("execution") or {}).get("installation_required") or {}
    install_req = None
    if install_fact.get("value") is True:
        install_req = True
    elif install_fact.get("value") is False and str(install_fact.get("status")) == STATUS_VERIFIED:
        install_req = False

    scope = facts.get("scope") or {}
    has_product = False
    for key in ("exact_model", "brand_name_or_equal", "products_services"):
        f = scope.get(key) or {}
        if f.get("value") is not None and str(f.get("status")) == STATUS_VERIFIED:
            # Product procurement evidence only when exact model / brand-name procurement is explicit
            if key in {"exact_model", "brand_name_or_equal"}:
                has_product = True
                break

    resolved = resolve_canonical_execution_class(
        stage1_category=(stage1 or {}).get("category"),
        stage0_classification=(stage0 or {}).get("classification"),
        stage2_category_value=stage2_cat,
        installation_required=install_req,
        has_product_procurement_evidence=has_product,
    )
    costs = map_cost_requirements_for_canonical_class(
        canonical_class=resolved["canonical_execution_class"],
        installation_required=install_req,
        has_product_procurement_evidence=has_product,
        requires_financing=True,
    )

    stated = (facts.get("economic_evidence") or {}).get("stated_value") or {}
    return {
        "canonical_execution_class": resolved["canonical_execution_class"],
        "classification_resolution": resolved,
        "costs": costs,
        "revenue_evidence": {
            "stated_value": stated,
            "is_current_revenue": False,
            "notes": "Stage 2 economic evidence only — not a supplier quote or actual profit input",
        },
        "actual_profit": None,
        "actual_profit_status": "INCOMPLETE",
    }


def completeness_counts(facts: dict[str, Any], research_needs: list[dict[str, Any]]) -> dict[str, Any]:
    critical_fields = [
        ("scope", "quantity"),
        ("compliance", "bond_requirement"),
        ("compliance", "licenses"),
        ("execution", "place_of_performance"),
        ("dates", "response_deadline"),
        ("procurement", "set_aside"),
    ]
    known = 0
    unknown = 0
    for section, key in critical_fields:
        fact = (facts.get(section) or {}).get(key) or {}
        if str(fact.get("status")) == STATUS_VERIFIED and fact.get("value") is not None:
            known += 1
        else:
            unknown += 1
    return {
        "critical_known": known,
        "critical_unknown": unknown,
        "blocking_research_needs": sum(1 for n in research_needs if n.get("blocking")),
        "research_needs_total": len(research_needs),
    }


def normalize_stage2_result(
    raw: dict[str, Any],
    *,
    corpus: str,
    manifest: list[dict[str, Any]],
    opportunity: Any = None,
    stage0: dict[str, Any] | None = None,
    stage1: dict[str, Any] | None = None,
    oversize: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest_ids = {str(d.get("document_id")) for d in manifest if d.get("document_id")}

    fact_by_code: dict[str, Any] = {}
    if opportunity is not None:
        fact_by_code.update(seed_structured_facts(opportunity))

    for item in raw.get("facts") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "")
        if code not in FACT_CODE_TO_PATH:
            continue
        path = FACT_CODE_TO_PATH[code]
        field = f"{path[0]}.{path[1]}"
        enveloped = validate_and_envelope_fact(
            {**item, "code": code}, field=field, corpus=corpus, manifest_ids=manifest_ids
        )
        if code in fact_by_code and str(enveloped.get("status")) != STATUS_VERIFIED:
            continue
        if str(enveloped.get("status")) == STATUS_UNKNOWN:
            continue
        fact_by_code[code] = enveloped

    tree = apply_facts_to_tree(empty_facts_tree(), fact_by_code)

    for money_key in ("stated_value", "funding_ceiling", "government_estimate"):
        mf = tree["economic_evidence"].get(money_key) or {}
        if mf.get("value") is not None and str(mf.get("status")) == STATUS_VERIFIED:
            if parse_money(mf.get("value"), allow_loose=False) is None and parse_money(
                mf.get("value"), allow_loose=True
            ) is None:
                tree["economic_evidence"][money_key] = unknown_fact(
                    source_field=f"economic_evidence.{money_key}"
                )

    fatal = validate_fatal_facts(
        raw.get("fatal_candidates") or raw.get("fatal_facts"), corpus=corpus, manifest_ids=manifest_ids
    )
    economic_requirements = assign_economic_requirements(tree, stage0=stage0, stage1=stage1)
    research_needs = build_research_needs(tree, economic_requirements=economic_requirements)
    completeness = completeness_counts(tree, research_needs)

    from product_deal import (
        build_product_requirements_view,
        build_product_research_needs,
        paid_work_priority_rank,
        resolve_core_fit,
    )

    core_fit_meta = resolve_core_fit(
        stage1_category=(stage1 or {}).get("category"),
        stage0_classification=(stage0 or {}).get("classification"),
        stage2_category_value=((tree.get("procurement") or {}).get("category") or {}).get("value"),
        canonical_class=(economic_requirements or {}).get("canonical_execution_class"),
    )
    product_requirements = build_product_requirements_view(tree, opportunity=opportunity)
    product_research_needs = build_product_research_needs(
        product_requirements=product_requirements,
        economic_requirements=economic_requirements,
        core_fit=core_fit_meta["core_fit"],
    )

    if oversize and not oversize.get("ok"):
        advance = True
        reason_code = "STAGE2_NEEDS_ADDITIONAL_DOCUMENT_PROCESSING"
    elif fatal:
        advance = False
        reason_code = "STAGE2_FATAL_VERIFIED"
    elif any(n.get("blocking") for n in research_needs):
        advance = True
        reason_code = "STAGE2_RESEARCH_REQUIRED"
    elif completeness["critical_unknown"] == 0:
        advance = True
        reason_code = "STAGE2_EVIDENCE_COMPLETE"
    else:
        advance = True
        reason_code = "STAGE2_INSUFFICIENT_EVIDENCE_ADVANCE"

    extracted_list = [
        {
            "code": code,
            "value": fact.get("value"),
            "status": fact.get("status"),
            "document_id": fact.get("document_id"),
            "locator": fact.get("locator"),
            "evidence_text": fact.get("evidence_text"),
            "confidence": fact.get("confidence"),
        }
        for code, fact in fact_by_code.items()
        if fact.get("value") is not None
    ]

    return {
        "schema_version": STAGE2_SCHEMA_VERSION,
        "instruction_version": STAGE2_INSTRUCTION_VERSION,
        "funnel_stage": FunnelStage.STAGE_2.value,
        "facts": tree,
        "extracted_facts": extracted_list,
        "documents_reviewed": _manifest_without_bodies(manifest),
        "requirements": {
            "compliance": tree.get("compliance"),
            "submission": tree.get("submission"),
            "execution": tree.get("execution"),
        },
        "product_requirements": product_requirements,
        "product_research_needs": product_research_needs,
        "core_fit": core_fit_meta["core_fit"],
        "product_purity": core_fit_meta.get("product_purity"),
        "paid_work_priority_rank": paid_work_priority_rank(core_fit_meta),
        "core_fit_status": "POLICY",
        "economic_requirements": economic_requirements,
        "research_needs": research_needs,
        "fatal_facts": fatal,
        "completeness": completeness,
        "advance": advance,
        "reason_code": reason_code,
        "stage0": stage0,
        "stage1_summary": {
            "category": (stage1 or {}).get("category"),
            "reason_code": (stage1 or {}).get("reason_code"),
            "advance": (stage1 or {}).get("advance"),
            "core_fit": (stage1 or {}).get("core_fit"),
        },
        "model_tier": ModelTier.CHEAP.value,
        "web_search": False,
        "oversize": oversize,
    }


def build_stage2_input(
    opportunity: Any,
    *,
    stage0: dict[str, Any] | None = None,
    stage1: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = build_document_manifest(opportunity)
    corpus = build_stage2_evidence_corpus(manifest)
    size = check_stage2_input_size(corpus["char_count"])
    meta = {
        "notice_id": _field(opportunity, "notice_id", default=None),
        "stage0_decision": (stage0 or {}).get("decision"),
        "stage1_reason": (stage1 or {}).get("reason_code"),
        "documents": _manifest_without_bodies(manifest),
        "included_document_ids": corpus["included_document_ids"],
        "evidence_complete": size.get("complete_review_claimed"),
        "schema": STAGE2_SCHEMA_VERSION,
    }
    if size.get("ok"):
        text = "STAGE 2 EVIDENCE PACKAGE\n" + json.dumps(meta, default=str) + "\n\n" + (corpus["text"] or "")
    else:
        text = (
            "STAGE 2 EVIDENCE PACKAGE EXCEEDS INPUT LIMIT.\n"
            + json.dumps({**meta, "oversize": size}, default=str)
            + "\nDo not invent requirements. Return facts=[] and fatal_candidates=[]."
        )
    return {
        "text": text,
        "manifest": manifest,
        "corpus": corpus,
        "oversize": size,
        "char_count": len(text),
        "source_hash": hash_source_material(
            corpus.get("content_hash"),
            [d.get("content_hash") for d in manifest],
            STAGE2_SCHEMA_VERSION,
            STAGE2_INSTRUCTION_VERSION,
        ),
    }


def measure_stage2_output_fixtures() -> dict[str, Any]:
    from ai_pricing import estimate_tokens_from_chars

    empty = {"facts": [], "fatal_candidates": []}
    typical = {
        "facts": [
            {
                "code": "RESPONSE_DEADLINE",
                "value": "2026-03-01",
                "document_id": "attachment_text",
                "locator": "p1",
                "evidence_text": "OFFER DUE DATE/ LOCAL TIME 01 Mar 2026",
                "confidence": "HIGH",
            },
            {
                "code": "WAGE_DETERMINATION",
                "value": True,
                "document_id": "attachment_text",
                "locator": "p2",
                "evidence_text": "Wage Determination applicable to this solicitation",
                "confidence": "HIGH",
            },
            {
                "code": "PLACE_OF_PERFORMANCE",
                "value": "Kapolei, HI",
                "document_id": "structured_opportunity",
                "locator": None,
                "evidence_text": "Kapolei, HI",
                "confidence": "MEDIUM",
            },
            {
                "code": "SCOPE_SUMMARY",
                "value": "groundskeeping services",
                "document_id": "attachment_text",
                "locator": None,
                "evidence_text": "groundskeeping services",
                "confidence": "HIGH",
            },
            {
                "code": "SUBMISSION_METHOD",
                "value": "email",
                "document_id": "attachment_text",
                "locator": None,
                "evidence_text": "Submit quotes via email",
                "confidence": "MEDIUM",
            },
        ],
        "fatal_candidates": [],
    }
    dense_facts = []
    for i, code in enumerate(FACT_CODES[:24]):
        dense_facts.append(
            {
                "code": code,
                "value": True if i % 3 == 0 else f"value-{i}",
                "document_id": "attachment_text",
                "locator": f"p{1 + (i % 9)}",
                "evidence_text": ("e" * 80) + f"-{code}",
                "confidence": "HIGH",
            }
        )
    dense = {"facts": dense_facts, "fatal_candidates": []}
    very_dense_facts = []
    for i, code in enumerate(FACT_CODES[:40]):
        very_dense_facts.append(
            {
                "code": code,
                "value": f"val-{code}"[:40],
                "document_id": "attachment_text",
                "locator": f"sec-{i}",
                "evidence_text": ("q" * 180) + f"-{code}",
                "confidence": "HIGH",
            }
        )
    very_dense = {
        "facts": very_dense_facts,
        "fatal_candidates": [
            {
                "code": "BOND_UNACCEPTABLE",
                "summary": "Performance bond required",
                "document_id": "attachment_text",
                "locator": "p14",
                "evidence_text": "Contractor shall provide a performance bond equal to 100 percent",
            }
        ],
    }

    def pack(name: str, obj: dict[str, Any]) -> dict[str, Any]:
        s = json.dumps(obj, separators=(",", ":"))
        return {"name": name, "chars": len(s), "est_tokens": estimate_tokens_from_chars(len(s))}

    fixtures = [
        pack("empty", empty),
        pack("typical", typical),
        pack("dense", dense),
        pack("very_dense", very_dense),
    ]
    max_tokens = max(f["est_tokens"] for f in fixtures)
    recommended = int(((max_tokens * 1.25) + 99) // 100 * 100)
    recommended = max(recommended, 1500)
    return {
        "fixtures": fixtures,
        "max_fixture_est_tokens": max_tokens,
        "recommended_ceiling": recommended,
        "configured_ceiling": STAGE2_MAX_OUTPUT_TOKENS,
    }


def record_stage2_metric(entry: dict[str, Any]) -> None:
    try:
        from database import SessionLocal
        from models import AppSetting

        key = "ai_stage2_metrics"
        session = SessionLocal()
        try:
            row = session.query(AppSetting).filter_by(key=key).first()
            data: dict[str, Any] = {
                "stage2_attempted": 0,
                "stage2_cache_hits": 0,
                "stage2_api_calls": 0,
                "stage2_parse_failures": 0,
                "stage2_advanced": 0,
                "stage2_rejected_verified_fatal": 0,
                "stage2_research_required": 0,
                "stage2_input_tokens": 0,
                "stage2_output_tokens": 0,
                "stage2_cost_usd": 0.0,
            }
            if row and row.value:
                try:
                    parsed = json.loads(row.value)
                    if isinstance(parsed, dict):
                        data.update(parsed)
                except json.JSONDecodeError:
                    pass
            data["stage2_attempted"] = int(data.get("stage2_attempted") or 0) + 1
            if entry.get("cache_hit"):
                data["stage2_cache_hits"] = int(data.get("stage2_cache_hits") or 0) + 1
            else:
                data["stage2_api_calls"] = int(data.get("stage2_api_calls") or 0) + 1
            if entry.get("parse_failure"):
                data["stage2_parse_failures"] = int(data.get("stage2_parse_failures") or 0) + 1
            if entry.get("advance"):
                data["stage2_advanced"] = int(data.get("stage2_advanced") or 0) + 1
            if entry.get("reason_code") == "STAGE2_FATAL_VERIFIED":
                data["stage2_rejected_verified_fatal"] = int(data.get("stage2_rejected_verified_fatal") or 0) + 1
            if entry.get("reason_code") == "STAGE2_RESEARCH_REQUIRED":
                data["stage2_research_required"] = int(data.get("stage2_research_required") or 0) + 1
            data["stage2_input_tokens"] = int(data.get("stage2_input_tokens") or 0) + int(entry.get("input_tokens") or 0)
            data["stage2_output_tokens"] = int(data.get("stage2_output_tokens") or 0) + int(
                entry.get("output_tokens") or 0
            )
            data["stage2_cost_usd"] = float(data.get("stage2_cost_usd") or 0) + float(
                entry.get("estimated_cost_usd") or 0
            )
            data["updated_at"] = now_utc().isoformat()
            serialized = json.dumps(data)
            if row:
                row.value = serialized
            else:
                session.add(AppSetting(key=key, value=serialized))
            session.commit()
        finally:
            session.close()
    except Exception:
        logger.debug("stage2 metrics skipped", exc_info=True)


def get_stage2_metrics() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        session = SessionLocal()
        try:
            row = session.query(AppSetting).filter_by(key="ai_stage2_metrics").first()
            if row and row.value:
                return json.loads(row.value)
        finally:
            session.close()
    except Exception:
        pass
    return {}


def run_stage2_evidence(
    opportunity: Any,
    *,
    stage0: dict[str, Any] | None = None,
    stage1: dict[str, Any] | None = None,
    stage1_resolution: dict[str, Any] | None = None,
    automatic: bool = False,
) -> dict[str, Any]:
    from openai_runtime import create_response, extract_json_object, get_last_response_meta

    gate = can_run_stage2(
        opportunity, stage0=stage0, stage1=stage1, stage1_resolution=stage1_resolution
    )
    if not gate.get("allowed"):
        return {
            "schema_version": STAGE2_SCHEMA_VERSION,
            "funnel_stage": FunnelStage.STAGE_2.value,
            "advance": False,
            "reason_code": gate.get("reason")
            if gate.get("reason") == "STAGE1_CURRENT_RESULT_MISSING"
            else "STAGE2_GATE_BLOCKED",
            "gate": gate,
            "facts": {},
            "research_needs": [],
            "fatal_facts": [],
            "economic_requirements": {},
            "web_search": False,
            "model_tier": ModelTier.CHEAP.value,
        }

    s0 = gate.get("stage0") or stage0
    s1 = gate.get("stage1") or stage1
    built = build_stage2_input(opportunity, stage0=s0, stage1=s1)

    if not built["oversize"].get("ok"):
        result = normalize_stage2_result(
            {"facts": [], "fatal_candidates": []},
            corpus=built["corpus"].get("text") or "",
            manifest=built["manifest"],
            opportunity=opportunity,
            stage0=s0,
            stage1=s1,
            oversize=built["oversize"],
        )
        result["cache_hit"] = False
        result["incremental_cost_usd"] = 0.0
        record_stage2_metric(
            {
                "cache_hit": True,
                "advance": True,
                "reason_code": result["reason_code"],
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_usd": 0.0,
                "api_skipped_oversize": True,
            }
        )
        return result

    content = [{"type": "input_text", "text": built["text"]}]
    text = create_response(
        task=STAGE2_TASK,
        instructions=STAGE2_INSTRUCTIONS,
        content=content,
        max_output_tokens=STAGE2_MAX_OUTPUT_TOKENS,
        web_search=False,
        notice_id=str(_field(opportunity, "notice_id", default="") or "") or None,
        funnel_stage=FunnelStage.STAGE_2.value,
        model_tier=ModelTier.CHEAP,
        automatic=automatic,
        use_cache=True,
        schema_version=stage2_schema_fingerprint_component(),
        text_format=stage2_response_format(),
    )

    parse_failure = False
    try:
        raw = extract_json_object(text)
    except Exception:
        parse_failure = True
        raw = {"facts": [], "fatal_candidates": []}

    result = normalize_stage2_result(
        raw if isinstance(raw, dict) else {"facts": [], "fatal_candidates": []},
        corpus=built["corpus"].get("text") or "",
        manifest=built["manifest"],
        opportunity=opportunity,
        stage0=s0,
        stage1=s1,
        oversize=built["oversize"],
    )
    if parse_failure:
        result["advance"] = True
        result["reason_code"] = "STAGE2_PARSE_ERROR_ADVANCE"

    meta = get_last_response_meta()
    cache_hit = bool(meta.get("cache_hit"))
    result["cache_hit"] = cache_hit
    result["incremental_cost_usd"] = 0.0 if cache_hit else float(meta.get("estimated_cost_usd") or 0.0)
    result["model"] = model_for_tier(ModelTier.CHEAP)

    record_stage2_metric(
        {
            "cache_hit": cache_hit,
            "parse_failure": parse_failure,
            "advance": result.get("advance"),
            "reason_code": result.get("reason_code"),
            "input_tokens": 0 if cache_hit else int(meta.get("input_tokens") or 0),
            "output_tokens": 0 if cache_hit else int(meta.get("output_tokens") or 0),
            "estimated_cost_usd": result["incremental_cost_usd"],
        }
    )
    return result
