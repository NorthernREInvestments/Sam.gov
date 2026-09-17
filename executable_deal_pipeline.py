"""ExecutableDealPipeline — orchestrate existing M3 engines; do not duplicate them.

Engines reused: deep_deal_qualification, deadline_runtime, transactional_bom,
on_demand/document_locator, economic_integrity, commercial_economics,
funding_underwriting / pre_bid_transaction_financing, deal_readiness,
reusable_knowledge, procurement_source_knowledge.
"""

from __future__ import annotations

import time
from copy import deepcopy
from typing import Any

from application_clock import clock_mode, now_utc
from deadline_runtime import STATUS_EXPIRED, evaluate_deadline
from deep_deal_constants import DEAL_ONE_TIME_PRODUCT, DEAL_SERVICE
from deep_deal_qualification import classify_deal_type, evaluate_pre_deep_fit
from economic_integrity import (
    COST_CALCULATED,
    COST_NOT_APPLICABLE,
    COST_REQUIRED_UNKNOWN,
    COST_UNKNOWN,
    COST_VERIFIED,
    calculate_actual_profit,
    cost_item,
    min_actual_profit_usd,
    revenue_item,
)
from evidence_maturity import assert_not_promoting_estimate, freight_fact, mature_fact
from executable_deal_constants import (
    ACTION_DOWNLOAD_AUTH,
    ACTION_OBTAIN_FREIGHT,
    ACTION_REVIEW_PG,
    EV_ESTIMATE,
    EV_OPERATOR_CONFIRMED,
    EV_PUBLIC_CURRENT,
    EV_QUOTE_REQUIRED,
    EV_UNKNOWN,
    EV_VERIFIED_SOLICITATION,
    FORMAL_QUOTE_REQUIRED,
    FUND_INCOMPATIBLE,
    FUND_LIKELY,
    FUND_POSSIBLE,
    FUND_STALE,
    FUND_UNKNOWN,
    LIVE_IOWA,
    MAX_SUPPLIER_SHORTLIST,
    OPERATOR_FICO,
    PROFIT_FLOOR_USD,
    READY_BID_PREP,
    READY_COMPLIANCE,
    READY_ECONOMICS_REVIEW,
    READY_FUNDING_CALL,
    READY_FUNDING_VERIFY,
    READY_OPERATOR_ACTION,
    READY_QUOTE_REQUIRED,
    READY_REJECTED,
    READY_RESEARCHING,
    STAGE_BOM_DEFINITION,
    STAGE_COMPLIANCE_REVIEW,
    STAGE_DEADLINE_VALIDATION,
    STAGE_DEAL_READY_FOR_BID_PREPARATION,
    STAGE_DEAL_REJECTED,
    STAGE_ECONOMIC_QUALIFICATION,
    STAGE_FREIGHT_AND_TRANSACTION_COSTING,
    STAGE_FUNDING_COMPATIBILITY,
    STAGE_OPERATOR_ACTION,
    STAGE_REQUIREMENT_ACQUISITION,
    STAGE_REQUIREMENT_EXTRACTION,
    STAGE_SUPPLIER_COSTING,
    STAGE_SUPPLIER_IDENTIFICATION,
    STAGE_TRANSACTIONAL_QUALIFICATION,
    STAGE_WORKING_CAPITAL_DEFINITION,
)
from operator_action_queue import (
    OperatorActionQueue,
    enqueue_financier_action,
    enqueue_supplier_quote_action,
    operator_action,
)
from procurement_source_knowledge import ProcurementSourceKnowledgeBase, assess_staleness
from reusable_knowledge import ReusableKnowledgeStore
from transactional_bom import empty_line_item


def _utc() -> str:
    return now_utc().isoformat()


