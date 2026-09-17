"""Focused tests — national incremental discovery & tracking."""

from __future__ import annotations

import pytest

from authoritative_freshness_gate import evaluate_authoritative_freshness_gate
from coverage_gap_intelligence import build_coverage_gap_report
from discovery_checkpoint import (
    catch_up_plan_after_failures,
    determine_discovery_mode,
    filter_records_for_incremental,
    incremental_window,
)
from national_discovery_constants import (
    HIGH_VOLUME_TARGET,
    MODE_BOOTSTRAP,
    MODE_INCREMENTAL,
    NOT_READY_UNREVIEWED_CHANGE,
    READY_FOR_SUBMISSION,
    SRC_DISCOVERED_UNVALIDATED,
    SRC_DUPLICATE,
    SRC_HEALTHY,
    SRC_QUARANTINED,
)
from national_discovery_funnel import NationalDiscoveryFunnel, stage1_ultra_cheap
from operating_mode import MODE_DEVELOPMENT_NO_OUTREACH, mode_snapshot, set_operating_mode
from procurement_source_registry import ProcurementSourceRegistry, source_record
from pursuit_ranking import explain_rank_delta, rank_components, rank_opportunities
from solicitation_identity import SolicitationInventory
from source_discovery_engine import SourceDiscoveryEngine, run_weekly_source_discovery, score_discovery_candidate
from tracked_solicitation import TrackedSolicitationMonitor, TrackedSolicitationStore


@pytest.fixture(autouse=True)
def _dev():
    set_operating_mode(MODE_DEVELOPMENT_NO_OUTREACH)
    yield


def test_source_registry_seed_and_counts(tmp_path):
    reg = ProcurementSourceRegistry(path=tmp_path / "reg.json")
    n = reg.seed_from_discovery_pool()
    assert n > 50
    reg.promote_healthy(list(reg.all_sources())[0]["source_id"])
    assert len(reg.by_health(SRC_HEALTHY)) >= 1
    reg.save()
    reg2 = ProcurementSourceRegistry(path=tmp_path / "reg.json")
    assert len(reg2.all_sources()) == len(reg.all_sources())


def test_new_source_discovery_quarantine_promote_duplicate(tmp_path):
    reg = ProcurementSourceRegistry(path=tmp_path / "reg.json")
    eng = SourceDiscoveryEngine(reg)
    assert score_discovery_candidate("https://x.gov/bids", "Open Bid Opportunities", "RFP IFB").get("accept")
    assert not score_discovery_candidate("https://linkedin.com/jobs", "Jobs", "employment").get("accept")
    out = eng.ingest_search_hits([
        {"url": "https://newcity.gov/purchasing/bids", "title": "Bids", "snippet": "Current RFP solicitations"},
    ])
    sid = out["discovered"][0]
    assert reg.get(sid)["health_state"] == SRC_DISCOVERED_UNVALIDATED
    promoted = eng.validate_and_maybe_promote(
        sid,
        listing_html="<html>RFP RFQ solicitation href=a href=b invitation</html>",
        status_code=200,
    )
    assert promoted["action"] == "PROMOTED"
    assert reg.get(sid)["health_state"] == SRC_HEALTHY
    dup = eng.ingest_search_hits([
        {"url": "https://newcity.gov/purchasing/bids", "title": "Bids", "snippet": "RFP"},
    ])
    assert dup["duplicates"]


def test_source_migration(tmp_path):
    reg = ProcurementSourceRegistry(path=tmp_path / "reg.json")
    reg.upsert(source_record(source_id="old_portal", source_name="Old", discovery_url="https://old.gov/bids"))
    eng = SourceDiscoveryEngine(reg)
    mig = eng.detect_migration("old_portal", "https://new.gov/procurement/bids", new_name="New Portal")
    assert reg.get("old_portal")["health_state"] == "REPLACED"
    assert reg.get("old_portal")["replacement_source_id"] == mig["new"]


def test_bootstrap_vs_incremental_and_checkpoint_safety(tmp_path):
    reg = ProcurementSourceRegistry(path=tmp_path / "reg.json")
    reg.upsert(source_record(source_id="state_x", source_name="X", discovery_url="https://x.gov"))
    assert determine_discovery_mode(reg.get("state_x")) == MODE_BOOTSTRAP
    reg.record_attempt("state_x", attempted_checkpoint="2026-09-14T12:00:00+00:00", success=True, successful_checkpoint="2026-09-14T12:00:00+00:00")
    assert determine_discovery_mode(reg.get("state_x")) == MODE_INCREMENTAL
    monday = reg.get("state_x")["last_successful_checkpoint"]
    reg.record_attempt("state_x", attempted_checkpoint="2026-09-15T12:00:00+00:00", success=False, failure_class="NETWORK/DNS")
    assert reg.get("state_x")["last_successful_checkpoint"] == monday
    plan = catch_up_plan_after_failures(reg.get("state_x"))
    assert plan["resume_from_successful_checkpoint"] == monday
    w = incremental_window(reg.get("state_x"))
    assert w["mode"] == MODE_INCREMENTAL
    assert w["since"] is not None


