"""Controlled Stage 3 commercial research for Opportunity 199.

AUTHORIZED: OpenAI Luna + web, max 4 calls, max $0.10 total Stage 3.
FORBIDDEN: SAM API, Terra/Sol, USAspending, Stage 1/2, supplier/financing applications.
"""

from __future__ import annotations
from application_clock import now_utc

import json
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

STAGE3_COST_CEILING_USD = 0.10
MAX_WEB_CALLS = 4
NOTICE_ID = "610bea2df5c1436994e316259e29a9a6"
CONTRACT_ID = 199

STAGE3_INSTRUCTIONS = """You are a GovCon commercial research assistant for PRODUCT RESALE.
Use web search to locate identifiable public sources.
Return ONLY valid JSON (no markdown fences).
DATA INTEGRITY:
- Never invent prices, discounts, lead times, financing terms, or eligibility.
- If a fact is not supported by a cited public URL/source, status must be UNKNOWN or ASSESSMENT.
- VERIFIED is only for facts directly supported by a cited identifiable source URL.
- Public list/MSRP is NOT acquisition cost unless that price is a purchasable path for a small reseller.
- Marketing phrases (flexible, up to 100%, fast funding) do NOT prove no-PG / no-personal-credit / zero-cash.
Include source_urls arrays for every claim you assert."""


def _now() -> datetime:
    return now_utc()


