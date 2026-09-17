"""Product-first deal model: core fit, requirements view, research planner.

PRIMARY MISSION: government PRODUCT resale (tangible goods).
Service/subcontract paths remain supported but are SECONDARY.

Zero external APIs. No invented facts.
POLICY classifications are labeled as such — never procurement facts.
"""

from __future__ import annotations
from application_clock import now_utc, today_local

from datetime import date, datetime, timezone
from typing import Any

from ai_funnel import (
    CLASS_CONSTRUCTION,
    CLASS_LABOR_HEAVY,
    CLASS_PRODUCT_PLUS_SERVICE,
    CLASS_PRODUCT_RESELL,
    CLASS_SPECIALIST_SERVICE,
    CLASS_SUBCONTRACTABLE_SERVICE,
    CLASS_UNKNOWN,
)
from data_integrity import STATUS_ASSESSMENT, STATUS_CALCULATED, STATUS_UNKNOWN, STATUS_VERIFIED, unknown_fact
from economic_integrity import (
    COST_REQUIRED_UNKNOWN,
    map_cost_requirements_for_canonical_class,
    resolve_canonical_execution_class,
)

# Business-policy opportunity fit (NOT a procurement fact)
FIT_CORE_PRODUCT = "CORE_PRODUCT"
FIT_SECONDARY_SERVICE = "SECONDARY_SERVICE"
FIT_UNKNOWN = "UNKNOWN"

STATUS_POLICY = "POLICY"

_PRODUCT_CORE_CLASSES = frozenset({CLASS_PRODUCT_RESELL, CLASS_PRODUCT_PLUS_SERVICE})
_SERVICE_CLASSES = frozenset(
    {
        CLASS_SUBCONTRACTABLE_SERVICE,
        CLASS_LABOR_HEAVY,
        CLASS_SPECIALIST_SERVICE,
        CLASS_CONSTRUCTION,
    }
)

PRODUCT_RESEARCH_NEED_CODES = (
    "EXACT_PRODUCT_IDENTIFICATION_REQUIRED",
    "QUANTITY_REQUIRED",
    "CURRENT_SUPPLIER_QUOTE_REQUIRED",
    "SUPPLIER_AVAILABILITY_REQUIRED",
    "FREIGHT_QUOTE_REQUIRED",
    "MANUFACTURER_AUTHORIZATION_VERIFICATION_REQUIRED",
    "CHANNEL_ELIGIBILITY_VERIFICATION_REQUIRED",
    "COUNTRY_OF_ORIGIN_VERIFICATION_REQUIRED",
    "DELIVERY_FEASIBILITY_VERIFICATION_REQUIRED",
    "FINANCING_TERMS_REQUIRED",
    "NO_PG_VERIFICATION_REQUIRED",
    "ZERO_UPFRONT_VERIFICATION_REQUIRED",
    "HISTORICAL_PRICING_REQUIRED",
    "COMPETITION_HISTORY_REQUIRED",
)

# Sibling reseller/niche tables — NEVER treated as GovCon knowledge by default.
_FORBIDDEN_SIBLING_TABLES = frozenset(
    {
        "products",
        "price_history",
        "stage1_result_cache",
        "niche_queue",
        "trends_cache",
        "search_history",
        "watchlist",
        "scrape_log",
        "credit_events",
    }
)