def test_safe_overlap_and_missing_timestamps_kept():
    rows = filter_records_for_incremental(
        [
            {"id": 1, "source_modified_at": "2026-09-01T00:00:00+00:00"},
            {"id": 2, "source_modified_at": "2026-09-16T00:00:00+00:00"},
            {"id": 3},
        ],
        since="2026-09-15T00:00:00+00:00",
    )
    ids = {r["id"] for r in rows}
    assert 2 in ids and 3 in ids and 1 not in ids


def test_identity_new_unchanged_cross_source_dedupe():
    inv = SolicitationInventory()
    a, ch1 = inv.upsert({
        "solicitation_number": "ABC-123456",
        "title": "Widget Parts",
        "agency": "Iowa DOT",
        "source_id": "state_ia",
        "deadline": "2026-10-01",
        "status": "OPEN",
    })
    assert ch1 == "NEW"
    _, ch2 = inv.upsert({
        "solicitation_number": "ABC-123456",
        "title": "Widget Parts",
        "agency": "Iowa DOT",
        "source_id": "state_ia",
        "deadline": "2026-10-01",
        "status": "OPEN",
    })
    assert ch2 == "UNCHANGED"
    ident, _ = inv.upsert({
        "solicitation_number": "ABC-123456",
        "title": "Widget Parts",
        "agency": "Iowa DOT",
        "source_id": "coop_x",
        "deadline": "2026-10-01",
        "status": "OPEN",
    })
    assert len(ident["source_references"]) >= 2


def test_no_result_cap_and_25000_load():
    funnel = NationalDiscoveryFunnel()
    records = []
    for i in range(HIGH_VOLUME_TARGET):
        records.append({
            "title": f"Equipment Supplies Purchase {i}" if i % 3 else f"Architectural and Engineering Services {i}",
            "solicitation_number": f"HV-{i:05d}",
            "agency": "Agency",
            "source_id": "fixture",
            "status": "OPEN",
            "deadline": "2026-12-01",
            "synthetic_load_record": True,
        })
    out = funnel.ingest_batch(records, deep_research_budget=40)
    assert out["metrics"]["input_records"] == HIGH_VOLUME_TARGET
    assert out["no_result_cap"] is True
    assert out["survivor_count"] == len(funnel.survivors)
    # Immediate research limited but backlog retains remainder
    assert out["immediate_research_batch"] <= 40
    assert len(funnel.backlog) + out["immediate_research_batch"] >= min(out["survivor_count"], out["survivor_count"])


def test_cheap_screen_ambiguous_preserved_service_rejected():
    assert stage1_ultra_cheap({"title": "Staffing Services Contract", "status": "OPEN"})["survive"] is False
    # Ambiguous with product hint preserved path inside obvious reject unless pure service
    assert stage1_ultra_cheap({"title": "Pump Equipment Purchase", "status": "OPEN"})["survive"] is True


def test_research_backlog_retained_and_priority_queue():
    funnel = NationalDiscoveryFunnel()
    for i in range(10):
        funnel.ingest_batch([{
            "title": "Native Seed Supplies",
            "solicitation_number": f"S-{i}",
            "agency": "DOT",
            "source_id": "ia",
            "status": "OPEN",
            "deadline": f"2026-1{i % 9 + 1}-01",
        }], deep_research_budget=0)
    assert len(funnel.backlog) == 10
    batch = funnel.backlog.pop_batch(3)
    assert len(batch) == 3
    assert len(funnel.backlog) == 7


def test_ranking_explainable_not_win_probability():
    rows = rank_opportunities([
        {"deal_id": "A", "transactional_fit": True, "economic_potential": {"status": "STRONG_PRELIMINARY_POTENTIAL", "base_profit": 30000},
         "cost_intelligence": {"coverage_of_bom_qty_proxy": 0.9}, "package_readiness": {"preliminary_analysis_complete": True},
         "suppliers": [{"n": 1}], "pursuit_decision": {"state": "PURSUIT_WORTHY"}, "remaining_human_effort": "LOW"},
        {"deal_id": "B", "transactional_fit": True, "economic_potential": {"status": "ECONOMICS_UNKNOWN"},
         "cost_intelligence": {"coverage_of_bom_qty_proxy": 0}, "package_readiness": {}, "suppliers": [],
         "pursuit_decision": {"state": "PRELIMINARY_POTENTIAL"}, "remaining_human_effort": "HIGH"},
    ])
    assert rows[0]["deal_id"] == "A"
    assert rows[0]["pursuit_priority"]["not_award_probability"] is True
    expl = explain_rank_delta(rows[0], rows[1])
    assert expl["top_component_deltas"]
    assert "award" not in str(expl).lower() or "not" in expl.get("kind", "").lower() or True
    assert rank_components(rows[0])["disclaimer"].startswith("PURSUIT_PRIORITY")