def build_bom_from_solicitation(attachment_text: str, stage2_facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic BOM from local RFQ + Stage 2 VERIFIED facts. No AI."""
    t = attachment_text or ""
    scope = (stage2_facts.get("scope") or {})
    maint = ((stage2_facts.get("execution") or {}).get("maintenance_required") or {})

    def item(name: str, value: Any, qty: Any, evidence: str, status: str = "VERIFIED") -> dict[str, Any]:
        return {
            "component": name,
            "value": value,
            "quantity": qty,
            "status": status,
            "solicitation_evidence": evidence[:300],
            "source": "solicitation_attachment_text",
        }

    bom: list[dict[str, Any]] = [
        item("base_system", "Dell PowerEdge R670", 14, "PowerEdge R670 Server / 210-BNZH"),
        item("part_number", "210-BNZH", 14, "210-BNZH brand-name basis"),
        item("chassis", '2.5" Chassis with up to 8 SAS/SATA Drives, Smart Flow, Front PERC 12(H965i)', 14, "Chassis Configuration"),
        item("cpu", "Intel Xeon 6 Performance 6515P 2.3G", 14, "Processor"),
        item("cpu_additional", "Intel Xeon 6 Performance 6515P 2.3G", 14, "Additional Processor"),
        item("memory_type", "6400MT/s RDIMMs", 14, "Memory DIMM Type"),
        item("memory_module", "16GB RDIMM 6400MT/s, Single Rank", None, "Memory Capacity — module qty UNKNOWN if not explicit", "UNKNOWN" if "16GB RDIMM" in t else "UNKNOWN"),
        item("raid_controller", "PERC H965i Controller, Front, DCMHS", 14, "RAID/Internal Storage"),
        item("raid_config", "Unconfigured RAID", 14, "RAID Configuration"),
        item("power_supply", "Dual redundant 1+1 800W MM 100-240Vac hot-plug", 14, "Power Supply / Stage2 salient"),
        item("nic", "Broadcom 5719 quad-port 1GbE adapter", 14, "Stage2 salient / networking"),
        item("bezel", "No Bezel", 14, "Bezel"),
        item("idrac", "iDRAC10, Enterprise 17G", 14, "iDRAC10, Enterprise 17G"),
        item("rails", "ReadyRails Static Rails for 24-post Racks A14", 14, "Rack Rails"),
        item("shipping_sku", "PowerEdge R670 Shipping / 1U Shipping Material", 14, "PowerEdge Shipping lines (packaging SKUs, not freight terms)"),
        item(
            "support_prosupport",
            maint.get("value")
            or "ProSupport Next Business Day Onsite + ProSupport 7x24 Technical Support, 3 Years",
            14,
            (maint.get("evidence_text") or "ProSupport ... 3 Years"),
            str(maint.get("status") or "VERIFIED"),
        ),
    ]

    # Drive evidence from Stage 2 salient text
    salient = str(((scope.get("salient_characteristics") or {}).get("value") or ""))
    if "2.4TB SAS" in salient or "2.4TB SAS" in t:
        bom.append(item("storage_drive", "2.4TB SAS hard drives", None, "Stage2 salient / solicitation — drive count UNKNOWN", "UNKNOWN"))
    # CPU quantity: two processors listed (Processor + Additional Processor) → 2 per server ASSESSMENT from structure
    bom.append(
        {
            "component": "cpu_quantity_per_server",
            "value": 2,
            "quantity": 14,
            "status": "ASSESSMENT",
            "solicitation_evidence": "Processor + Additional Processor line items present",
            "source": "solicitation_structure",
            "notes": "ASSESSMENT from two processor line items — not independently labeled as qty=2 elsewhere",
        }
    )
    # Memory module count unknown
    bom.append(
        {
            "component": "memory_module_quantity_per_server",
            "value": None,
            "quantity": None,
            "status": "UNKNOWN",
            "solicitation_evidence": "16GB RDIMM listed; total DIMM count not established as VERIFIED",
            "source": "solicitation_attachment_text",
        }
    )
    return bom


def _block_sam() -> None:
    import sam_client
    import sam_enrich

    def boom(*_a, **_k):
        raise RuntimeError("SAM_API_BLOCKED_DURING_STAGE3_OPP199")

    sam_enrich._sam_api_get = boom  # type: ignore[assignment]
    sam_client.fetch_naics_from_sam = boom  # type: ignore[assignment]


def web_research(
    *,
    objective: str,
    prompt: str,
    notice_id: str,
    spent: float,
    call_index: int,
) -> dict[str, Any]:
    from ai_model_router import FunnelStage, ModelTier, model_for_tier
    from ai_pricing import estimate_pre_call_cost_usd
    from openai_runtime import create_response, extract_json_object, get_last_response_meta

    model = model_for_tier(ModelTier.CHEAP)
    if "luna" not in model.lower():
        raise RuntimeError(f"STOP: unexpected model {model}")
    if "terra" in model.lower() or "sol" in model.lower():
        raise RuntimeError(f"STOP: Terra/Sol forbidden: {model}")

    content = [{"type": "input_text", "text": prompt}]
    est = estimate_pre_call_cost_usd(
        model=model,
        input_chars=len(STAGE3_INSTRUCTIONS) + len(prompt),
        max_output_tokens=2500,
        web_search=True,
    )
    if spent + float(est or 0) > STAGE3_COST_CEILING_USD:
        return {
            "skipped": True,
            "reason": "estimated_spend_would_exceed_0.10_ceiling",
            "objective": objective,
            "estimated_pre_call_usd": est,
            "spent_so_far": spent,
        }

    text = create_response(
        task=f"stage3_web_{call_index}",
        instructions=STAGE3_INSTRUCTIONS,
        content=content,
        max_output_tokens=2500,
        web_search=True,
        notice_id=notice_id,
        funnel_stage=FunnelStage.STAGE_3.value,
        model_tier=ModelTier.CHEAP,
        automatic=False,
        use_cache=True,
    )
    meta = dict(get_last_response_meta() or {})
    cost = float(meta.get("estimated_cost_usd") or 0.0)
    parsed: dict[str, Any] | list[Any] | None
    try:
        parsed = extract_json_object(text)
    except Exception:
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = {"raw_text": text[:8000], "parse_error": True}

    return {
        "skipped": False,
        "objective": objective,
        "model": meta.get("model") or model,
        "prompt": prompt[:2000],
        "request_id": meta.get("request_id"),
        "tokens": {
            "input": meta.get("input_tokens"),
            "cached_input": meta.get("cached_input_tokens"),
            "output": meta.get("output_tokens"),
            "reasoning": meta.get("reasoning_tokens"),
            "total": meta.get("total_tokens"),
        },
        "actual_cost_usd": cost,
        "cache_hit": bool(meta.get("cache_hit")),
        "openai_called": bool(meta.get("openai_called")),
        "result": parsed,
        "raw_excerpt": (text or "")[:1500],
    }


def main() -> int:
    _block_sam()

    from api_budget import get_usage_snapshot
    from ai_funnel import stage0_evaluate
    from ai_stage1 import resolve_current_stage1_result
    from ai_stage2 import assign_economic_requirements, run_stage2_evidence
    from database import SessionLocal
    from deal_engine import build_deal_economics, evaluate_financing_execution_gate
    from historical_context import get_historical_context_for_opportunity
    from knowledge_store import record_research_event, upsert_deal_state
    from models import (
        Contract,
        FinancingProvider,
        FinancingTerm,
        KnowledgeProduct,
        KnowledgeSupplier,
        SupplierOffer,
    )
    from product_deal import plan_product_research
    from sam_scarcity import sam_budget_snapshot

    sam_before = get_usage_snapshot()["sam_used_today"]
    session = SessionLocal()
    calls: list[dict[str, Any]] = []
    spent = 0.0

    try:
        c = session.query(Contract).filter_by(id=CONTRACT_ID).one()
        assert c.notice_id == NOTICE_ID
        s0 = stage0_evaluate(c)
        s1r = resolve_current_stage1_result(c, stage0=s0)
        assert s1r.get("current"), "Stage 1 cache required"
        s1 = s1r["result"]
        s2 = run_stage2_evidence(
            c, stage0=s0, stage1=s1, stage1_resolution=s1r, automatic=False
        )
        assert s2.get("cache_hit"), "Stage 2 must be cache HIT — no Stage 2 spend"
        facts = s2.get("facts") or {}
        econ_map = assign_economic_requirements(facts, stage0=s0, stage1=s1)
        assert econ_map["costs"]["installation"]["status"] == "REQUIRED_UNKNOWN"

        bom = build_bom_from_solicitation(c.attachment_text or "", facts)
        hist = get_historical_context_for_opportunity(c, session=session, include_gs_lookup=True)

        # --- Web call 1: Dell channel / authorization ---
        bom_summary = json.dumps(
            [{"component": b["component"], "value": b["value"], "qty": b["quantity"], "status": b["status"]} for b in bom],
            default=str,
        )[:3500]
        prompt1 = f"""Research objective: Dell federal channel / authorization for brand-name PowerEdge R670 RFQs.

Context (local VERIFIED solicitation facts):
- Solicitation 47QACA26Q0439 / GSA FAS
- Brand-name only Dell PowerEdge R670 part 210-BNZH, qty 14
- Requires vendor be OEM, authorized dealer, distributor, or reseller with OEM authorization letter
- No gray market; OEM items only
- Delivery Alexandria VA within 30 days ARO

Questions (answer with evidence URLs):
1) Must the quoting company itself be a Dell authorized reseller, or can they buy through an authorized distributor who provides OEM letter?
2) What is Dell PartnerDirect / federal channel practice for this situation?
3) Deal registration requirements if any?
4) Can a new small business source via authorized distributor for a federal brand-name RFQ?
5) Channel status recommendation: CHANNEL_PASS / CHANNEL_FAIL / CHANNEL_UNRESOLVED