def resolve_core_fit(
    *,
    canonical_class: Any = None,
    stage1_category: Any = None,
    stage0_classification: Any = None,
    stage2_category_value: Any = None,
) -> dict[str, Any]:
    """
    Deterministic business-policy fit for paid-work prioritization.

    Free-text Stage 2 category cannot flip product↔service.
    Only known execution-class enums count.
    """
    resolved = resolve_canonical_execution_class(
        stage1_category=stage1_category,
        stage0_classification=stage0_classification,
        stage2_category_value=stage2_category_value,
    )
    klass = str(canonical_class or resolved["canonical_execution_class"] or CLASS_UNKNOWN).upper()
    if klass in {"PRODUCT_RESALE", "PRODUCT"}:
        klass = CLASS_PRODUCT_RESELL

    if klass == CLASS_PRODUCT_RESELL:
        fit = FIT_CORE_PRODUCT
        purity = "CLEAN"
    elif klass == CLASS_PRODUCT_PLUS_SERVICE:
        fit = FIT_CORE_PRODUCT
        purity = "PLUS_SERVICE"
    elif klass in _SERVICE_CLASSES:
        fit = FIT_SECONDARY_SERVICE
        purity = None
    else:
        fit = FIT_UNKNOWN
        purity = None

    return {
        "core_fit": fit,
        "product_purity": purity,
        "canonical_class": klass
        if klass in (_PRODUCT_CORE_CLASSES | _SERVICE_CLASSES | {CLASS_UNKNOWN})
        else CLASS_UNKNOWN,
        "status": STATUS_POLICY,
        "source_type": "BUSINESS_POLICY",
        "basis": (
            f"derived from canonical_execution_class={klass}; "
            "PRIMARY mission is PRODUCT_RESELL; services are secondary"
        ),
        "classification_resolution": resolved,
    }


def paid_work_priority_rank(core_fit_result: dict[str, Any] | str | None) -> int:
    """Lower rank = process sooner for paid AI/research. POLICY only."""
    if isinstance(core_fit_result, dict):
        fit = str(core_fit_result.get("core_fit") or FIT_UNKNOWN)
        purity = core_fit_result.get("product_purity")
    else:
        fit = str(core_fit_result or FIT_UNKNOWN)
        purity = None
    if fit == FIT_CORE_PRODUCT and purity == "CLEAN":
        return 10
    if fit == FIT_CORE_PRODUCT:
        return 20
    if fit == FIT_UNKNOWN:
        return 50
    return 100  # SECONDARY_SERVICE


def _fact_or_unknown(section: dict[str, Any] | None, key: str, *, source_field: str) -> dict[str, Any]:
    f = (section or {}).get(key)
    if isinstance(f, dict) and "status" in f:
        out = dict(f)
        out.setdefault("source_field", source_field)
        return out
    return unknown_fact(source_field=source_field)


def _status_of(fact: dict[str, Any] | None) -> str:
    if not fact:
        return STATUS_UNKNOWN
    st = str(fact.get("status") or STATUS_UNKNOWN).upper()
    if fact.get("value") is None and st == STATUS_VERIFIED:
        return STATUS_UNKNOWN
    return st


def _is_known(fact: dict[str, Any] | None) -> bool:
    st = _status_of(fact)
    return st in {STATUS_VERIFIED, STATUS_CALCULATED} and fact is not None and fact.get("value") is not None