def normalize_bom_line(raw: dict[str, Any]) -> dict[str, Any]:
    line = empty_line_item()
    mapping = {
        "line_number": "line_number",
        "CLIN_or_item_number": "CLIN_or_item_number",
        "clin": "CLIN_or_item_number",
        "description": "description",
        "quantity": "quantity",
        "unit": "unit",
        "unit_of_measure": "unit",
        "manufacturer": "manufacturer",
        "part_number": "part_number",
        "model": "model",
        "delivery_location": "delivery_location",
        "required_date": "required_date",
    }
    for src, dst in mapping.items():
        if raw.get(src) is not None:
            line[dst] = raw[src]
    line["supplier_candidate"] = raw.get("supplier_candidate")
    line["unit_cost"] = raw.get("unit_cost")
    line["extended_cost"] = raw.get("extended_cost")
    line["cost_evidence_state"] = raw.get("cost_evidence_state") or EV_UNKNOWN
    line["freight_treatment"] = raw.get("freight_treatment") or EV_UNKNOWN
    line["compliance_notes"] = raw.get("compliance_notes")
    line["unresolved_blockers"] = list(raw.get("unresolved_blockers") or [])
    line["acceptable_equivalent_rules"] = raw.get("acceptable_equivalent_rules")
    line["salient_characteristics"] = raw.get("salient_characteristics")
    line["government_item_code"] = raw.get("government_item_code")
    return line