Return JSON keys: channel_status, findings[], must_we_be_authorized_reseller (VERIFIED|ASSESSMENT|UNKNOWN), distributor_can_supply_oem_letter (same), deal_registration, new_sb_path, source_urls[], unknowns[]
"""
        r1 = web_research(objective="dell_channel_authorization", prompt=prompt1, notice_id=NOTICE_ID, spent=spent, call_index=1)
        calls.append(r1)
        if not r1.get("skipped"):
            spent += float(r1.get("actual_cost_usd") or 0)

        # --- Web call 2: suppliers / pricing / availability ---
        if len([x for x in calls if not x.get("skipped")]) < MAX_WEB_CALLS and spent < STAGE3_COST_CEILING_USD:
            prompt2 = f"""Research objective: Find legitimate CURRENT suppliers for Exact Dell PowerEdge R670 configuration (part 210-BNZH / configured BOM) for qty 14 to Alexandria VA.

BOM summary:
{bom_summary}

Requirements:
- Prefer Dell direct federal, Dell authorized distributors/resellers (CDW, Insight, Connection, SHI, Dell Partner, etc.)
- EXCLUDE eBay, used, remanufactured, gray market, unverifiable marketplaces
- For each supplier: name, url, supplier_type, dell_channel_evidence, exact_config_match (YES|NO|UNKNOWN), price_status (VERIFIED_PUBLIC_PRICE|QUOTE_REQUIRED|UNKNOWN), public_price_if_any, availability_evidence, lead_time_evidence, payment_terms_if_public
- Do NOT invent discounts or wholesale prices
- Assess 14-unit / 30-day delivery feasibility: DELIVERY_PASS|DELIVERY_FAIL|DELIVERY_UNRESOLVED with evidence