def build_product_requirements_view(
    facts: dict[str, Any] | None,
    *,
    opportunity: Any = None,
) -> dict[str, Any]:
    """
    Deterministic PRODUCT REQUIREMENTS VIEW from validated Stage 2 facts.

    Does not call AI. Does not fill holes. ASSESSMENT stays ASSESSMENT.
    """
    tree = facts or {}
    scope = tree.get("scope") or {}
    execution = tree.get("execution") or {}
    compliance = tree.get("compliance") or {}
    procurement = tree.get("procurement") or {}
    dates = tree.get("dates") or {}
    economic = tree.get("economic_evidence") or {}
    identity = tree.get("identity") or {}

    brand_fact = _fact_or_unknown(scope, "brand_name_or_equal", source_field="scope.brand_name_or_equal")
    brand_val = brand_fact.get("value")
    brand_text = str(brand_val).lower() if brand_val is not None else ""

    brand_name_only = unknown_fact(source_field="product_requirements.brand_name_only")
    brand_name_or_equal = brand_fact
    approved_equal = unknown_fact(source_field="product_requirements.approved_equal_language")
    if _is_known(brand_fact) or _status_of(brand_fact) == STATUS_ASSESSMENT:
        if any(x in brand_text for x in ("brand name only", "brand-name only", "no substitut", "no equal")):
            brand_name_only = {
                **brand_fact,
                "value": True,
                "source_field": "product_requirements.brand_name_only",
                "notes": "Derived from brand/equal language evidence — not invented",
            }
        if any(x in brand_text for x in ("or equal", "or-equal", "brand name or equal", "approved equal")):
            approved_equal = {
                **brand_fact,
                "value": True,
                "source_field": "product_requirements.approved_equal_language",
            }

    # Manufacturer is NOT inferred from brand/model without an explicit field.
    manufacturer = unknown_fact(source_field="product_requirements.manufacturer")

    products_services = scope.get("products_services") or {}
    if products_services.get("value") is not None:
        product_description = _fact_or_unknown(
            scope, "products_services", source_field="scope.products_services"
        )
    else:
        product_description = _fact_or_unknown(scope, "summary", source_field="scope.summary")

    return {
        "schema": "product-requirements-v1",
        "status_note": "Each field retains its envelope status; ASSESSMENT is not a procurement fact",
        "product_description": product_description,
        "manufacturer": manufacturer,
        "brand": brand_fact,
        "part_model_number": _fact_or_unknown(scope, "exact_model", source_field="scope.exact_model"),
        "quantity": _fact_or_unknown(scope, "quantity", source_field="scope.quantity"),
        "unit_of_measure": _fact_or_unknown(scope, "unit_of_measure", source_field="scope.unit_of_measure"),
        "required_specifications": _fact_or_unknown(
            scope, "salient_characteristics", source_field="scope.salient_characteristics"
        ),
        "salient_characteristics": _fact_or_unknown(
            scope, "salient_characteristics", source_field="scope.salient_characteristics"
        ),
        "brand_name_only": brand_name_only,
        "brand_name_or_equal": brand_name_or_equal,
        "approved_equal_language": approved_equal,
        "country_of_origin": _fact_or_unknown(
            compliance, "domestic_sourcing", source_field="compliance.domestic_sourcing"
        ),
        "buy_american_taa": _fact_or_unknown(
            compliance, "domestic_sourcing", source_field="compliance.domestic_sourcing"
        ),
        "manufacturer_authorization_requirement": _fact_or_unknown(
            compliance, "manufacturer_authorization", source_field="compliance.manufacturer_authorization"
        ),
        "authorized_dealer_reseller_requirement": _fact_or_unknown(
            compliance, "manufacturer_authorization", source_field="compliance.manufacturer_authorization"
        ),
        "condition_new_used_remanufactured": unknown_fact(
            source_field="product_requirements.condition_new_used_remanufactured"
        ),
        "delivery_destination": _fact_or_unknown(
            execution, "delivery_location", source_field="execution.delivery_location"
        ),
        "required_delivery_date": _fact_or_unknown(
            dates, "delivery_deadline", source_field="dates.delivery_deadline"
        ),
        "fob_terms": unknown_fact(source_field="product_requirements.fob_terms"),
        "installation_requirement": _fact_or_unknown(
            execution, "installation_required", source_field="execution.installation_required"
        ),
        "warranty_requirement": unknown_fact(source_field="product_requirements.warranty_requirement"),
        "set_aside": _fact_or_unknown(procurement, "set_aside", source_field="procurement.set_aside"),
        "naics": _fact_or_unknown(procurement, "naics", source_field="procurement.naics"),
        "psc": _fact_or_unknown(procurement, "psc", source_field="procurement.psc"),
        "response_deadline": _fact_or_unknown(
            dates, "response_deadline", source_field="dates.response_deadline"
        ),
        "government_stated_estimated_value": _fact_or_unknown(
            economic, "stated_value", source_field="economic_evidence.stated_value"
        ),
        "title": _fact_or_unknown(identity, "title", source_field="identity.title"),
        "agency": _fact_or_unknown(identity, "agency", source_field="identity.agency"),
    }