def query_supplier_knowledge(
    reusable: ReusableKnowledgeStore,
    *,
    manufacturer: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Reuse permanent supplier intelligence before web search."""
    hits = []
    mfr = (manufacturer or "").strip().lower()
    cat = (category or "").strip().lower()
    if not mfr and not cat:
        return []
    for fact in reusable.suppliers:
        row = dict(fact)
        if row.get("staleness") == "STALE":
            row["needs_reverification"] = True
        m_ok = bool(mfr) and bool(row.get("manufacturer")) and mfr in str(row.get("manufacturer")).lower()
        # Also match supplier name against manufacturer token (e.g. Winter Equipment)
        s_ok = bool(mfr) and mfr in str(row.get("supplier") or "").lower()
        c_ok = bool(cat) and bool(row.get("category")) and cat in str(row.get("category")).lower()
        if m_ok or s_ok or c_ok:
            hits.append(row)
    return hits[:MAX_SUPPLIER_SHORTLIST]


def rank_supplier_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def score(c: dict[str, Any]) -> int:
        s = 0
        if c.get("authorization_evidence"):
            s += 3
        if c.get("quote_required") is False:
            s += 2
        if c.get("government_sales_capability"):
            s += 2
        if c.get("needs_reverification"):
            s -= 2
        if c.get("staleness") == "CURRENT":
            s += 1
        return s

    return sorted(candidates, key=score, reverse=True)[:MAX_SUPPLIER_SHORTLIST]


def calculate_working_capital(
    *,
    supplier_total: float | None,
    deposit: float | None = None,
    freight: float | None = None,
    other_pre_delivery: float | None = None,
    supplier_balance_timing: str = "BEFORE_SHIPMENT",
    government_payment_timing: str = "NET_60",
) -> dict[str, Any]:
    """Max capital outstanding ≠ always full supplier cost when timing differs."""
    if supplier_total is None:
        return {
            "working_capital_required": None,
            "maturity": EV_UNKNOWN,
            "note": "supplier_total_unknown",
        }
    dep = float(deposit or 0)
    bal = float(supplier_total) - dep
    fr = float(freight or 0) if freight is not None else None
    other = float(other_pre_delivery or 0)
    # Conservative: if balance due before ship and gov pays later, exposure ≈ deposit+balance+freight
    components = {
        "supplier_deposit": dep,
        "supplier_balance": bal,
        "freight": fr,
        "other_pre_delivery": other,
        "supplier_balance_timing": supplier_balance_timing,
        "government_payment_timing": government_payment_timing,
    }
    known = [dep, bal]
    if fr is not None:
        known.append(fr)
    known.append(other)
    maximum = sum(known)
    return {
        "working_capital_required": maximum,
        "components": components,
        "maturity": EV_ESTIMATE if fr is None else EV_OPERATOR_CONFIRMED,
        "note": "maximum_capital_outstanding_estimate",
    }


def match_finance_knowledge(
    reusable: ReusableKnowledgeStore,
    *,
    amount: float | None,
) -> list[dict[str, Any]]:
    """Query permanent financing intelligence before new research."""
    by_provider: dict[str, dict[str, Any]] = {}
    for f in reusable.finance:
        pid = str(f.get("provider_id"))
        by_provider.setdefault(pid, {"provider_id": pid, "facts": [], "status": FUND_UNKNOWN})
        by_provider[pid]["facts"].append(f)
        if f.get("staleness") == "STALE":
            by_provider[pid]["status"] = FUND_STALE
    out = []
    for pid, row in by_provider.items():
        facts = {f["field"]: f for f in row["facts"]}
        min_fico = facts.get("minimum_fico", {}).get("value")
        status = row["status"]
        if status != FUND_STALE:
            if min_fico is not None and facts.get("minimum_fico", {}).get("confidence") == "HIGH":
                try:
                    if float(OPERATOR_FICO) < float(min_fico):
                        # only REJECT if verified floor — handled by underwriting elsewhere
                        status = FUND_INCOMPATIBLE
                    else:
                        status = FUND_LIKELY
                except (TypeError, ValueError):
                    status = FUND_POSSIBLE
            elif facts.get("pg_requirement"):
                status = FUND_POSSIBLE  # PG ≠ auto-reject
            else:
                status = FUND_POSSIBLE
        out.append(
            {
                "provider_id": pid,
                "status": status,
                "facts": row["facts"],
                "amount_context": amount,
            }
        )
    return out


def map_operator_readiness(
    *,
    stage: str,
    quote_required: bool = False,
    funding_call_ready: bool = False,
    funding_verify: bool = False,
    compliance_block: bool = False,
    rejected: bool = False,
    bid_prep: bool = False,
    economics_review: bool = False,
    operator_action: bool = False,
) -> str:
    if rejected:
        return READY_REJECTED
    if bid_prep and not compliance_block:
        return READY_BID_PREP
    if compliance_block:
        return READY_COMPLIANCE
    if funding_verify:
        return READY_FUNDING_VERIFY
    if funding_call_ready:
        return READY_FUNDING_CALL
    if quote_required:
        return READY_QUOTE_REQUIRED
    if economics_review:
        return READY_ECONOMICS_REVIEW
    if operator_action:
        return READY_OPERATOR_ACTION
    if stage == STAGE_DEAL_REJECTED:
        return READY_REJECTED
    return READY_RESEARCHING


class ExecutableDealPipeline:
    """Bounded progressive orchestrator over existing engines."""

    def __init__(
        self,
        *,
        reusable: ReusableKnowledgeStore | None = None,
        source_knowledge: ProcurementSourceKnowledgeBase | None = None,
        action_queue: OperatorActionQueue | None = None,
        max_external_requests: int = 10,
    ) -> None:
        self.reusable = reusable or ReusableKnowledgeStore()
        self.source_knowledge = source_knowledge or ProcurementSourceKnowledgeBase()
        self.actions = action_queue or OperatorActionQueue()
        self.max_external_requests = max_external_requests
        self.metrics = {
            "supplier_facts_reused": 0,
            "finance_facts_reused": 0,
            "buyer_facts_reused": 0,
            "source_recipes_reused": 0,
            "external_requests": 0,
            "external_requests_avoided": 0,
            "stages_skipped_existing_info": 0,
            "new_reusable_facts_learned": 0,
            "stale_facts_reverified": 0,
            "engines_reused": [
                "deep_deal_qualification",
                "deadline_runtime",
                "transactional_bom",
                "economic_integrity",
                "commercial_economics",
                "funding_underwriting/pre_bid_financing",
                "deal_readiness",
                "reusable_knowledge",
                "procurement_source_knowledge",
                "document_locator/on_demand",
            ],
            "major_new_engines": [
                "executable_deal_pipeline_orchestrator",
                "operator_action_queue",
                "operator_result_ingestion",
                "operator_deal_packet",
            ],
        }

    def run(self, opportunity: dict[str, Any]) -> dict[str, Any]:
        t0 = time.perf_counter()
        deal = deepcopy(opportunity)
        deal.setdefault("deal_id", deal.get("solicitation_number") or "UNKNOWN")
        stages_run: list[str] = []
        stop_reason = None
        quote_required = False
        funding_call_ready = False
        funding_verify = False
        compliance_block = False
        rejected = False
        bid_prep = False
        economics_review = False
        operator_action_needed = False

        # --- STAGE: transactional qualification ---
        stages_run.append(STAGE_TRANSACTIONAL_QUALIFICATION)
        row = {
            "title": deal.get("title") or deal.get("product"),
            "description": deal.get("description") or deal.get("title"),
            "product_classification": deal.get("product_classification") or "CORE_PRODUCT",
            "solicitation_number": deal.get("solicitation_number"),
        }
        deal_type = classify_deal_type(row)
        fit = evaluate_pre_deep_fit(row, deal_type)
        deal["deal_type"] = deal_type.get("deal_type")
        deal["fit"] = fit.get("fit")
        if deal_type.get("deal_type") == DEAL_SERVICE or fit.get("fit") == "NOT_OUR_MODEL":
            rejected = True
            stop_reason = "not_transactional_product_resale"
            return self._finish(
                deal,
                stages_run,
                STAGE_DEAL_REJECTED,
                stop_reason,
                t0,
                rejected=True,
            )

        # --- STAGE: deadline ---
        stages_run.append(STAGE_DEADLINE_VALIDATION)
        deadline = deal.get("bid_deadline") or deal.get("deadline") or deal.get("response_deadline")
        if deadline:
            dl = evaluate_deadline(response_deadline=deadline, local_timezone=deal.get("timezone") or "America/Chicago")
            deal["deadline_evaluation"] = {
                "status": dl.get("deadline_status") or dl.get("status"),
                "raw": deadline,
                "calendar_days_remaining": dl.get("calendar_days_remaining"),
            }
            if (dl.get("deadline_status") or dl.get("status")) == STATUS_EXPIRED:
                rejected = True
                stop_reason = "deadline_expired"
                return self._finish(deal, stages_run, STAGE_DEAL_REJECTED, stop_reason, t0, rejected=True)
        else:
            deal["deadline_evaluation"] = {"status": "DEADLINE_UNKNOWN"}

        # --- Requirements / BOM ---
        lines_raw = deal.get("line_items") or deal.get("bom") or []
        if not lines_raw and deal.get("requirements_insufficient"):
            stages_run.append(STAGE_REQUIREMENT_ACQUISITION)
            # Reuse source recipe — no new portal mirror
            recipes = self.source_knowledge.match_by_jurisdiction(
                deal.get("state") or deal.get("jurisdiction"), deal.get("agency")
            )
            if recipes:
                self.metrics["source_recipes_reused"] += 1
            if deal.get("auth_required_for_spec"):
                self.actions.add(
                    operator_action(
                        ACTION_DOWNLOAD_AUTH,
                        deal_id=str(deal["deal_id"]),
                        why="Authoritative specification behind authentication boundary",
                        who_where=str(deal.get("portal") or "procurement portal"),
                        questions=["Download authoritative solicitation attachments"],
                        information_expected=["specification_pdf"],
                        unlocks_stage=STAGE_REQUIREMENT_EXTRACTION,
                        priority=15,
                    )
                )
                operator_action_needed = True
                stop_reason = "AUTH_REQUIRED"
                return self._finish(
                    deal,
                    stages_run,
                    STAGE_OPERATOR_ACTION,
                    stop_reason,
                    t0,
                    operator_action=True,
                )
            stop_reason = "requirements_insufficient"
            return self._finish(
                deal,
                stages_run,
                STAGE_REQUIREMENT_ACQUISITION,
                stop_reason,
                t0,
                operator_action=True,
            )

        stages_run.append(STAGE_REQUIREMENT_EXTRACTION)
        stages_run.append(STAGE_BOM_DEFINITION)
        bom = [normalize_bom_line(li if isinstance(li, dict) else {"description": str(li)}) for li in lines_raw]
        deal["bom"] = bom
        deal["bom_line_count"] = len(bom)
        if not bom:
            deal["requirements_insufficient"] = True
            stop_reason = "bom_empty"
            return self._finish(deal, stages_run, STAGE_BOM_DEFINITION, stop_reason, t0, operator_action=True)

        # Mark quantities from solicitation when provided as verified
        for li in bom:
            if li.get("quantity") is not None and deal.get("quantities_from_solicitation"):
                li["quantity_maturity"] = EV_VERIFIED_SOLICITATION

        # Auth-gated authoritative attachment — surface action without fabricating specs
        if deal.get("auth_required_for_spec") or deal.get("auth_barriers"):
            self.actions.add(
                operator_action(
                    ACTION_DOWNLOAD_AUTH,
                    deal_id=str(deal["deal_id"]),
                    why="Authoritative specification attachment behind authentication — do not fabricate",
                    who_where=str(deal.get("portal") or "procurement portal"),
                    questions=["Download authoritative specification attachment with operator credentials"],
                    information_expected=["authoritative_specification"],
                    unlocks_stage=STAGE_REQUIREMENT_EXTRACTION,
                    priority=15,
                )
            )
            operator_action_needed = True
            self.metrics["stages_skipped_existing_info"] += 1  # BOM already from public event doc

        # --- Supplier identification (knowledge first) ---
        stages_run.append(STAGE_SUPPLIER_IDENTIFICATION)
        manufacturers = list({li.get("manufacturer") for li in bom if li.get("manufacturer")})
        category = deal.get("product_category") or deal.get("title")
        reused: list[dict[str, Any]] = []
        if manufacturers:
            for mfr in manufacturers:
                reused.extend(query_supplier_knowledge(self.reusable, manufacturer=mfr, category=category))
        if not reused:
            reused = query_supplier_knowledge(self.reusable, category=category)
        # Prefer known specialty name hints (e.g. Winter Equipment) without web crawl
        for hint in deal.get("preferred_suppliers") or []:
            reused.extend(query_supplier_knowledge(self.reusable, manufacturer=str(hint), category=category))
        # Dedupe by supplier name
        seen = set()
        deduped = []
        for s in reused:
            key = str(s.get("supplier") or s.get("name") or id(s))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(s)
        reused = deduped
        if reused:
            self.metrics["supplier_facts_reused"] += len(reused)
            self.metrics["external_requests_avoided"] += 1
        else:
            # Seed from opportunity-provided candidates without web crawl in orchestrator
            for s in deal.get("supplier_candidates") or []:
                reused.append(s if isinstance(s, dict) else {"supplier": str(s), "source": "opportunity"})
            if reused:
                self.metrics["stages_skipped_existing_info"] += 1
        ranked = rank_supplier_candidates(reused)
        # Merge explicit opportunity suppliers
        for s in deal.get("supplier_candidates") or []:
            if not isinstance(s, dict):
                continue
            key = str(s.get("name") or s.get("supplier") or "").lower()
            if key and any(key == str(x.get("name") or x.get("supplier") or "").lower() for x in ranked):
                continue
            ranked.append(s)
        ranked = ranked[:MAX_SUPPLIER_SHORTLIST]
        deal["supplier_shortlist"] = ranked
        if not ranked:
            # Still can request quote if names known from packet
            pass

        # --- Supplier costing ---
        stages_run.append(STAGE_SUPPLIER_COSTING)
        quoted = deal.get("supplier_quote") or {}
        quoted_total = None
        if isinstance(quoted.get("quoted_total"), dict):
            quoted_total = quoted["quoted_total"].get("value")
            maturity = quoted["quoted_total"].get("maturity") or EV_OPERATOR_CONFIRMED
        elif quoted.get("quoted_total") is not None:
            quoted_total = quoted.get("quoted_total")
            maturity = EV_OPERATOR_CONFIRMED
        elif deal.get("public_price_total") is not None:
            quoted_total = deal.get("public_price_total")
            maturity = EV_PUBLIC_CURRENT
            deal["price_sufficiency"] = "PUBLIC_PRICE_SUFFICIENT_FOR_PRELIMINARY_ECONOMICS"
        else:
            maturity = EV_QUOTE_REQUIRED
            quote_required = True
            deal["price_sufficiency"] = FORMAL_QUOTE_REQUIRED
            best = ranked[0] if ranked else {"name": deal.get("preferred_supplier") or "primary supplier candidate"}
            if not best.get("name") and best.get("supplier"):
                best = {**best, "name": best["supplier"]}
            enqueue_supplier_quote_action(
                self.actions,
                deal=deal,
                supplier=best,
                line_items=bom,
            )
            operator_action_needed = True

        deal["supplier_cost"] = assert_not_promoting_estimate(
            mature_fact(quoted_total, maturity if quoted_total is not None else EV_QUOTE_REQUIRED)
        )

        # --- Freight ---
        stages_run.append(STAGE_FREIGHT_AND_TRANSACTION_COSTING)
        if deal.get("freight") and isinstance(deal["freight"], dict):
            freight = assert_not_promoting_estimate(deal["freight"])
        elif quoted.get("freight_included") is True:
            freight = freight_fact(0, included_in_supplier=True, maturity=EV_OPERATOR_CONFIRMED)
        elif deal.get("freight_amount") is not None:
            freight = freight_fact(deal.get("freight_amount"), maturity=deal.get("freight_maturity") or EV_ESTIMATE)
        else:
            freight = freight_fact(None)
            if not quote_required:
                self.actions.add(
                    operator_action(
                        ACTION_OBTAIN_FREIGHT,
                        deal_id=str(deal["deal_id"]),
                        why="Freight unknown and cannot silently default to zero",
                        who_where=str((ranked[0] if ranked else {}).get("name") or "supplier/carrier"),
                        questions=["Confirm freight amount or delivered pricing"],
                        information_expected=["freight_amount_or_included"],
                        unlocks_stage=STAGE_FREIGHT_AND_TRANSACTION_COSTING,
                        priority=25,
                    )
                )
                operator_action_needed = True
        deal["freight"] = freight

        # Early exit for quote required before funding
        if quote_required:
            stop_reason = "QUOTE_REQUIRED"
            return self._finish(
                deal,
                stages_run,
                STAGE_OPERATOR_ACTION,
                stop_reason,
                t0,
                quote_required=True,
                operator_action=True,
            )

        # --- Economics ---
        stages_run.append(STAGE_ECONOMIC_QUALIFICATION)
        supplier_cost_val = deal["supplier_cost"].get("value")
        freight_val = deal["freight"].get("value") if deal["freight"].get("maturity") != EV_QUOTE_REQUIRED else None
        # If freight quote required, economics incomplete — not false reject
        if deal["freight"].get("quote_required") and freight_val is None:
            economics_review = True
            deal["economics"] = {
                "status": "INSUFFICIENT_EVIDENCE",
                "reason": "freight_unknown",
                "supplier_cost": supplier_cost_val,
                "expected_actual_profit": None,
                "meets_profit_floor": None,
            }
            stop_reason = "freight_quote_required"
            return self._finish(
                deal,
                stages_run,
                STAGE_FREIGHT_AND_TRANSACTION_COSTING,
                stop_reason,
                t0,
                economics_review=True,
                operator_action=True,
            )

        financing_cost = deal.get("financing_cost_amount")
        # Before a funding conversation, financing is unknown — do not treat as verified zero,
        # and do not hard-block progression to funding call readiness.
        if financing_cost is not None:
            fin_status = COST_VERIFIED
        elif deal.get("funding_deal_specific_confirmed"):
            fin_status = COST_REQUIRED_UNKNOWN
        else:
            fin_status = COST_UNKNOWN
        if supplier_cost_val is None:
            deal["economics"] = {"status": "QUOTE_REQUIRED"}
            return self._finish(deal, stages_run, STAGE_SUPPLIER_COSTING, "QUOTE_REQUIRED", t0, quote_required=True)

        other = float(deal.get("other_transaction_costs") or 0)
        cost_sum = float(supplier_cost_val) + float(freight_val or 0) + float(financing_cost or 0) + other
        min_bid_10k = cost_sum + float(min_actual_profit_usd() or PROFIT_FLOOR_USD)
        break_even = cost_sum
        proposed = deal.get("proposed_bid")
        if proposed is None:
            # Do not invent competitive price — only executable boundary
            proposed = None
            deal["executable_price"] = {
                "break_even": break_even,
                "minimum_bid_for_10k_profit": min_bid_10k,
                "proposed_bid": None,
                "note": "EXECUTABLE_PRICE boundaries only — not winning/competitive price",
            }
            profit = calculate_actual_profit(
                revenue=revenue_item(value=min_bid_10k, status="CALCULATED", source_field="min_bid_for_floor"),
                costs={
                    "supplier_acquisition": cost_item(
                        category="supplier_acquisition",
                        value=supplier_cost_val,
                        status=COST_VERIFIED if deal["supplier_cost"]["is_verified"] else COST_CALCULATED,
                        required=True,
                    ),
                    "freight": cost_item(
                        category="freight",
                        value=freight_val if freight_val is not None else None,
                        status=(
                            COST_VERIFIED
                            if deal["freight"].get("is_verified")
                            else (COST_CALCULATED if freight_val is not None else COST_REQUIRED_UNKNOWN)
                        ),
                        required=True,
                    ),
                    "financing": cost_item(
                        category="financing",
                        value=financing_cost,
                        status=fin_status,
                        required=True,
                    ),
                    "other_required": cost_item(
                        category="other_required",
                        value=other if other else None,
                        status=COST_VERIFIED if other else COST_NOT_APPLICABLE,
                    ),
                },
            )
            deal["economics"] = {
                "status": "PRELIMINARY_ECONOMICS" if not deal["supplier_cost"]["is_verified"] else "KNOWN_ECONOMICS",
                "supplier_cost": supplier_cost_val,
                "freight": freight_val,
                "financing_cost": financing_cost,
                "other_costs": other,
                "break_even": break_even,
                "minimum_bid_for_10k_profit": min_bid_10k,
                "proposed_bid": None,
                "expected_actual_profit": profit.get("actual_profit"),
                "meets_profit_floor": profit.get("meets_min_actual_profit"),
                "profit_detail": profit,
                "false_estimate_as_verified": False,
            }
        else:
            profit = calculate_actual_profit(
                revenue=revenue_item(value=float(proposed), status="CALCULATED", source_field="operator_proposed_bid"),
                costs={
                    "supplier_acquisition": cost_item(
                        category="supplier_acquisition",
                        value=supplier_cost_val,
                        status=COST_VERIFIED if deal["supplier_cost"]["is_verified"] else COST_CALCULATED,
                        required=True,
                    ),
                    "freight": cost_item(
                        category="freight",
                        value=freight_val if freight_val is not None else None,
                        status=(
                            COST_VERIFIED
                            if deal["freight"].get("is_verified")
                            else (COST_CALCULATED if freight_val is not None else COST_REQUIRED_UNKNOWN)
                        ),
                        required=True,
                    ),
                    "financing": cost_item(
                        category="financing",
                        value=financing_cost,
                        status=fin_status,
                        required=True,
                    ),
                    "other_required": cost_item(
                        category="other_required",
                        value=other if other else None,
                        status=COST_VERIFIED if other else COST_NOT_APPLICABLE,
                    ),
                },
            )
            deal["executable_price"] = {
                "break_even": break_even,
                "minimum_bid_for_10k_profit": min_bid_10k,
                "proposed_bid": proposed,
            }
            deal["economics"] = {
                "status": "KNOWN_ECONOMICS" if deal["supplier_cost"]["is_verified"] else "PRELIMINARY_ECONOMICS",
                "supplier_cost": supplier_cost_val,
                "freight": freight_val,
                "financing_cost": financing_cost,
                "other_costs": other,
                "break_even": break_even,
                "minimum_bid_for_10k_profit": min_bid_10k,
                "proposed_bid": proposed,
                "expected_actual_profit": profit.get("actual_profit"),
                "meets_profit_floor": profit.get("meets_min_actual_profit"),
                "profit_detail": profit,
                "false_estimate_as_verified": False,
            }

        # Early economic kill only when actual profit is known and fails floor.
        # Unknown financing/freight must NOT cause false rejection (→ quote/funding path).
        if (
            proposed is not None
            and deal["supplier_cost"]["is_verified"]
            and deal["economics"].get("expected_actual_profit") is not None
            and deal["economics"].get("meets_profit_floor") is False
        ):
            rejected = True
            stop_reason = "economics_below_profit_floor"
            return self._finish(deal, stages_run, STAGE_DEAL_REJECTED, stop_reason, t0, rejected=True)

        # --- Working capital ---
        stages_run.append(STAGE_WORKING_CAPITAL_DEFINITION)
        deposit = None
        if isinstance(deal.get("supplier_quote"), dict):
            deposit = deal["supplier_quote"].get("deposit")
        wc = calculate_working_capital(
            supplier_total=float(supplier_cost_val),
            deposit=deposit,
            freight=float(freight_val) if freight_val is not None else None,
        )
        deal["working_capital"] = wc

        # --- Funding (only after economics mature) ---
        stages_run.append(STAGE_FUNDING_COMPATIBILITY)
        finance_matches = match_finance_knowledge(self.reusable, amount=wc.get("working_capital_required"))
        if finance_matches:
            self.metrics["finance_facts_reused"] += sum(len(m.get("facts") or []) for m in finance_matches)
            self.metrics["external_requests_avoided"] += 1
        deal["finance_matches"] = finance_matches
        # Public marketing alone ≠ verified path
        if deal.get("funding_deal_specific_confirmed"):
            funding_verify = False
            if any(m.get("status") == FUND_INCOMPATIBLE for m in finance_matches):
                # still allow PG review path
                pass
            fr = deal.get("financier_result") or {}
            if fr.get("pg_requirement") in {True, "yes", "full", "FULL", "required"}:
                self.actions.add(
                    operator_action(
                        ACTION_REVIEW_PG,
                        deal_id=str(deal["deal_id"]),
                        why="PG required — operator approval needed (not auto-reject)",
                        who_where="operator",
                        questions=["Approve or decline personal guarantee for this transaction"],
                        information_expected=["pg_operator_decision"],
                        unlocks_stage=STAGE_FUNDING_COMPATIBILITY,
                        priority=30,
                    )
                )
                operator_action_needed = True
                deal["funding_status"] = "WORKABLE_WITH_OPERATOR_PG_APPROVAL"
            elif fr.get("pre_bid_conditional") or fr.get("accepted_in_principle"):
                deal["funding_status"] = "CONDITIONAL_PATH_IDENTIFIED"
            else:
                deal["funding_status"] = "DEAL_SPECIFIC_VERIFICATION_REQUIRED"
                funding_verify = True
        else:
            # Ready for pre-bid call when economics mature
            funding_call_ready = True
            deal["funding_status"] = "READY_FOR_PRE_BID_CALL"
            provider = {"provider_id": (finance_matches[0]["provider_id"] if finance_matches else "transaction_po_financier")}
            enqueue_financier_action(
                self.actions,
                deal={**deal, "supplier_name": (ranked[0].get("supplier") if ranked else None)},
                provider=provider,
                economics=deal.get("economics"),
                working_capital=wc,
            )
            operator_action_needed = True

        # --- Compliance ---
        stages_run.append(STAGE_COMPLIANCE_REVIEW)
        blockers = list(deal.get("compliance_blockers") or [])
        for li in bom:
            blockers.extend(li.get("unresolved_blockers") or [])
        deal["compliance"] = {
            "resolved": list(deal.get("compliance_resolved") or []),
            "unresolved": blockers,
            "critical": [b for b in blockers if b],
        }
        if blockers:
            compliance_block = True
            stop_reason = "COMPLIANCE_REVIEW_REQUIRED"
            return self._finish(
                deal,
                stages_run,
                STAGE_COMPLIANCE_REVIEW,
                stop_reason,
                t0,
                compliance_block=True,
                funding_call_ready=funding_call_ready,
            )

        if funding_call_ready and not deal.get("funding_deal_specific_confirmed"):
            stop_reason = "FUNDING_CALL_READY"
            return self._finish(
                deal,
                stages_run,
                STAGE_FUNDING_COMPATIBILITY,
                stop_reason,
                t0,
                funding_call_ready=True,
                operator_action=True,
            )

        if deal.get("funding_status") in {
            "CONDITIONAL_PATH_IDENTIFIED",
            "VERIFIED_PRE_BID_PATH",
            "WORKABLE_WITH_OPERATOR_PG_APPROVAL",
        } and deal.get("pg_operator_approved") is not False:
            # PG approval pending still blocks bid prep
            if deal.get("funding_status") == "WORKABLE_WITH_OPERATOR_PG_APPROVAL" and not deal.get("pg_operator_approved"):
                stop_reason = "PG_OPERATOR_REVIEW"
                return self._finish(
                    deal,
                    stages_run,
                    STAGE_OPERATOR_ACTION,
                    stop_reason,
                    t0,
                    funding_verify=True,
                    operator_action=True,
                )
            bid_prep = True
            stages_run.append(STAGE_DEAL_READY_FOR_BID_PREPARATION)
            stop_reason = "READY_FOR_BID_PREPARATION"
            return self._finish(deal, stages_run, STAGE_DEAL_READY_FOR_BID_PREPARATION, stop_reason, t0, bid_prep=True)

        stop_reason = deal.get("funding_status") or "OPERATOR_ACTION_REQUIRED"
        return self._finish(
            deal,
            stages_run,
            STAGE_OPERATOR_ACTION,
            stop_reason,
            t0,
            funding_call_ready=funding_call_ready,
            funding_verify=funding_verify,
            operator_action=operator_action_needed,
        )

    def _finish(
        self,
        deal: dict[str, Any],
        stages_run: list[str],
        stage: str,
        stop_reason: str | None,
        t0: float,
        **flags: Any,
    ) -> dict[str, Any]:
        readiness = map_operator_readiness(stage=stage, **flags)
        deal["pipeline_stage"] = stage
        deal["operator_readiness"] = readiness
        deal["stop_reason"] = stop_reason
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "deal": deal,
            "stage": stage,
            "operator_readiness": readiness,
            "stop_reason": stop_reason,
            "stages_run": stages_run,
            "actions": self.actions.open_actions(str(deal.get("deal_id"))),
            "metrics": {
                **self.metrics,
                "elapsed_ms": elapsed_ms,
                "clock_mode": clock_mode(),
                "external_communication": False,
                "bid_submitted": False,
            },
        }
