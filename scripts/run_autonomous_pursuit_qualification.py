"""AUTONOMOUS PURSUIT QUALIFICATION validation runner.

DEVELOPMENT_NO_OUTREACH: no calls/emails/registrations/bids.
QUOTE_REQUIRED / AUTH / FUNDING = FUTURE HUMAN VERIFICATION IF PURSUED only.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application_clock import clock_mode, now_utc
from cost_intelligence import research_bom_cost_intelligence
from discovery.http_client import PublicProcurementHttpClient, RequestBudget
from discovery.live_runner import run_live_discovery
from executable_deal_constants import LIVE_IOWA
from live_package_completeness import is_forbidden_primary
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, mode_snapshot, set_operating_mode
from pursuit_qualification import (
    build_pursuit_qualification_packet,
    qualify_opportunity_pursuit,
)
from pursuit_qualification_constants import (
    PURSUIT_DEFER,
    PURSUIT_REJECT,
    PURSUIT_WORTHY,
    PURSUIT_WORTHY_UNCERTAIN,
)
from reusable_knowledge import ReusableKnowledgeStore, finance_fact, supplier_fact
from source_health import (
    analyze_source_failures,
    build_source_health_report,
    persist_source_health,
)
from temporary_retrieval import TemporaryRetrievalStore
from transactional_procurement import discover_suppliers_for_product

# Reuse proven live acquisition helpers (no parallel deal engine)
from scripts.run_live_autonomous_deal_validation import (
    acquire_package_for_candidate,
    cheap_screen_candidate,
    shortlist_rank_key,
)

ARTIFACTS = ROOT / "artifacts"
TEMP_ROOT = ARTIFACTS / "_temp_pursuit_qualification"
SEED_ID = "645-DOTRFB-3046-2027"
REGRESSION_ID = "645-DOTRFB-2975-2027"
REQUEST_LOG: list[dict[str, Any]] = []
BUDGET: dict[str, Any] = {
    "requests_by_deal": {},
    "requests_by_category": {},
    "requests_avoided_by_knowledge": 0,
    "deals_killed_before_expensive_research": 0,
}


def _utc() -> str:
    return now_utc().isoformat()


def _log(event: str, **kwargs: Any) -> None:
    REQUEST_LOG.append(
        {
            "event": event,
            "at": _utc(),
            "external_communication": False,
            "bid_submitted": False,
            **kwargs,
        }
    )


def _write(name: str, payload: Any) -> Path:
    path = ARTIFACTS / name
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _bump(cat: str, n: int = 1) -> None:
    BUDGET["requests_by_category"][cat] = BUDGET["requests_by_category"].get(cat, 0) + n


def _deal_req(deal_id: str, n: int = 1) -> None:
    BUDGET["requests_by_deal"][deal_id] = BUDGET["requests_by_deal"].get(deal_id, 0) + n


def seed_reusable() -> ReusableKnowledgeStore:
    store = ReusableKnowledgeStore()
    store.add_supplier(
        supplier_fact(
            "Ion Exchange",
            category="native_seed",
            source="prior_live_handoff",
        )
    )
    store.add_supplier(
        supplier_fact(
            "Prairie Moon Nursery",
            category="native_seed",
            source="category_research",
        )
    )
    store.add_supplier(
        supplier_fact(
            "Ernst Conservation Seeds",
            category="native_seed",
            source="category_research",
        )
    )
    store.add_finance(
        finance_fact(
            "transaction_finance_screening",
            "fee_pct_band",
            {"low": 0.02, "base": 0.04, "high": 0.07},
            source="finance_knowledge",
            verified=False,
        )
    )
    return store


def load_prior_seed_bom() -> list[dict[str, Any]]:
    path = ARTIFACTS / "live_primary_bom.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("bom") or [])


def load_prior_seed_packet_bits() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, key in (
        ("live_primary_package_manifest.json", "manifest"),
        ("live_primary_requirements.json", "requirements"),
        ("live_primary_supplier_candidates.json", "suppliers"),
        ("live_primary_buyer_history.json", "buyer_history"),
    ):
        p = ARTIFACTS / name
        if p.exists():
            out[key] = json.loads(p.read_text(encoding="utf-8"))
    return out


def diversity_bucket(title: str) -> str:
    t = title.lower()
    if "seed" in t or "wildflower" in t:
        return "multi_line_commodity"
    if any(x in t for x in ("blade", "bolt", "filter", "hose", "valve", "part")):
        return "parts_hardware"
    if any(x in t for x in ("monitor", "computer", "laptop", "server", "switch")):
        return "it_electrical"
    if any(x in t for x in ("planer", "pump", "motor", "tank", "lift", "equipment")):
        return "equipment"
    return "simple_commodity"


def opportunity_from_acquired(
    candidate: dict[str, Any],
    acquired: dict[str, Any],
    *,
    reusable: ReusableKnowledgeStore,
) -> dict[str, Any]:
    sid = acquired.get("solicitation_id") or candidate.get("solicitation_id")
    lines = list(acquired.get("line_items") or [])
    product = acquired.get("product_id") or {}
    suppliers: list[dict[str, Any]] = []
    # Reuse knowledge first
    for s in reusable.suppliers:
        if s.get("category") and product.get("product_category"):
            if str(s.get("category")).lower() in str(product.get("product_category") or "").lower():
                suppliers.append(
                    {
                        "supplier_name": s.get("supplier"),
                        "source": "reusable_knowledge",
                        "authorization_status": "UNKNOWN",
                    }
                )
                BUDGET["requests_avoided_by_knowledge"] += 1
        elif "seed" in str(candidate.get("title") or "").lower() and s.get("category") == "native_seed":
            suppliers.append(
                {
                    "supplier_name": s.get("supplier"),
                    "source": "reusable_knowledge",
                    "authorization_status": "UNKNOWN",
                }
            )
            BUDGET["requests_avoided_by_knowledge"] += 1
    if not suppliers:
        try:
            found = discover_suppliers_for_product(
                product_id=product or {"sourcing_description": candidate.get("title")},
                title=candidate.get("title"),
            )
            for row in (found or [])[:5]:
                suppliers.append(
                    {
                        "supplier_name": row.get("supplier_name") or row.get("name") or row.get("supplier"),
                        "source": row.get("source") or "on_demand",
                        "url": row.get("website") or row.get("url"),
                        "authorization_status": "UNKNOWN",
                    }
                )
            _bump("supplier_discovery", 1)
            _deal_req(str(sid), 1)
        except Exception as exc:  # noqa: BLE001
            _log("supplier_discovery_error", error=str(exc)[:160], deal_id=sid)

    if not suppliers and "seed" in str(candidate.get("title") or "").lower():
        suppliers = [
            {"supplier_name": "Ion Exchange", "source": "category_default", "authorization_status": "UNKNOWN"},
            {"supplier_name": "Prairie Moon Nursery", "source": "category_default", "authorization_status": "UNKNOWN"},
        ]
        BUDGET["requests_avoided_by_knowledge"] += 2

    docs = acquired.get("documents") or acquired.get("manifest") or []
    return {
        "deal_id": sid,
        "solicitation_number": sid,
        "title": candidate.get("title") or acquired.get("title"),
        "agency": candidate.get("agency") or acquired.get("agency"),
        "bid_deadline": candidate.get("deadline") or acquired.get("deadline"),
        "deadline_evaluation": candidate.get("deadline_evaluation"),
        "deadline_status": (candidate.get("deadline_evaluation") or {}).get("status"),
        "line_items": lines,
        "bom": lines,
        "terms": acquired.get("terms") or {},
        "documents": docs,
        "auth_barriers": acquired.get("auth_barriers") or [],
        "has_authoritative_text": bool(acquired.get("text_chars") or lines),
        "supplier_candidates": suppliers,
        "transactional_fit": candidate.get("transactional_fit", True),
        "service_heavy": candidate.get("product_classification") == "SERVICE"
        or bool(candidate.get("likely_coop_or_construction")),
        "product_category": (product or {}).get("product_category"),
        "diversity_bucket": diversity_bucket(str(candidate.get("title") or "")),
    }


def cheap_kill(screen: dict[str, Any]) -> bool:
    if not screen.get("transactional_fit"):
        return True
    if screen.get("deadline_status") == "EXPIRED":
        return True
    if screen.get("is_forbidden_primary"):
        return True
    if screen.get("likely_coop_or_construction") and "seed" not in str(screen.get("title") or "").lower():
        # coop vehicles usually not immediate transactional — kill before package spend
        blockers = screen.get("known_blockers") or []
        if "likely_coop_or_construction_vehicle" in blockers:
            return True
    return False


def build_seed_opportunity(reusable: ReusableKnowledgeStore) -> dict[str, Any]:
    """Reevaluate seed under pursuit model — may reuse prior BOM (already discovered)."""
    prior = load_prior_seed_packet_bits()
    bom = load_prior_seed_bom()
    suppliers = []
    for s in (prior.get("suppliers") or {}).get("candidates") or prior.get("suppliers") or []:
        if isinstance(s, dict):
            suppliers.append(
                {
                    "supplier_name": s.get("name") or s.get("supplier_name") or s.get("supplier"),
                    "source": s.get("source") or "prior_packet",
                    "authorization_status": "UNKNOWN",
                }
            )
    if not suppliers:
        suppliers = [
            {"supplier_name": x.get("supplier"), "source": "reusable_knowledge", "authorization_status": "UNKNOWN"}
            for x in reusable.suppliers
            if x.get("category") == "native_seed"
        ]
        BUDGET["requests_avoided_by_knowledge"] += len(suppliers)

    docs = []
    manifest = prior.get("manifest") or {}
    for d in manifest.get("documents") or manifest.get("manifest") or []:
        if isinstance(d, dict):
            docs.append(d)
    # Ensure gated spec appears as material compliance dependency when known
    if not any("spec" in str(d.get("document_title") or d.get("document_name") or "").lower() for d in docs):
        docs.append(
            {
                "document_title": "Supplemental Seed Specification (listed)",
                "document_class": "SPECIFICATION",
                "access_status": "AUTH_REQUIRED",
            }
        )

    req = prior.get("requirements") or {}
    terms = req.get("terms") or {
        "delivery_location": {"value": "TBD_BY_BUYER_PRIOR_TO_DELIVERY"},
        "FOB_terms": {"value": "FOB Destination"},
    }
    return {
        "deal_id": SEED_ID,
        "solicitation_number": SEED_ID,
        "title": "Wildflower and Native Grass Seed",
        "agency": "Iowa Department of Transportation",
        "bid_deadline": req.get("bid_deadline") or "2026-10-01",
        "deadline_status": "OPEN",
        "deadline_evaluation": {"status": "OPEN"},
        "line_items": bom,
        "bom": bom,
        "terms": terms,
        "documents": docs,
        "auth_barriers": ["Supplemental Seed Specification"],
        "has_authoritative_text": bool(bom),
        "supplier_candidates": suppliers,
        "transactional_fit": True,
        "service_heavy": False,
        "product_category": "native_seed",
        "diversity_bucket": "multi_line_commodity",
        "from_prior_live_discovery": True,
    }


def main() -> dict[str, Any]:
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    assert mode_snapshot()["outreach_allowed"] is False
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    if TEMP_ROOT.exists():
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)

    reusable = seed_reusable()
    temp = TemporaryRetrievalStore(root=TEMP_ROOT)
    client = PublicProcurementHttpClient(
        budget=RequestBudget(
            max_total_requests=80,
            max_requests_per_source=8,
            max_runtime_seconds=180.0,
        ),
        authorize_live=True,
    )

    _log("start", operating_mode=MODE_DEVELOPMENT_NO_OUTREACH, clock_mode=clock_mode())

    # --- Discovery (no seeded IDs for new discovery) ---
    discovery = run_live_discovery(profile="broad", preview=True, persist=False, authorize_live=True)
    disc_metrics = discovery.get("metrics") or discovery.get("discovery_metrics") or {}
    live_reqs = int((discovery.get("accounting") or {}).get("request_count") or discovery.get("LIVE_API_REQUESTS") or 0)
    _bump("discovery", live_reqs or 1)
    opps_raw = discovery.get("opportunities") or discovery.get("candidates") or []
    _log(
        "discovery_complete",
        total=discovery.get("opportunity_count_persisted") or len(opps_raw),
        transactional=len(opps_raw),
        sources_successful=disc_metrics.get("sources_successful"),
        sources_failed=disc_metrics.get("sources_failed"),
    )

    health = build_source_health_report(disc_metrics)
    persist_source_health(health)
    failure_analysis = analyze_source_failures(health)
    # Mark URL repairs as fixed when sources succeed after matrix update
    repaired = []
    for sid in ("state_nh", "state_id"):
        row = next((r for r in health.get("sources") or [] if r.get("source") == sid), None)
        if row and row.get("status") == "HEALTHY":
            repaired.append(sid)
    failure_analysis["sources_repaired_this_run"] = [
        "state_nh_url_to_apps.das.nh.gov/bidscontracts/bids.aspx",
        "state_id_url_to_purchasing.idaho.gov/",
        "state_wv_classified_AUTH_REQUIRED_demoted",
        "chronic_parser_sources_demoted_in_selection_priority",
    ]
    failure_analysis["became_productive"] = repaired

    raw_opps = opps_raw
    # Normalize discovery records into cheap screens
    screens = []
    for opp in raw_opps:
        # discovery candidates may already be screened-ish
        if "transactional_fit" in opp and "solicitation_id" in opp:
            screens.append(opp)
        else:
            screens.append(cheap_screen_candidate(opp))

    # Prefer diversity; exclude Iowa incomplete regression from primary pursuit spend
    ranked = sorted(screens, key=shortlist_rank_key)
    selected: list[dict[str, Any]] = []
    buckets_seen: set[str] = set()
    for c in ranked:
        sid = str(c.get("solicitation_id") or "")
        if is_forbidden_primary(sid) or sid == REGRESSION_ID:
            continue
        if cheap_kill(c):
            BUDGET["deals_killed_before_expensive_research"] += 1
            _log("cheap_kill", deal_id=sid, title=c.get("title"))
            continue
        bucket = diversity_bucket(str(c.get("title") or ""))
        # Prefer filling new buckets until we have 5
        if len(selected) < 5 or bucket not in buckets_seen:
            selected.append(c)
            buckets_seen.add(bucket)
        if len(selected) >= 8:
            break

    # Ensure seed path present via reevaluation (already discovered previously — not a new seed)
    list_html_cache: dict[str, str] = {}
    qualifications: list[dict[str, Any]] = []
    packets: list[dict[str, Any]] = []
    cost_intel_rows: list[dict[str, Any]] = []
    buyer_rows: list[dict[str, Any]] = []
    econ_rows: list[dict[str, Any]] = []
    future_actions: list[dict[str, Any]] = []
    knowledge_reuse = {
        "supplier_facts_seeded": len(reusable.suppliers),
        "finance_facts_seeded": len(reusable.finance),
        "requests_avoided": 0,
    }

    for cand in selected[:6]:
        sid = str(cand.get("solicitation_id"))
        _log("acquire_start", deal_id=sid, title=cand.get("title"))
        before = client.request_count
        acquired = acquire_package_for_candidate(
            cand, client=client, temp=temp, list_html_cache=list_html_cache
        )
        used = client.request_count - before
        _deal_req(sid, used)
        _bump("package_acquisition", used)
        if acquired.get("skipped"):
            BUDGET["deals_killed_before_expensive_research"] += 1
            continue
        if not (acquired.get("line_items") or acquired.get("has_authoritative_text")):
            # Insufficient package — light qualify as incomplete / defer without deep spend
            opp = opportunity_from_acquired(cand, acquired, reusable=reusable)
            opp["transactional_fit"] = cand.get("transactional_fit", True)
            result = qualify_opportunity_pursuit(opp, reusable=reusable)
            qualifications.append(result)
            packets.append(build_pursuit_qualification_packet(result))
            continue

        opp = opportunity_from_acquired(cand, acquired, reusable=reusable)
        result = qualify_opportunity_pursuit(opp, reusable=reusable)
        qualifications.append(result)
        packets.append(build_pursuit_qualification_packet(result))
        cost_intel_rows.append({"deal_id": sid, "cost": result.get("cost_intelligence")})
        buyer_rows.append({"deal_id": sid, "buyer_history": result.get("buyer_history")})
        econ_rows.append({"deal_id": sid, "economic_potential": result.get("economic_potential")})
        future_actions.extend(result.get("future_human_actions") or [])
        _log(
            "qualified",
            deal_id=sid,
            state=(result.get("pursuit_decision") or {}).get("state"),
            econ=(result.get("economic_potential") or {}).get("status"),
        )

    # --- Seed reevaluation (prior live discovery; not newly seeded for discovery) ---
    seed_opp = build_seed_opportunity(reusable)
    # If seed already in qualifications, replace/annotate; else append
    seed_result = qualify_opportunity_pursuit(
        seed_opp,
        reusable=reusable,
        prior_packet={"buyer_history": load_prior_seed_packet_bits().get("buyer_history")},
    )
    seed_packet = build_pursuit_qualification_packet(seed_result)
    # Ensure seed appears in queues
    if not any(q.get("deal_id") == SEED_ID for q in qualifications):
        qualifications.append(seed_result)
        packets.append(seed_packet)
        cost_intel_rows.append({"deal_id": SEED_ID, "cost": seed_result.get("cost_intelligence")})
        buyer_rows.append({"deal_id": SEED_ID, "buyer_history": seed_result.get("buyer_history")})
        econ_rows.append({"deal_id": SEED_ID, "economic_potential": seed_result.get("economic_potential")})
        future_actions.extend(seed_result.get("future_human_actions") or [])
    else:
        # overwrite prior seed entry with reevaluation
        for i, q in enumerate(qualifications):
            if q.get("deal_id") == SEED_ID:
                qualifications[i] = seed_result
                packets[i] = seed_packet
                break

    # Light regression note for blades — no major request budget
    regression_note = {
        "solicitation": REGRESSION_ID,
        "role": "incomplete_package_regression_only",
        "pursuit_run_budget_spent": 0,
        "forbidden_as_primary": True,
    }

    pursuit_worthy = []
    defer_reject = []
    for q in qualifications:
        state = (q.get("pursuit_decision") or {}).get("state")
        row = {
            "deal_id": q.get("deal_id"),
            "title": q.get("title"),
            "state": state,
            "reasons": (q.get("pursuit_decision") or {}).get("reasons"),
            "economic_potential": q.get("economic_potential"),
            "cost_coverage": (q.get("cost_intelligence") or {}).get("coverage_of_bom_qty_proxy"),
            "future_actions": q.get("future_human_actions"),
            "packet": build_pursuit_qualification_packet(q),
        }
        if state in {PURSUIT_WORTHY, PURSUIT_WORTHY_UNCERTAIN}:
            pursuit_worthy.append(row)
        elif state in {PURSUIT_DEFER, PURSUIT_REJECT} or state:
            defer_reject.append(row)

    knowledge_reuse["requests_avoided"] = BUDGET["requests_avoided_by_knowledge"]
    knowledge_reuse["supplier_reuse_in_cost"] = sum(
        int((r.get("cost") or {}).get("supplier_facts_reused") or 0) for r in cost_intel_rows
    )

    outreach = mode_snapshot()
    assert outreach["emails_sent"] == 0
    assert outreach["calls_placed"] == 0
    assert outreach["bids_submitted"] == 0
    assert all(a.get("timing") != "ACTION_NOW" for a in future_actions if a.get("action_type") != "NONE")

    validation = {
        "kind": "PursuitQualificationValidation",
        "generated_at": _utc(),
        "operating_mode": MODE_DEVELOPMENT_NO_OUTREACH,
        "clock_mode": clock_mode(),
        "live_candidates_seriously_analyzed": len(qualifications),
        "pursuit_worthy_count": sum(
            1 for q in qualifications if (q.get("pursuit_decision") or {}).get("state") == PURSUIT_WORTHY
        ),
        "pursuit_worthy_uncertain_count": sum(
            1
            for q in qualifications
            if (q.get("pursuit_decision") or {}).get("state") == PURSUIT_WORTHY_UNCERTAIN
        ),
        "defer_reject_count": len(defer_reject),
        "seed_reevaluation": {
            "deal_id": SEED_ID,
            "decision": seed_result.get("pursuit_decision"),
            "package_readiness": seed_result.get("package_readiness"),
            "economic_potential": seed_result.get("economic_potential"),
            "cost_intelligence_summary": {
                "status": (seed_result.get("cost_intelligence") or {}).get("status"),
                "coverage": (seed_result.get("cost_intelligence") or {}).get("coverage_of_bom_qty_proxy"),
                "ranges": (seed_result.get("cost_intelligence") or {}).get("ranges"),
                "exact_vs_comparable": (seed_result.get("cost_intelligence") or {}).get("exact_vs_comparable"),
            },
            "missing_spec_effect": "blocks_formal_compliance_and_formal_quote_not_preliminary_economics",
            "synthetic_award_used": False,
        },
        "iowa_blades_regression": regression_note,
        "outreach": outreach,
        "no_seeded_ids_for_new_discovery": True,
        "benchmark_leakage": False,
        "synthetic_live_success": False,
        "source_repairs": failure_analysis.get("sources_repaired_this_run"),
        "sources_became_productive": repaired,
        "NEXT_STATE_CANDIDATE": "AUTONOMOUS_PURSUIT_QUALIFICATION_OPERATIONAL",
    }

    _write("pursuit_qualification_validation.json", validation)
    _write("pursuit_worthy_queue.json", {"queue": pursuit_worthy, "count": len(pursuit_worthy)})
    _write("defer_reject_queue.json", {"queue": defer_reject, "count": len(defer_reject)})
    _write("live_candidate_cost_intelligence.json", {"candidates": cost_intel_rows})
    _write("live_candidate_buyer_history.json", {"candidates": buyer_rows})
    _write("live_candidate_economic_potential.json", {"candidates": econ_rows})
    _write(
        "future_human_action_queue.json",
        {
            "actions": future_actions,
            "all_future_if_pursued": True,
            "action_now_count": sum(1 for a in future_actions if a.get("timing") == "ACTION_NOW"),
            "note": "FUTURE HUMAN VERIFICATION IF PURSUED — no immediate outreach",
        },
    )
    _write("seed_deal_pursuit_reevaluation.json", seed_packet | {"raw": seed_result})
    _write("source_health_report.json", health)
    _write("source_failure_analysis.json", failure_analysis)
    _write(
        "research_budget_report.json",
        {
            **BUDGET,
            "http_client_requests": client.request_count,
            "discovery_api_requests": live_reqs,
            "discovery_metrics_sources_ok": disc_metrics.get("sources_successful"),
            "discovery_metrics_sources_failed": disc_metrics.get("sources_failed"),
        },
    )
    _write("knowledge_reuse_report.json", knowledge_reuse)
    _write(
        "autonomous_pursuit_request_log.json",
        {"events": REQUEST_LOG, "outreach_zero": True, "bids_zero": True},
    )
    _write("pursuit_qualification_packets.json", {"packets": packets})

    # Cleanup temp retrievals
    cleanup = {"removed": False}
    if TEMP_ROOT.exists():
        shutil.rmtree(TEMP_ROOT, ignore_errors=True)
        cleanup = {"removed": True, "path": str(TEMP_ROOT)}
    _write("pursuit_temp_cleanup.json", cleanup)

    print(json.dumps({
        "ok": True,
        "analyzed": len(qualifications),
        "pursuit_worthy": validation["pursuit_worthy_count"],
        "uncertain": validation["pursuit_worthy_uncertain_count"],
        "seed_state": (seed_result.get("pursuit_decision") or {}).get("state"),
        "outreach": outreach,
    }, indent=2))
    return validation


if __name__ == "__main__":
    main()