def build_product_research_needs(
    *,
    product_requirements: dict[str, Any],
    economic_requirements: dict[str, Any] | None = None,
    known_current: list[dict[str, Any]] | None = None,
    core_fit: str = FIT_CORE_PRODUCT,
) -> list[dict[str, Any]]:
    """Emit product research needs only when genuinely unresolved/applicable."""
    if core_fit != FIT_CORE_PRODUCT:
        return []

    needs: list[dict[str, Any]] = []
    known_codes = {
        str(k.get("code") or k.get("knowledge_type") or "")
        for k in (known_current or [])
        if isinstance(k, dict) and str(k.get("status") or "").upper() == STATUS_VERIFIED
    }

    def add(
        code: str,
        reason: str,
        *,
        related_field: str,
        current_status: str,
        priority: str = "HIGH",
        blocking: bool = True,
    ) -> None:
        if code not in PRODUCT_RESEARCH_NEED_CODES:
            return
        if code == "CURRENT_SUPPLIER_QUOTE_REQUIRED" and "CURRENT_SUPPLIER_QUOTE" in known_codes:
            return
        if code == "SUPPLIER_AVAILABILITY_REQUIRED" and "CURRENT_SUPPLIER_QUOTE" in known_codes:
            return
        if code == "FREIGHT_QUOTE_REQUIRED" and "CURRENT_FREIGHT_QUOTE" in known_codes:
            return
        if code == "FINANCING_TERMS_REQUIRED" and "CURRENT_FINANCING_TERM" in known_codes:
            return
        needs.append(
            {
                "code": code,
                "reason": reason,
                "priority": priority,
                "blocking": blocking,
                "related_field": related_field,
                "current_status": current_status,
            }
        )

    model = product_requirements.get("part_model_number") or {}
    brand = product_requirements.get("brand") or {}
    desc = product_requirements.get("product_description") or {}
    if not _is_known(model) and not _is_known(brand) and not _is_known(desc):
        add(
            "EXACT_PRODUCT_IDENTIFICATION_REQUIRED",
            "Product identity (model/brand/description) not established as VERIFIED",
            related_field="part_model_number",
            current_status=_status_of(model),
        )
    elif not _is_known(model) and not _is_known(brand):
        add(
            "EXACT_PRODUCT_IDENTIFICATION_REQUIRED",
            "Exact model/brand not VERIFIED — description alone is insufficient for sourcing",
            related_field="part_model_number",
            current_status=_status_of(model),
            priority="HIGH",
        )

    qty = product_requirements.get("quantity") or {}
    if not _is_known(qty):
        add(
            "QUANTITY_REQUIRED",
            "Quantity not established as VERIFIED",
            related_field="quantity",
            current_status=_status_of(qty),
        )

    costs = (economic_requirements or {}).get("costs") or {}
    supplier = costs.get("supplier") or {}
    if str(supplier.get("status") or "") == COST_REQUIRED_UNKNOWN:
        add(
            "CURRENT_SUPPLIER_QUOTE_REQUIRED",
            "Current verified supplier/acquisition cost missing",
            related_field="economic.supplier",
            current_status=str(supplier.get("status") or STATUS_UNKNOWN),
        )
        add(
            "SUPPLIER_AVAILABILITY_REQUIRED",
            "Supplier availability not verified for the required product",
            related_field="economic.supplier",
            current_status=STATUS_UNKNOWN,
            priority="MEDIUM",
        )

    freight = costs.get("freight") or {}
    if str(freight.get("status") or "") == COST_REQUIRED_UNKNOWN:
        add(
            "FREIGHT_QUOTE_REQUIRED",
            "Freight responsibility applies but no verified freight amount",
            related_field="economic.freight",
            current_status=str(freight.get("status") or STATUS_UNKNOWN),
        )

    auth = product_requirements.get("manufacturer_authorization_requirement") or {}
    if auth.get("value") in (True, "required", "REQUIRED") or (
        isinstance(auth.get("value"), str) and "authoriz" in str(auth.get("value")).lower()
    ):
        add(
            "MANUFACTURER_AUTHORIZATION_VERIFICATION_REQUIRED",
            "Manufacturer/dealer authorization appears required or relevant",
            related_field="manufacturer_authorization_requirement",
            current_status=_status_of(auth),
        )

    brand_only = product_requirements.get("brand_name_only") or {}
    if brand_only.get("value") is True:
        add(
            "CHANNEL_ELIGIBILITY_VERIFICATION_REQUIRED",
            "Brand/channel restriction language present — eligibility not verified",
            related_field="brand_name_only",
            current_status=_status_of(brand_only),
        )

    coo = product_requirements.get("country_of_origin") or {}
    if coo.get("value") is not None and _status_of(coo) in {STATUS_VERIFIED, STATUS_ASSESSMENT}:
        add(
            "COUNTRY_OF_ORIGIN_VERIFICATION_REQUIRED",
            "Domestic sourcing / COO language present — compliance not verified for offered product",
            related_field="country_of_origin",
            current_status=_status_of(coo),
            priority="MEDIUM",
        )

    dest = product_requirements.get("delivery_destination") or {}
    ddate = product_requirements.get("required_delivery_date") or {}
    if not _is_known(dest) or not _is_known(ddate):
        add(
            "DELIVERY_FEASIBILITY_VERIFICATION_REQUIRED",
            "Delivery destination and/or required delivery date incomplete",
            related_field="delivery_destination",
            current_status=_status_of(dest) if not _is_known(dest) else _status_of(ddate),
            priority="MEDIUM",
            blocking=not _is_known(dest),
        )

    financing = costs.get("financing") or {}
    if str(financing.get("status") or "") == COST_REQUIRED_UNKNOWN:
        add(
            "FINANCING_TERMS_REQUIRED",
            "Executable no-PG / zero-upfront financing terms not verified",
            related_field="economic.financing",
            current_status=COST_REQUIRED_UNKNOWN,
        )
        add(
            "NO_PG_VERIFICATION_REQUIRED",
            "No personal guarantee path not verified",
            related_field="economic.financing",
            current_status=STATUS_UNKNOWN,
        )
        add(
            "ZERO_UPFRONT_VERIFICATION_REQUIRED",
            "Zero personal cash upfront path not verified",
            related_field="economic.financing",
            current_status=STATUS_UNKNOWN,
        )

    add(
        "HISTORICAL_PRICING_REQUIRED",
        "Historical pricing context not yet attached as verified historical knowledge",
        related_field="historical",
        current_status=STATUS_UNKNOWN,
        priority="LOW",
        blocking=False,
    )
    add(
        "COMPETITION_HISTORY_REQUIRED",
        "Competition/bid history not established",
        related_field="competition",
        current_status=STATUS_UNKNOWN,
        priority="LOW",
        blocking=False,
    )

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for n in needs:
        if n["code"] in seen:
            continue
        seen.add(n["code"])
        unique.append(n)
    return unique