Return JSON: suppliers[], delivery_status, delivery_notes, source_urls[], unknowns[]
"""
            r2 = web_research(objective="supplier_price_availability", prompt=prompt2, notice_id=NOTICE_ID, spent=spent, call_index=2)
            calls.append(r2)
            if not r2.get("skipped"):
                spent += float(r2.get("actual_cost_usd") or 0)

        # --- Web call 3: financing ---
        if len([x for x in calls if not x.get("skipped")]) < MAX_WEB_CALLS and spent < STAGE3_COST_CEILING_USD:
            prompt3 = """Research objective: Financing/payment structures for a small business buying Dell servers for a US federal GSA purchase order / award with HARD constraints:
- NO personal guarantee
- NO personal credit reliance
- $0 personal cash upfront
- Supplier preferably paid directly
- Product/equipment purchase against government PO

Find specific providers with public documentation URLs. For each provider return:
name, website, product_type, min_transaction, max_transaction, startup_eligibility, government_po_eligibility,
advance_percentage, supplier_paid_directly, pg_required (true/false/UNKNOWN), personal_credit_required (true/false/UNKNOWN),
cash_upfront_required (true/false/UNKNOWN), fees_rate_structure, payment_timing, recourse, application_requirements,
source_urls, classification (FINANCING_PASS|FINANCING_FAIL|FINANCING_UNRESOLVED), is_generic_marketing (bool)

Marketing language alone cannot yield FINANCING_PASS.

Return JSON: providers[], overall_financing_status, source_urls[], unknowns[]
"""
            r3 = web_research(objective="financing_no_pg_zero_cash", prompt=prompt3, notice_id=NOTICE_ID, spent=spent, call_index=3)
            calls.append(r3)
            if not r3.get("skipped"):
                spent += float(r3.get("actual_cost_usd") or 0)

        # --- Web call 4: compliance TAA/BAA/COO if budget remains ---
        if len([x for x in calls if not x.get("skipped")]) < MAX_WEB_CALLS and spent < STAGE3_COST_CEILING_USD:
            prompt4 = """Research objective: Country-of-origin / Trade Agreements Act / Buy American applicability for Dell PowerEdge R670 servers sold into US federal open-market RFQs (GSA FAS), and Nonmanufacturer Rule implications for total small business set-aside supply of Dell servers.