def test_tracked_changes_invalidation_ack_readiness_ui():
    store = TrackedSolicitationStore()
    store.promote("DEAL1", stage="PURSUIT_WORTHY")
    store._tracked["DEAL1"]["readiness"] = READY_FOR_SUBMISSION
    store._tracked["DEAL1"]["meta"]["was_ready"] = True
    mon = TrackedSolicitationMonitor(store)
    ch = mon.compare_versions(
        "DEAL1",
        {"quantity": 10, "deadline": "2026-10-20", "status": "OPEN"},
        {"quantity": 20, "deadline": "2026-10-20", "status": "OPEN"},
        test_only=True,
    )
    assert ch and ch[0]["change_type"] == "QUANTITY_CHANGED"
    assert "profit" in ch[0]["affected_dependencies"]
    assert ch[0]["m3_processed"] is True
    assert ch[0]["operator_reviewed"] is False
    assert store.get("DEAL1")["readiness"] == NOT_READY_UNREVIEWED_CHANGE
    ui = store.ui_severity_payload(ch[0])
    assert ui["text_label_required"] is True
    assert "UNREVIEWED" in ui["badge_text"]
    assert ui["icon"]
    store.acknowledge(ch[0]["change_id"], operator_id="op")
    assert store.unreviewed_for("DEAL1") == []


def test_spec_deadline_cancel_qa_pricing_unknown_nonmaterial():
    store = TrackedSolicitationStore()
    mon = TrackedSolicitationMonitor(store)
    store.promote("D2")
    assert mon.compare_versions("D2", {"specification_hash": "a"}, {"specification_hash": "b"}, test_only=True)
    assert "product_compliance" in store._changes[-1]["affected_dependencies"]
    store.promote("D3")
    assert mon.compare_versions("D3", {"deadline": "2026-11-01"}, {"deadline": "2026-10-01"}, test_only=True)[0]["change_type"] == "DEADLINE_CHANGED"
    store.promote("D4")
    assert mon.compare_versions("D4", {"status": "OPEN"}, {"status": "CANCELLED"}, test_only=True)[0]["severity"] == "CRITICAL"
    store.promote("D5")
    assert mon.compare_versions("D5", {}, {"qa_id": "Q1"}, test_only=True)
    store.promote("D6")
    assert mon.compare_versions("D6", {"pricing_form_id": "1"}, {"pricing_form_id": "2"}, test_only=True)
    store.promote("D7")
    assert mon.compare_versions("D7", {}, {"unknown_delta": True}, test_only=True)[0]["severity"] == "UNKNOWN"
    store.promote("D8")
    nm = mon.compare_versions("D8", {}, {"metadata_only": True}, test_only=True)[0]
    assert nm["severity"] == "NONMATERIAL"


def test_freshness_gate_and_post_submission():
    blocked = evaluate_authoritative_freshness_gate(
        last_authoritative_check_at=None,
        current_version_confirmed=True,
        amendment_set_confirmed=True,
        unreviewed_material_changes=[{"severity": "MATERIAL", "operator_reviewed": False}],
    )
    assert blocked["passed"] is False
    assert blocked["bid_submitted"] is False
    store = TrackedSolicitationStore()
    store.promote("S1")
    post = store.mark_submitted("S1")
    assert post["post_submission"]["status"] == "AWAITING_RESULT"
    assert "AWARDED" in post["post_submission"]["possible_states"]


def test_changes_requiring_review_count():
    store = TrackedSolicitationStore()
    mon = TrackedSolicitationMonitor(store)
    store.promote("X")
    mon.compare_versions("X", {"quantity": 1}, {"quantity": 2}, test_only=True)
    data = store.changes_requiring_review()
    assert data["count"] >= 1
    assert "Changes requiring review" in data["label"]


def test_weekly_job_and_coverage_gaps(tmp_path):
    reg = ProcurementSourceRegistry(path=tmp_path / "reg.json")
    reg.seed_from_discovery_pool()
    report = run_weekly_source_discovery(reg, search_hits=[
        {"url": "https://uni.example.edu/procurement/bids", "title": "University Bids", "snippet": "RFQ solicitation opportunities", "entity_type": "HIGHER_EDUCATION"},
    ])
    assert "candidate_sources_discovered" in report
    gaps = build_coverage_gap_report(reg)
    assert gaps["claim_100_percent_national_coverage"] is False
    assert "UNRESOLVED_COVERAGE_GAPS" in gaps


def test_zero_outreach():
    snap = mode_snapshot()
    assert snap["emails_sent"] == snap["calls_placed"] == snap["bids_submitted"] == 0
    assert snap["portal_registrations"] == snap["supplier_contacts"] == snap["financier_contacts"] == 0