def _opp_field(opportunity: Any, *names: str, default: Any = None) -> Any:
    if opportunity is None:
        return default
    if isinstance(opportunity, dict):
        for n in names:
            if n in opportunity and opportunity[n] is not None:
                return opportunity[n]
        return default
    for n in names:
        if hasattr(opportunity, n):
            v = getattr(opportunity, n)
            if v is not None:
                return v
    return default


def collect_known_knowledge_from_postgres(
    opportunity: Any,
    *,
    session: Any = None,
    allow_sibling_products_table: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """
    READ-ONLY scan of GovCon namespace tables for current vs historical knowledge.

    NEVER reads sibling `products` / niche tables unless explicitly allowed (default False).
    """
    if allow_sibling_products_table:
        raise ValueError("Sibling products table is not authorized as GovCon knowledge")

    known_current: list[dict[str, Any]] = []
    known_historical: list[dict[str, Any]] = []

    notice = _opp_field(opportunity, "notice_id", default=None)
    if notice:
        known_current.append(
            {
                "knowledge_type": "OPPORTUNITY_IDENTITY",
                "code": "OPPORTUNITY_IDENTITY",
                "status": STATUS_VERIFIED,
                "value": notice,
                "source": "gt_contracts.notice_id",
                "temporal": "current",
            }
        )

    # pricing_intel awards = historical context only (no DB required)
    pi = _opp_field(opportunity, "pricing_intel", default=None)
    if isinstance(pi, dict):
        awards = pi.get("awards") or []
        if isinstance(awards, list):
            for aw in awards[:20]:
                if not isinstance(aw, dict):
                    continue
                known_historical.append(
                    {
                        "knowledge_type": "HISTORICAL_AWARD",
                        "code": "HISTORICAL_AWARD",
                        "status": STATUS_VERIFIED
                        if aw.get("award_id") or aw.get("generated_unique_award_id")
                        else STATUS_ASSESSMENT,
                        "value": aw.get("award_amount") or aw.get("amount"),
                        "source": pi.get("source") or "pricing_intel",
                        "fetched_at": pi.get("fetched_at") or pi.get("cached_at"),
                        "temporal": "historical",
                        "notes": "Historical government award ≠ current revenue or supplier cost",
                    }
                )

    contract_id = _opp_field(opportunity, "id", default=None)
    owns_session = False
    db = session
    if db is None and contract_id is not None:
        try:
            from database import SessionLocal

            db = SessionLocal()
            owns_session = True
        except Exception:
            db = None

    try:
        if db is not None and contract_id is not None:
            from models import SubContact

            assert "products" in _FORBIDDEN_SIBLING_TABLES

            rows = db.query(SubContact).filter_by(contract_id=int(contract_id)).all()
            today = today_local()
            for row in rows:
                if row.quote_amount is None:
                    continue
                qdate = row.quote_date
                if isinstance(qdate, datetime):
                    qdate = qdate.date()
                item = {
                    "knowledge_type": "SUPPLIER_QUOTE",
                    "code": "SUPPLIER_QUOTE",
                    "status": STATUS_VERIFIED,
                    "value": float(row.quote_amount) if row.quote_amount is not None else None,
                    "supplier_name": row.company_name,
                    "quote_date": qdate.isoformat() if qdate else None,
                    "source": "gt_sub_contacts",
                    "source_id": row.id,
                }
                if qdate is not None and qdate == today:
                    item["code"] = "CURRENT_SUPPLIER_QUOTE"
                    item["temporal"] = "current"
                    known_current.append(item)
                else:
                    item["code"] = "HISTORICAL_SUPPLIER_QUOTE"
                    item["temporal"] = "historical"
                    item["notes"] = (
                        "Historical quote — cannot satisfy CURRENT_SUPPLIER_QUOTE_REQUIRED"
                    )
                    known_historical.append(item)
    finally:
        if owns_session and db is not None:
            db.close()

    return {"known_current": known_current, "known_historical": known_historical}


def plan_product_research(
    opportunity: Any,
    *,
    stage0: dict[str, Any] | None = None,
    stage1: dict[str, Any] | None = None,
    stage2: dict[str, Any] | None = None,
    session: Any = None,
) -> dict[str, Any]:
    """
    ZERO-COST Postgres-first PRODUCT RESEARCH PLANNER.

    No OpenAI, no web, no SAM, no USAspending.
    Does not read sibling `products` table.
    """
    s0 = stage0 or {}
    s1 = stage1 or {}
    s2 = stage2 or {}

    facts = s2.get("facts") if isinstance(s2.get("facts"), dict) else {}

    fit = resolve_core_fit(
        stage1_category=s1.get("category"),
        stage0_classification=s0.get("classification"),
        stage2_category_value=((facts.get("procurement") or {}).get("category") or {}).get("value"),
    )

    product_requirements = s2.get("product_requirements")
    if not isinstance(product_requirements, dict):
        product_requirements = build_product_requirements_view(facts, opportunity=opportunity)

    economic = s2.get("economic_requirements")
    if not isinstance(economic, dict) or not economic.get("costs"):
        install_fact = product_requirements.get("installation_requirement") or {}
        install_req = None
        if install_fact.get("value") is True:
            install_req = True
        elif install_fact.get("value") is False and str(install_fact.get("status")) == STATUS_VERIFIED:
            install_req = False
        has_product = _is_known(product_requirements.get("part_model_number")) or _is_known(
            product_requirements.get("brand")
        )
        costs = map_cost_requirements_for_canonical_class(
            canonical_class=fit["canonical_class"],
            installation_required=install_req,
            has_product_procurement_evidence=has_product,
            requires_financing=True,
        )
        economic = {
            "canonical_execution_class": fit["canonical_class"],
            "costs": costs,
            "actual_profit": None,
            "actual_profit_status": "INCOMPLETE",
        }

    knowledge = collect_known_knowledge_from_postgres(
        opportunity, session=session, allow_sibling_products_table=False
    )
    known_current = knowledge["known_current"]
    known_historical = knowledge["known_historical"]

    research_needs = build_product_research_needs(
        product_requirements=product_requirements,
        economic_requirements=economic,
        known_current=known_current,
        core_fit=fit["core_fit"],
    )

    if any(k.get("knowledge_type") == "HISTORICAL_AWARD" for k in known_historical):
        research_needs = [n for n in research_needs if n["code"] != "HISTORICAL_PRICING_REQUIRED"]

    missing = [
        {
            "code": n["code"],
            "related_field": n.get("related_field"),
            "current_status": n.get("current_status"),
            "blocking": n.get("blocking"),
        }
        for n in research_needs
    ]

    research_tasks = [
        {
            "code": n["code"],
            "reason": n["reason"],
            "blocking": n["blocking"],
            "priority": n["priority"],
            "allowed_sources": [],
            "status": "NOT_STARTED",
            "related_field": n.get("related_field"),
            "current_status": n.get("current_status"),
        }
        for n in research_needs
    ]

    costs = economic.get("costs") or {}

    def _econ_status(cat: str) -> str:
        item = costs.get(cat) or {}
        return str(item.get("status") or STATUS_UNKNOWN)

    actual_profit = economic.get("actual_profit")
    required_unknown = any(
        str((costs.get(c) or {}).get("status")) == COST_REQUIRED_UNKNOWN for c in costs
    )
    if required_unknown:
        actual_profit = None

    ready = fit["core_fit"] == FIT_CORE_PRODUCT and len(research_tasks) > 0

    return {
        "opportunity_id": _opp_field(opportunity, "id", default=None),
        "notice_id": _opp_field(opportunity, "notice_id", default=None),
        "core_fit": fit["core_fit"],
        "product_purity": fit.get("product_purity"),
        "canonical_class": fit["canonical_class"],
        "core_fit_meta": fit,
        "paid_work_priority_rank": paid_work_priority_rank(fit),
        "product_requirements": product_requirements,
        "known_current": known_current,
        "known_historical": known_historical,
        "missing": missing,
        "research_tasks": research_tasks,
        "economic_readiness": {
            "supplier_cost": _econ_status("supplier"),
            "freight": _econ_status("freight"),
            "installation": _econ_status("installation"),
            "subcontract": _econ_status("subcontract"),
            "financing": _econ_status("financing"),
            "actual_profit": actual_profit,
            "status": "INCOMPLETE"
            if actual_profit is None
            else str(economic.get("actual_profit_status") or "UNKNOWN"),
        },
        "ready_for_external_research": ready,
        "sibling_products_table_used": False,
        "external_api_calls": 0,
        "planned_at": now_utc().isoformat(),
    }