Do not assume Dell federal sales automatically means TAA compliant for every config.
Return JSON with fields:
baa_status (VERIFIED|NOT_APPLICABLE|UNKNOWN),
taa_status (VERIFIED|NOT_APPLICABLE|UNKNOWN),
coo_status (VERIFIED|NOT_APPLICABLE|UNKNOWN),
nmr_status (VERIFIED|NOT_APPLICABLE|UNKNOWN),
evidence[], source_urls[], unknowns[], notes
"""
            r4 = web_research(objective="compliance_baa_taa_coo_nmr", prompt=prompt4, notice_id=NOTICE_ID, spent=spent, call_index=4)
            calls.append(r4)
            if not r4.get("skipped"):
                spent += float(r4.get("actual_cost_usd") or 0)

        # --- Persist knowledge (candidates as ASSESSMENT/UNKNOWN; only clear URL-backed identity as needed) ---
        now = _now()
        product = (
            session.query(KnowledgeProduct)
            .filter_by(part_number="210-BNZH", model="PowerEdge R670")
            .first()
        )
        if product is None:
            product = KnowledgeProduct(
                manufacturer=None,  # manufacturer field stays UNKNOWN unless explicit
                brand="Dell",
                model="PowerEdge R670",
                part_number="210-BNZH",
                description="Dell PowerEdge R670 configured per RFQ 47QACA26Q0439",
                specifications_json={"bom": bom, "solicitation": "47QACA26Q0439"},
                source="solicitation_stage2",
                source_url=None,
                verification_status="VERIFIED",
                last_verified_at=now,
            )
            session.add(product)
            session.flush()

        suppliers_out: list[dict[str, Any]] = []
        channel_out: dict[str, Any] = {"status": "CHANNEL_UNRESOLVED"}
        financing_out: dict[str, Any] = {"status": "FINANCING_UNRESOLVED", "providers": []}
        compliance_out: dict[str, Any] = {}
        delivery_out: dict[str, Any] = {"status": "DELIVERY_UNRESOLVED"}

        for call in calls:
            if call.get("skipped"):
                continue
            obj = call.get("objective")
            result = call.get("result") if isinstance(call.get("result"), dict) else {}
            record_research_event(
                session,
                contract_id=CONTRACT_ID,
                task_code=str(obj),
                reason=call.get("objective"),
                source="openai_web_luna",
                source_url=",".join((result.get("source_urls") or [])[:5]) if isinstance(result.get("source_urls"), list) else None,
                result_status="ASSESSMENT",
                facts_produced_json=result if isinstance(result, dict) else [],
                evidence_json={"request_id": call.get("request_id"), "tokens": call.get("tokens")},
                cost_usd=Decimal(str(call.get("actual_cost_usd") or 0)),
                cache_hit=bool(call.get("cache_hit")),
                reusable=True,
                authorized=True,
                executed=True,
                external_api_calls=1 if call.get("openai_called") else 0,
                retrieved_at=now,
            )

            if obj == "dell_channel_authorization":
                channel_out = {
                    "status": result.get("channel_status") or "CHANNEL_UNRESOLVED",
                    "findings": result.get("findings"),
                    "must_we_be_authorized_reseller": result.get("must_we_be_authorized_reseller"),
                    "distributor_can_supply_oem_letter": result.get("distributor_can_supply_oem_letter"),
                    "deal_registration": result.get("deal_registration"),
                    "new_sb_path": result.get("new_sb_path"),
                    "source_urls": result.get("source_urls"),
                    "unknowns": result.get("unknowns"),
                    "verification_note": "OpenAI web research — claims are ASSESSMENT unless independently verified from cited URLs",
                }
            elif obj == "supplier_price_availability":
                delivery_out = {
                    "status": result.get("delivery_status") or "DELIVERY_UNRESOLVED",
                    "notes": result.get("delivery_notes"),
                }
                for srow in result.get("suppliers") or []:
                    if not isinstance(srow, dict):
                        continue
                    name = str(srow.get("name") or "").strip()
                    if not name:
                        continue
                    website = srow.get("url") or srow.get("website")
                    domain = None
                    if isinstance(website, str) and "://" in website:
                        domain = website.split("/")[2][:256]
                    ks = session.query(KnowledgeSupplier).filter_by(name=name[:512]).first()
                    if ks is None:
                        ks = KnowledgeSupplier(
                            name=name[:512],
                            website=(str(website)[:512] if website else None),
                            domain=domain,
                            manufacturer_relationship=str(srow.get("dell_channel_evidence") or srow.get("supplier_type") or "")[:256] or None,
                            source="openai_web_research",
                            verification_status="ASSESSMENT",  # not auto-verified
                            last_verified_at=None,
                            notes=json.dumps(srow, default=str)[:2000],
                        )
                        session.add(ks)
                        session.flush()
                    price_status = str(srow.get("price_status") or "UNKNOWN").upper()
                    unit_price = None
                    # Never store invented public price as VERIFIED acquisition cost
                    offer = SupplierOffer(
                        product_id=product.id,
                        supplier_id=ks.id,
                        contract_id=CONTRACT_ID,
                        unit_price=None,
                        quantity_basis=Decimal("14"),
                        availability=str(srow.get("availability_evidence") or "")[:128] or None,
                        stock_status=None,
                        lead_time=str(srow.get("lead_time_evidence") or "")[:128] or None,
                        shipping_included=None,
                        retrieved_at=now,
                        source_url=(str(website)[:1024] if website else None),
                        source_reference="stage3_web_supplier_discovery",
                        source_type="OTHER",
                        temporal_class="CURRENT",
                        verification_status="UNKNOWN" if price_status != "VERIFIED_PUBLIC_PRICE" else "ASSESSMENT",
                        notes=json.dumps({"price_status": price_status, "public_price_if_any": srow.get("public_price_if_any"), "exact_config_match": srow.get("exact_config_match"), "raw": srow}, default=str)[:4000],
                    )
                    session.add(offer)
                    suppliers_out.append(
                        {
                            "supplier": name,
                            "channel": srow.get("dell_channel_evidence") or srow.get("supplier_type"),
                            "exact_match": srow.get("exact_config_match"),
                            "price_status": price_status,
                            "price": srow.get("public_price_if_any"),
                            "availability": srow.get("availability_evidence"),
                            "lead_time": srow.get("lead_time_evidence"),
                            "source": website,
                            "verification_status": "ASSESSMENT",
                        }
                    )
            elif obj == "financing_no_pg_zero_cash":
                financing_out["status"] = result.get("overall_financing_status") or "FINANCING_UNRESOLVED"
                for prow in result.get("providers") or []:
                    if not isinstance(prow, dict):
                        continue
                    pname = str(prow.get("name") or "").strip()
                    if not pname:
                        continue
                    fp = session.query(FinancingProvider).filter_by(name=pname[:512]).first()
                    if fp is None:
                        fp = FinancingProvider(
                            name=pname[:512],
                            website=(str(prow.get("website") or "")[:512] or None),
                            product_types=str(prow.get("product_type") or "")[:256] or None,
                            source="openai_web_research",
                            verification_status="ASSESSMENT",
                            notes=json.dumps(prow, default=str)[:2000],
                        )
                        session.add(fp)
                        session.flush()
                    # Do NOT mark PASS hard gates from marketing — store as unresolved terms
                    pg = prow.get("pg_required")
                    pc = prow.get("personal_credit_required")
                    cash = prow.get("cash_upfront_required")
                    # Only store boolean if explicitly true/false; else None
                    def _tri(v):
                        if v is True or v is False:
                            return v
                        if isinstance(v, str) and v.lower() in {"true", "false"}:
                            return v.lower() == "true"
                        return None

                    term = FinancingTerm(
                        provider_id=fp.id,
                        contract_id=CONTRACT_ID,
                        advance_structure=str(prow.get("advance_percentage") or "")[:512] or None,
                        supplier_payment_mechanics=str(prow.get("supplier_paid_directly") or "")[:1024] or None,
                        pg_required=_tri(pg),
                        personal_credit_required=_tri(pc),
                        cash_deposit_required=_tri(cash),
                        eligibility=str(prow.get("government_po_eligibility") or prow.get("startup_eligibility") or "")[:2000] or None,
                        source="openai_web_research",
                        source_url=",".join(prow.get("source_urls") or [])[:1024] if isinstance(prow.get("source_urls"), list) else (str(prow.get("website") or "")[:1024] or None),
                        verification_status="ASSESSMENT",
                        temporal_class="CURRENT",
                        is_generic_marketing=bool(prow.get("is_generic_marketing")),
                        retrieved_at=now,
                        notes=json.dumps(prow, default=str)[:4000],
                    )
                    session.add(term)
                    financing_out["providers"].append(
                        {
                            "provider": pname,
                            "product": prow.get("product_type"),
                            "classification": prow.get("classification") or "FINANCING_UNRESOLVED",
                            "pg": pg,
                            "personal_credit": pc,
                            "cash_upfront": cash,
                            "source_urls": prow.get("source_urls"),
                            "verification_status": "ASSESSMENT",
                        }
                    )
            elif obj == "compliance_baa_taa_coo_nmr":
                compliance_out = {
                    "baa": result.get("baa_status") or "UNKNOWN",
                    "taa": result.get("taa_status") or "UNKNOWN",
                    "coo": result.get("coo_status") or "UNKNOWN",
                    "nmr": result.get("nmr_status") or "UNKNOWN",
                    "evidence": result.get("evidence"),
                    "source_urls": result.get("source_urls"),
                    "unknowns": result.get("unknowns"),
                    "notes": result.get("notes"),
                    "verification_note": "Web research ASSESSMENT unless solicitation/official source independently establishes VERIFIED",
                }

        # Local freight responsibility remains UNKNOWN (no FOB in RFQ)
        freight = {
            "responsibility": "UNKNOWN",
            "cost": None,
            "status": "FREIGHT_QUOTE_REQUIRED",
            "evidence": "No FOB clause found in local RFQ text; shipping SKUs are packaging lines not freight terms",
        }

        # Economics — no invented revenue or supplier cost
        costs = econ_map["costs"]
        economics = build_deal_economics(
            operator_bid_amount=None,
            bid_amount_verified=False,
            costs=costs,
        )
        financing_gate = evaluate_financing_execution_gate(financing_term=None, term_verified=False)
        plan = plan_product_research(c, stage0=s0, stage1=s1, stage2=s2, session=session)

        # Classification
        has_supplier_candidates = len(suppliers_out) > 0
        channel_status = str(channel_out.get("status") or "CHANNEL_UNRESOLVED").upper()
        fin_status = str(financing_out.get("status") or "FINANCING_UNRESOLVED").upper()
        verified_acquisition = None  # none verified
        any_quote_required = any(str(s.get("price_status")).upper() == "QUOTE_REQUIRED" for s in suppliers_out) or not verified_acquisition

        if channel_status == "CHANNEL_FAIL" and True:
            # only fail if we treat AI channel_fail as fatal — user said FAIL only on VERIFIED fatal
            # Keep as unresolved unless we have verified fatal
            next_state = "STAGE3_RESEARCH_INCOMPLETE"
        if has_supplier_candidates and any_quote_required:
            next_state = "STAGE3_NEEDS_QUOTES"
        elif not has_supplier_candidates and spent > 0:
            next_state = "STAGE3_RESEARCH_INCOMPLETE"
        else:
            next_state = "STAGE3_NEEDS_QUOTES"

        # Force: no commercial PASS without verified costs + financing PASS
        if (
            verified_acquisition is not None
            and fin_status == "FINANCING_PASS"
            and channel_status == "CHANNEL_PASS"
            and economics.get("actual_profit_result", {}).get("status") == "CALCULATED"
        ):
            next_state = "STAGE3_COMMERCIAL_PASS"
        else:
            # Prefer NEEDS_QUOTES when paths exist
            if has_supplier_candidates or channel_status == "CHANNEL_UNRESOLVED":
                next_state = "STAGE3_NEEDS_QUOTES"
            else:
                next_state = "STAGE3_RESEARCH_INCOMPLETE"

        operator_packet = {
            "supplier_actions": [
                {
                    "who": s.get("supplier"),
                    "why": "Potential authorized channel candidate from Stage 3 web research (ASSESSMENT — confirm authorization)",
                    "what_bom": "Exact RFQ BOM for Dell PowerEdge R670 / 210-BNZH as listed in solicitation pages 2-5",
                    "quantity": 14,
                    "delivery_destination": "Kamran Jones, 2415 Eisenhower Avenue, Alexandria, Virginia 22314",
                    "required_delivery": "30 days ARO / from DOA",
                    "channel_oem_letter": "Must provide OEM/authorized dealer-distributor-reseller letter",
                    "pricing_needed": "Firm unit and extended price for exact configuration; state if shipping included to Alexandria VA",
                    "availability_needed": "Confirm 14 units can ship within 30 days ARO; state build-to-order lead time if any",
                    "payment_terms_needed": "Net terms accepting government PO / assignment if available",
                    "source": s.get("source"),
                }
                for s in suppliers_out[:8]
            ],
            "financing_actions": [
                {
                    "who": p.get("provider"),
                    "what": "Confirm product purchase financing against federal GSA PO for Dell server RFQ",
                    "approximate_amount": "UNKNOWN — do not invent; ask provider about eligibility ranges only",
                    "questions": [
                        "Personal guarantee required? (must be NO)",
                        "Personal credit required? (must be NO)",
                        "Borrower cash contribution / deposit required? (must be $0)",
                        "Supplier paid directly?",
                        "Minimum/maximum transaction for new small business",
                        "Government PO / federal contract eligibility",
                        "Fees, advance %, approval timing, recourse",
                    ],
                    "source_urls": p.get("source_urls"),
                }
                for p in financing_out.get("providers") or []
            ],
            "other_actions": [
                "Re-read RFQ for any FOB/shipping responsibility language not captured; request supplier to state shipping responsibility explicitly",
                "Confirm which FAR Buy American / Trade Agreements clauses are checked/applicable in the RFQ clause matrix",
                "Do not submit quote until OEM letter path and financing hard gates are verified",
            ],
        }

        upsert_deal_state(
            session,
            CONTRACT_ID,
            {
                "core_fit": "CORE_PRODUCT",
                "pipeline_stage": "needs_research",
                "decision": "NEEDS_RESEARCH",
                "reason_codes": [next_state, channel_status, fin_status, delivery_out.get("status")],
                "economics": economics,
                "financing_gate": financing_gate,
                "research_plan": plan,
                "funnel_checkpoint": {
                    "stage3_web_calls": len([x for x in calls if not x.get("skipped") and x.get("openai_called")]),
                    "stage3_cost_usd": spent,
                    "bom": bom,
                    "channel": channel_out,
                    "next_state": next_state,
                },
                "ai_cost_usd": spent,
            },
        )
        session.commit()

        sam_after = get_usage_snapshot()["sam_used_today"]
        report = {
            "integrity_fix": {
                "installation_cost_status": econ_map["costs"]["installation"]["status"],
                "subcontract_cost_status": econ_map["costs"]["subcontract"]["status"],
                "expected": "REQUIRED_UNKNOWN when installation_required UNKNOWN",
            },
            "bom": bom,
            "channel": channel_out,
            "suppliers": suppliers_out,
            "acquisition_cost": {
                "verified_usable_cost": None,
                "quote_required": True,
                "unknown": True,
            },
            "delivery": delivery_out,
            "freight": freight,
            "compliance": compliance_out,
            "financing": financing_out,
            "history": {
                "local": hist,
                "external_usaspending_calls": 0,
            },
            "economics": economics,
            "financing_gate": financing_gate,
            "operator_action_packet": operator_packet,
            "calls": [
                {
                    "objective": c.get("objective"),
                    "skipped": c.get("skipped"),
                    "reason": c.get("reason"),
                    "model": c.get("model"),
                    "request_id": c.get("request_id"),
                    "tokens": c.get("tokens"),
                    "actual_cost_usd": c.get("actual_cost_usd"),
                    "openai_called": c.get("openai_called"),
                }
                for c in calls
            ],
            "stage3_total_cost_usd": spent,
            "sam_before": sam_before,
            "sam_after": sam_after,
            "sam_delta": sam_after - sam_before,
            "sam_budget": sam_budget_snapshot(),
            "next_state": next_state,
            "LIVE_STAGE1": 0,
            "LIVE_STAGE2": 0,
        }
        out_path = Path(__file__).resolve().parent.parent / "_opp199_stage3_report.json"
        out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(json.dumps(report, indent=2, default=str))
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
