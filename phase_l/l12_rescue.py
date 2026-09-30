"""Phase L.12 — auth-walled award recovery + buyer-specific history paths (no accounts)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.acquisition_lanes import (
    DEEP_RESEARCH_NO_FIXED_COUNT,
    MANUAL_QUEUE_NO_FIXED_CAP,
    STAGE3_NO_ROW_CAP,
    classify_acquisition_lane,
)
from phase_l.auth_access import (
    BUILD,
    BUYER_SPECIFIC_ACCOUNT_REQUIRED,
    FREE_REGISTRATION_REQUIRED,
    HISTORY_NOT_AVAILABLE,
    NO_PUBLIC_HISTORY_FEATURE,
    PLATFORM_HISTORY_BLOCKED,
    PRIVATE_RESTRICTED,
    PUBLIC_ANTI_BOT_BLOCKED,
    VENDOR_ACCOUNT_REQUIRED,
)
from phase_l.auth_history_recovery import (
    build_registration_priorities,
    run_auth_walled_history_recovery,
)
from phase_l.buyer_history_paths import all_buyer_history_paths, update_platform_memory
from phase_l.buyer_registry import (
    build_registry_from_rows,
    buyer_type_coverage_report,
    category_coverage_report,
    state_coverage_report,
)
from phase_l.economic_evaluability import recompute_economics_from_recovery
from phase_l.evidence_recovery import run_parallel_recovery
from phase_l.exact_history_recovery import (
    GOV_UPGRADED_A,
    GOV_UPGRADED_B,
    GOV_UPGRADED_C,
    HISTORY_AUTH_REQUIRED,
    HISTORY_BUYER_RECORDS_NOT_FOUND,
    HISTORY_EVIDENCE_EXHAUSTED,
    HISTORY_SOURCE_BLOCKED,
    run_exact_history_recovery,
)
from phase_l.legacy_cleanup import assert_canonical_caps, assert_no_fixed_positive_cap, legacy_cleanup_report
from phase_l.l11_rescue import seed_history_graphs_from_artifacts
from phase_l.original_solicitation import resolve_original_solicitation
from phase_l.platform_history import detect_platform, platform_history_inventory, weak_platform_audit
from phase_l.progressive_funnel import run_progressive_stages_cheap
from phase_l.quality_audit import (
    GOV_VALUE_A,
    GOV_VALUE_B,
    GOV_VALUE_C,
    GOV_VALUE_D,
    RECON_ONLY_CATEGORY_BENCHMARK,
    SECONDARY_QUOTE_TARGET,
    SUPPLIER_A,
    SUPPLIER_B,
    VALIDATED_QUOTE_TARGET,
    audit_quote_positive,
)
from phase_l.quote_economics import BUYER_VALUE_PATH, SUPPLIER_MEMORY_PATH, load_json, save_json
from phase_l.resilient_hunt import HUNT_COMPLETE, HUNT_COMPLETE_WITH_FAILURES, reset_checkpoint
from phase_l.supplier_upgrade import run_supplier_upgrade_loop

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)
PRIOR_AWARDEES_PATH = ROOT / "data" / "phase_l12_prior_awardees.json"

L11_BASELINE = {
    "gov_d": 91,
    "gov_a_upgrades": 0,
    "gov_b_upgrades": 0,
    "gov_c_upgrades": 1,
    "validated": 2,
    "secondary": 2,
    "recon_only": 3,
}


def _utc() -> str:
    return now_utc().isoformat()


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _deadline_days(row: dict[str, Any], pipe: dict[str, Any]) -> float | None:
    d = (pipe.get("stage0") or {}).get("deadline") or {}
    for k in ("days_remaining", "days_to_deadline", "deadline_days"):
        v = _f(d.get(k) or row.get(k))
        if v is not None:
            return v
    return None


def _persist_prior_awardee(entry: dict[str, Any]) -> None:
    data = {"build": BUILD, "awardees": []}
    if PRIOR_AWARDEES_PATH.exists():
        try:
            data = json.loads(PRIOR_AWARDEES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    rows = list(data.get("awardees") or [])
    key = (
        str(entry.get("buyer") or ""),
        str(entry.get("vendor") or ""),
        str(entry.get("product") or "")[:40],
        str(entry.get("award_date") or ""),
    )
    existing = {
        (
            str(r.get("buyer") or ""),
            str(r.get("vendor") or ""),
            str(r.get("product") or "")[:40],
            str(r.get("award_date") or ""),
        )
        for r in rows
        if isinstance(r, dict)
    }
    if key not in existing:
        rows.append(entry)
    data["awardees"] = rows[-500:]
    data["updated_at"] = _utc()
    data["build"] = BUILD
    PRIOR_AWARDEES_PATH.parent.mkdir(parents=True, exist_ok=True)
    PRIOR_AWARDEES_PATH.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def build_history_access_gap_queue(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """HISTORY_ACCESS_GAP_QUEUE — no cap."""
    queue: list[dict[str, Any]] = []
    for r in rows:
        score = float(r.get("likely_value_if_unlocked") or 0) / 1000.0
        score += 5 if r.get("registration_required") else 0
        score += {"GOV_VALUE_D": 3, "GOV_VALUE_C": 1}.get(str(r.get("current_gov_grade") or ""), 0)
        dd = _f(r.get("deadline")) or 0
        score += min(dd, 30) * 0.2
        queue.append({**r, "score": round(score, 2)})
    queue.sort(key=lambda x: -x["score"])
    return queue


def build_public_history_recovery_queue(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PUBLIC_HISTORY_RECOVERY_QUEUE — public evidence likely, still needs exact recovery."""
    queue: list[dict[str, Any]] = []
    for r in rows:
        identity = 10 if r.get("product_identity_quality") else 0
        supplier = {"SUPPLIER_A": 8, "SUPPLIER_B": 5, "SUPPLIER_C": 2}.get(str(r.get("supplier_grade") or ""), 0)
        econ = float(r.get("apparent_economics") or 0) / 1000.0
        recur = 3 if r.get("buyer_recurrence") else 0
        dd = min(_f(r.get("deadline")) or 0, 30) * 0.2
        avail = 5 if r.get("evidence_availability") else 0
        score = identity + supplier + econ + recur + dd + avail
        queue.append({**r, "score": round(score, 2)})
    queue.sort(key=lambda x: -x["score"])
    return queue


def run_phase_l12_auth_history(
    rows: list[dict[str, Any]] | None = None,
    *,
    authorize_live: bool = True,
    refresh_hunt: bool = False,
    max_hunt_sources: int = 20,
    reset_hunt_checkpoint: bool = False,
    history_live_fetches: int = 2,
) -> dict[str, Any]:
    assert_canonical_caps()
    assert_no_fixed_positive_cap()
    assert STAGE3_NO_ROW_CAP and DEEP_RESEARCH_NO_FIXED_COUNT and MANUAL_QUEUE_NO_FIXED_CAP

    seed_stats = seed_history_graphs_from_artifacts()
    discovery_meta: dict[str, Any] = {}
    hunt_status = None

    if refresh_hunt and authorize_live:
        if reset_hunt_checkpoint:
            reset_checkpoint()
        try:
            from phase_l.hunt import run_phase_l_hunt

            hunt = run_phase_l_hunt(
                authorize_live=True,
                max_sources=max_hunt_sources,
                profile="commercial_feed",
            )
            discovery_meta = hunt.get("discovery_meta") or {}
            hunt_status = (discovery_meta.get("live_runner") or {}).get("run_status")
            rows = list(
                (json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))).get("rows") or []
            )
        except Exception as exc:
            discovery_meta = {"hunt_error": str(exc)[:300]}
            hunt_status = "FAILED"

    if rows is None:
        data = json.loads((OUT / "accessible_latest.json").read_text(encoding="utf-8"))
        rows = list(data.get("rows") or [])

    buyer_memory = load_json(BUYER_VALUE_PATH)
    supplier_memory = load_json(SUPPLIER_MEMORY_PATH)
    build_registry_from_rows(rows)

    access_yes = [r for r in rows if str(r.get("our_bid_access") or "") == "YES"]
    stage3: list[dict[str, Any]] = []
    stage_counts = Counter()
    for i, row in enumerate(access_yes):
        if i and i % 300 == 0:
            print(f"[l12] stage-scan {i}/{len(access_yes)} s3={len(stage3)}", flush=True)
        pipe = run_progressive_stages_cheap(row)
        if (pipe.get("stage1") or {}).get("pass"):
            stage_counts["stage1"] += 1
        if (pipe.get("stage2") or {}).get("pass"):
            stage_counts["stage2"] += 1
        if pipe.get("survives_to_stage3"):
            stage3.append({"row": row, "pipe": pipe})
    stage_counts["stage3"] = len(stage3)
    print(f"[l12] Stage 3={len(stage3)} — auth-walled buyer-pivot recovery (no cap)...", flush=True)

    gov_grades = Counter()
    supplier_grades = Counter()
    quality_states = Counter()
    history_outcomes = Counter()
    access_modes = Counter()
    recovery_sources = Counter()
    supplier_upgrade_moves = Counter()
    competition_bidder_counts = 0
    competition_distributions = 0

    validated: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []
    recon_only: list[dict[str, Any]] = []
    upgrade_audits: list[dict[str, Any]] = []
    gap_rows: list[dict[str, Any]] = []
    public_queue_rows: list[dict[str, Any]] = []
    reg_opps: list[dict[str, Any]] = []
    prior_awardees: list[dict[str, Any]] = []
    supplier_upgrade_rows: list[dict[str, Any]] = []

    old_corpus_upgrades = Counter()
    fresh_corpus_upgrades = Counter()
    starting_d = 0
    to_a = to_b = to_c = 0

    # Track Stage-3 IDs as "fresh" relative to prior Gov-D artifact corpus
    prior_gov_d_ids: set[str] = set()
    for path in (OUT / "l11_gov_d_history_upgrade.json", OUT / "l10_gov_d_upgrade.json", OUT / "l10_recon_only.json"):
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        plist = raw if isinstance(raw, list) else (raw.get("rows") or [])
        for g in plist:
            if isinstance(g, dict) and g.get("opportunity_id"):
                prior_gov_d_ids.add(str(g["opportunity_id"]))
        if prior_gov_d_ids:
            break

    for i, item in enumerate(stage3):
        row = item["row"]
        pipe = item["pipe"]
        screen = pipe.get("stage2_screen") or {}
        commercial = dict(
            screen.get("commercial_identity") or ((pipe.get("stage2") or {}).get("commercial") or {})
        )
        s3 = pipe.get("stage3") or {}
        oid = str(row.get("notice_id") or row.get("solicitation_id") or row.get("id") or i)
        platform = detect_platform(row)
        is_old_corpus = oid in prior_gov_d_ids

        lane = classify_acquisition_lane(row, commercial=commercial).get("acquisition_lane")
        original = resolve_original_solicitation(row)
        recovery = run_parallel_recovery(
            row,
            commercial=commercial,
            history={},
            buyer_memory=buyer_memory,
            supplier_memory=supplier_memory,
            stage3=s3,
        )
        econ = recompute_economics_from_recovery(
            row,
            gov_rec=recovery.get("gov"),
            qty_rec=recovery.get("quantity"),
            supplier_rec=recovery.get("suppliers"),
        )

        pre_audit = audit_quote_positive(
            row,
            commercial=commercial,
            gov=econ.get("government_value"),
            suppliers=econ.get("suppliers"),
            qty_info=econ.get("quantity") or recovery.get("quantity"),
            max_buy=econ.get("max_buy"),
            qdep=econ.get("quote_dependent"),
            freight=econ.get("freight"),
            original=original,
            lane=lane,
            deadline_days=_deadline_days(row, pipe),
            buyer_memory=buyer_memory,
            supplier_memory=supplier_memory,
            stage3=s3,
            attempt_upgrades=False,
        )
        pre_gov = pre_audit.get("gov_grade")
        pre_sup = pre_audit.get("supplier_grade")

        hist_rec = None
        auth_rec = None
        if pre_gov == GOV_VALUE_D or (
            "BENCHMARK" in str((econ.get("government_value") or {}).get("source") or "").upper()
        ):
            starting_d += 1
            if i % 20 == 0:
                print(f"[l12] history {i+1}/{len(stage3)} {oid} {(row.get('title') or '')[:40]}", flush=True)

            # L.11 exact path first (memory/graphs)
            hist_rec = run_exact_history_recovery(
                row,
                commercial=commercial,
                history={},
                buyer_memory=buyer_memory,
                authorize_live=False,
                max_live_fetches=0,
            )

            # Always run L.12 buyer-pivot when still D / platform-blocked
            still_d = hist_rec.get("grade_after") not in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}
            platform_blocked = hist_rec.get("outcome") in {
                HISTORY_AUTH_REQUIRED,
                HISTORY_SOURCE_BLOCKED,
                HISTORY_BUYER_RECORDS_NOT_FOUND,
                HISTORY_EVIDENCE_EXHAUSTED,
            } or "bidnet" in platform.lower() or still_d

            if still_d or platform_blocked:
                auth_rec = run_auth_walled_history_recovery(
                    row,
                    commercial=commercial,
                    buyer_memory=buyer_memory,
                    authorize_live=authorize_live and history_live_fetches > 0,
                    max_live_fetches=history_live_fetches,
                    platform_blocked=True,
                )
                # Prefer auth recovery if it upgrades; else keep L.11
                if auth_rec.get("grade_after") in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
                    hist_rec = {
                        **hist_rec,
                        "outcome": auth_rec["outcome"],
                        "grade_after": auth_rec["grade_after"],
                        "gov": auth_rec.get("gov"),
                        "rule_id": auth_rec.get("rule_id"),
                        "vendor_intel": auth_rec.get("vendor_intel"),
                        "awards_found": auth_rec.get("awards_found"),
                        "attempts": (hist_rec.get("attempts") or []) + (auth_rec.get("attempts") or []),
                        "auth_class": auth_rec.get("access_mode"),
                        "recovery_source": auth_rec.get("recovery_source"),
                        "competition": auth_rec.get("competition"),
                        "registration_opportunity": auth_rec.get("registration_opportunity"),
                        "platform_state": auth_rec.get("platform_state"),
                    }
                else:
                    hist_rec["auth_class"] = auth_rec.get("access_mode")
                    hist_rec["platform_state"] = auth_rec.get("platform_state")
                    hist_rec["registration_opportunity"] = auth_rec.get("registration_opportunity")
                    hist_rec["recovery_source"] = auth_rec.get("recovery_source")
                    hist_rec["attempts"] = (hist_rec.get("attempts") or []) + (auth_rec.get("attempts") or [])
                    if auth_rec.get("outcome") and still_d:
                        # Preserve PLATFORM_HISTORY_BLOCKED semantics vs HISTORY_NOT_AVAILABLE
                        if auth_rec.get("platform_state") == PLATFORM_HISTORY_BLOCKED:
                            hist_rec["outcome"] = auth_rec["outcome"]
                            hist_rec["platform_state"] = PLATFORM_HISTORY_BLOCKED

            history_outcomes[hist_rec["outcome"]] += 1
            am = (auth_rec or {}).get("access_mode") or hist_rec.get("auth_class")
            if am:
                access_modes[am] += 1
            src = hist_rec.get("recovery_source") or (auth_rec or {}).get("recovery_source")
            if src:
                recovery_sources[src] += 1

            if hist_rec.get("gov") and hist_rec.get("grade_after") in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
                econ = recompute_economics_from_recovery(
                    row,
                    gov_rec={**hist_rec["gov"], "recovered": True},
                    qty_rec=recovery.get("quantity"),
                    supplier_rec=recovery.get("suppliers"),
                )
                g = hist_rec["grade_after"]
                if g == GOV_VALUE_A:
                    to_a += 1
                elif g == GOV_VALUE_B:
                    to_b += 1
                else:
                    to_c += 1
                bucket = old_corpus_upgrades if is_old_corpus else fresh_corpus_upgrades
                bucket[g] += 1

            # Prior awardee → supplier
            pre_sup_letter = str(pre_sup or "")
            if hist_rec.get("vendor_intel"):
                sloop = run_supplier_upgrade_loop(
                    row,
                    commercial=commercial,
                    history={
                        "awardee": hist_rec["vendor_intel"].get("vendor"),
                        "vendor_role": hist_rec["vendor_intel"].get("role"),
                    },
                    supplier_memory=supplier_memory,
                    suppliers=econ.get("suppliers"),
                )
                if sloop.get("upgraded"):
                    after_sup = str(sloop.get("grade_after") or "")
                    move = f"{pre_sup_letter}→{after_sup}"
                    supplier_upgrade_moves[move] += 1
                    econ["suppliers"] = sloop.get("suppliers") or econ.get("suppliers")
                    supplier_upgrade_rows.append(
                        {
                            "opportunity_id": oid,
                            "vendor": hist_rec["vendor_intel"].get("vendor"),
                            "role": hist_rec["vendor_intel"].get("role"),
                            "before": pre_sup_letter,
                            "after": after_sup,
                        }
                    )
                va = {
                    "buyer": row.get("agency"),
                    "product": (row.get("title") or "")[:120],
                    "vendor": hist_rec["vendor_intel"].get("vendor"),
                    "award_date": (hist_rec.get("gov") or {}).get("date"),
                    "price": (hist_rec.get("gov") or {}).get("unit_value"),
                    "quantity": (hist_rec.get("gov") or {}).get("quantity"),
                    "contract": row.get("solicitation_id") or oid,
                    "repeated_wins": False,
                }
                prior_awardees.append(va)
                _persist_prior_awardee(va)

            if hist_rec.get("competition"):
                competition_bidder_counts += 1
                if hist_rec["competition"].get("bidder_prices_public"):
                    competition_distributions += 1

            upgrade_audits.append(
                {
                    "opportunity_id": oid,
                    "buyer": row.get("agency"),
                    "solicitation_number": row.get("solicitation_id") or oid,
                    "product": (row.get("title") or "")[:160],
                    "original_gov_grade": GOV_VALUE_D,
                    "buyer_paths_attempted": [
                        a.get("step") for a in (hist_rec.get("attempts") or []) if isinstance(a, dict)
                    ][:20],
                    "public_documents_found": hist_rec.get("awards_found") or 0,
                    "auth_barriers": am,
                    "exact_history_found": hist_rec.get("grade_after") in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C},
                    "new_gov_grade": hist_rec.get("grade_after"),
                    "award_vendor": (hist_rec.get("vendor_intel") or {}).get("vendor"),
                    "supplier_upgrade": bool(hist_rec.get("vendor_intel") and supplier_upgrade_rows and supplier_upgrade_rows[-1].get("opportunity_id") == oid),
                    "economic_state_change": bool(hist_rec.get("gov")),
                    "recovery_source": src,
                    "platform": platform,
                    "platform_state": hist_rec.get("platform_state") or PLATFORM_HISTORY_BLOCKED,
                    "outcome": hist_rec.get("outcome"),
                    "corpus": "old" if is_old_corpus else "fresh",
                    "final_status": hist_rec.get("outcome"),
                }
            )

            # Gap vs public recovery queues
            reg = hist_rec.get("registration_opportunity") or (auth_rec or {}).get("registration_opportunity")
            if reg:
                reg_opps.append(reg)

            apparent = _f((econ.get("quote_dependent") or {}).get("expected_profit")) or 0.0
            reg_required = am in {
                FREE_REGISTRATION_REQUIRED,
                VENDOR_ACCOUNT_REQUIRED,
                BUYER_SPECIFIC_ACCOUNT_REQUIRED,
                PUBLIC_ANTI_BOT_BLOCKED,
            }
            if hist_rec.get("grade_after") == GOV_VALUE_D and reg_required:
                gap_rows.append(
                    {
                        "opportunity": oid,
                        "buyer": row.get("agency"),
                        "platform": platform,
                        "blocked_evidence": "award_history",
                        "access_mode": am,
                        "registration_required": am
                        in {FREE_REGISTRATION_REQUIRED, VENDOR_ACCOUNT_REQUIRED, BUYER_SPECIFIC_ACCOUNT_REQUIRED},
                        "likely_value_if_unlocked": apparent,
                        "current_gov_grade": GOV_VALUE_D,
                        "supplier_grade": pre_sup,
                        "current_quote_status": pre_audit.get("quality_state"),
                        "deadline": _deadline_days(row, pipe),
                        "recommended_owner_action": (
                            "owner_free_registration"
                            if am == FREE_REGISTRATION_REQUIRED
                            else "manual_authenticated_history"
                            if am
                            in {VENDOR_ACCOUNT_REQUIRED, BUYER_SPECIFIC_ACCOUNT_REQUIRED, PUBLIC_ANTI_BOT_BLOCKED}
                            else "defer"
                        ),
                    }
                )
            elif hist_rec.get("grade_after") == GOV_VALUE_D and am not in {
                PRIVATE_RESTRICTED,
                NO_PUBLIC_HISTORY_FEATURE,
                HISTORY_NOT_AVAILABLE,
            }:
                public_queue_rows.append(
                    {
                        "opportunity": oid,
                        "buyer": row.get("agency"),
                        "platform": platform,
                        "product_identity_quality": bool(commercial.get("model") or commercial.get("mpn")),
                        "supplier_grade": pre_sup,
                        "apparent_economics": apparent,
                        "buyer_recurrence": False,
                        "deadline": _deadline_days(row, pipe),
                        "evidence_availability": True,
                        "current_gov_grade": GOV_VALUE_D,
                    }
                )

            update_platform_memory(
                platform,
                access_mode=am,
                anti_bot=am == PUBLIC_ANTI_BOT_BLOCKED,
                public_award_access=hist_rec.get("grade_after") in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C},
                buyer_pivot_success=hist_rec.get("grade_after") in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C},
            )

        audit = audit_quote_positive(
            row,
            commercial=commercial,
            gov=econ.get("government_value") or (hist_rec or {}).get("gov"),
            suppliers=econ.get("suppliers"),
            qty_info=econ.get("quantity") or recovery.get("quantity"),
            max_buy=econ.get("max_buy"),
            qdep=econ.get("quote_dependent"),
            freight=econ.get("freight"),
            original=original,
            lane=lane,
            deadline_days=_deadline_days(row, pipe),
            buyer_memory=buyer_memory,
            supplier_memory=supplier_memory,
            stage3=s3,
            attempt_upgrades=True,
        )
        gov_grades[audit["gov_grade"]] += 1
        supplier_grades[str(audit.get("supplier_grade"))] += 1
        quality_states[audit["quality_state"]] += 1

        entry = {
            "opportunity_id": oid,
            "buyer": row.get("agency"),
            "product": (row.get("title") or "")[:160],
            "gov_grade": audit["gov_grade"],
            "supplier_grade": audit.get("supplier_grade"),
            "quality_state": audit["quality_state"],
            "history_outcome": (hist_rec or {}).get("outcome"),
            "access_mode": (hist_rec or {}).get("auth_class"),
            "cav": (audit.get("cav") or {}).get("ConfidenceAdjustedOpportunityValue"),
            "send_authorized": False,
        }
        if audit["quality_state"] == VALIDATED_QUOTE_TARGET:
            validated.append(entry)
        elif audit["quality_state"] == SECONDARY_QUOTE_TARGET:
            secondary.append(entry)
        elif audit["quality_state"] in {RECON_ONLY_CATEGORY_BENCHMARK, "RECON_ONLY_SUPPLIER_SEED"}:
            recon_only.append(entry)

    # Replay prior Gov-D artifact rows not in Stage 3
    seen = {r["opportunity_id"] for r in upgrade_audits}
    for path in (OUT / "l11_gov_d_history_upgrade.json", OUT / "l10_gov_d_upgrade.json"):
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        prior = raw if isinstance(raw, list) else (raw.get("rows") or [])
        for g in prior:
            oid = str(g.get("opportunity_id") or "")
            if not oid or oid in seen:
                continue
            thin = {
                "notice_id": oid,
                "solicitation_id": g.get("solicitation_number") or oid,
                "agency": g.get("buyer"),
                "title": g.get("product") or g.get("title"),
                "our_bid_access": "YES",
                "source": g.get("platform") or "BidNet",
            }
            starting_d += 1
            auth_rec = run_auth_walled_history_recovery(
                thin,
                commercial={},
                buyer_memory=buyer_memory,
                authorize_live=False,
                max_live_fetches=0,
                platform_blocked=True,
            )
            history_outcomes[auth_rec["outcome"]] += 1
            access_modes[auth_rec.get("access_mode") or "UNKNOWN"] += 1
            if auth_rec.get("grade_after") == GOV_VALUE_A:
                to_a += 1
                old_corpus_upgrades[GOV_VALUE_A] += 1
            elif auth_rec.get("grade_after") == GOV_VALUE_B:
                to_b += 1
                old_corpus_upgrades[GOV_VALUE_B] += 1
            elif auth_rec.get("grade_after") == GOV_VALUE_C:
                to_c += 1
                old_corpus_upgrades[GOV_VALUE_C] += 1
            if auth_rec.get("registration_opportunity"):
                reg_opps.append(auth_rec["registration_opportunity"])
            upgrade_audits.append(
                {
                    "opportunity_id": oid,
                    "buyer": thin.get("agency"),
                    "solicitation_number": thin.get("solicitation_id"),
                    "product": (thin.get("title") or "")[:160],
                    "original_gov_grade": GOV_VALUE_D,
                    "buyer_paths_attempted": [a.get("step") for a in (auth_rec.get("attempts") or [])][:20],
                    "public_documents_found": auth_rec.get("awards_found") or 0,
                    "auth_barriers": auth_rec.get("access_mode"),
                    "exact_history_found": auth_rec.get("grade_after") in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C},
                    "new_gov_grade": auth_rec.get("grade_after"),
                    "award_vendor": (auth_rec.get("vendor_intel") or {}).get("vendor"),
                    "supplier_upgrade": False,
                    "economic_state_change": bool(auth_rec.get("gov")),
                    "recovery_source": auth_rec.get("recovery_source"),
                    "platform": auth_rec.get("platform"),
                    "platform_state": auth_rec.get("platform_state"),
                    "outcome": auth_rec.get("outcome"),
                    "corpus": "old_artifact_replay",
                    "final_status": auth_rec.get("outcome"),
                }
            )
            if auth_rec.get("grade_after") == GOV_VALUE_D:
                gap_rows.append(
                    {
                        "opportunity": oid,
                        "buyer": thin.get("agency"),
                        "platform": auth_rec.get("platform"),
                        "blocked_evidence": "award_history",
                        "access_mode": auth_rec.get("access_mode"),
                        "registration_required": auth_rec.get("access_mode")
                        in {FREE_REGISTRATION_REQUIRED, VENDOR_ACCOUNT_REQUIRED, BUYER_SPECIFIC_ACCOUNT_REQUIRED},
                        "likely_value_if_unlocked": 0,
                        "current_gov_grade": GOV_VALUE_D,
                        "supplier_grade": None,
                        "current_quote_status": "RECON_ONLY",
                        "deadline": None,
                        "recommended_owner_action": "owner_free_registration"
                        if auth_rec.get("access_mode") == FREE_REGISTRATION_REQUIRED
                        else "manual_authenticated_history",
                    }
                )
            seen.add(oid)
        break

    remain_d = max(0, starting_d - to_a - to_b - to_c)
    auth_queue = build_history_access_gap_queue(gap_rows)
    public_queue = build_public_history_recovery_queue(public_queue_rows)
    reg_priorities = build_registration_priorities(reg_opps)
    weak_plat = weak_platform_audit()

    # Hunt-level BidNet / network anti-bot → owner registration roadmap even when
    # current accessible corpus is SAM-dominated after failed commercial fetches.
    hunt_failed = list((discovery_meta.get("live_runner") or {}).get("failed_sources") or [])
    if not hunt_failed:
        for path in (OUT / "l12_fresh_hunt.json", OUT / "l11_fresh_hunt.json"):
            if not path.exists():
                continue
            try:
                prior_hunt = json.loads(path.read_text(encoding="utf-8"))
                hunt_failed = list(
                    ((prior_hunt.get("discovery_meta") or {}).get("live_runner") or {}).get("failed_sources")
                    or prior_hunt.get("failed_sources")
                    or []
                )
                if hunt_failed:
                    break
            except Exception:
                continue
    bidnet_fails = [s for s in hunt_failed if "bidnet" in str(s).lower()]
    if bidnet_fails or any("bidnet" in str(w.get("platform") or "").lower() for w in (weak_plat or [])):
        if not any(r.get("platform") == "BidNet" for r in reg_priorities):
            from phase_l.auth_history_recovery import registration_opportunity, history_access_registration_priority

            synthetic = registration_opportunity(
                platform="BidNet",
                buyer="(network — multiple state/local buyers)",
                access_mode=FREE_REGISTRATION_REQUIRED,
                registration_url="https://www.bidnetdirect.com/",
                blocked_opportunities=max(len(bidnet_fails), len(auth_queue), 1),
                potential_value=sum(float(x.get("likely_value_if_unlocked") or 0) for x in auth_queue),
                buyers_on_platform=max(len(bidnet_fails), 20),
            )
            synthetic["priority_score"] = history_access_registration_priority(synthetic)
            synthetic["recommendation"] = "owner_register_if_high_priority"
            synthetic["hunt_sources_failed"] = bidnet_fails
            synthetic["note"] = (
                "Commercial BidNet feeds failed/anti-bot in fresh hunt; "
                "free vendor registration is the highest-leverage unlock for state/local award history"
            )
            reg_priorities = build_registration_priorities([synthetic] + reg_opps)
            update_platform_memory(
                "BidNet",
                access_mode=PUBLIC_ANTI_BOT_BLOCKED,
                anti_bot=True,
                registration_leverage=synthetic["number_of_current_blocked_opportunities"],
            )

    # Enrich registration priorities with platform leverage counts
    plat_buyers: dict[str, set[str]] = {}
    for r in gap_rows:
        plat_buyers.setdefault(str(r.get("platform") or ""), set()).add(str(r.get("buyer") or ""))
    for rec in reg_priorities:
        plat = rec["platform"]
        buyers_n = len(plat_buyers.get(plat) or set()) or int(rec.get("buyers_unlocked") or 0)
        rec["buyers_unlocked"] = max(int(rec.get("buyers_unlocked") or 0), buyers_n)
        update_platform_memory(plat, registration_leverage=rec["opportunities_blocked"])

    state_cov = state_coverage_report(rows)
    buyer_cov = buyer_type_coverage_report(rows)
    cat_cov = category_coverage_report(rows)
    buyer_paths = all_buyer_history_paths()

    material_ab = (to_a + to_b) >= 3
    material_c = to_c >= 5
    hunt_ok = hunt_status in {HUNT_COMPLETE, HUNT_COMPLETE_WITH_FAILURES, None} or (
        refresh_hunt and hunt_status in {HUNT_COMPLETE, HUNT_COMPLETE_WITH_FAILURES}
    )
    quote_improved = (len(validated) + len(secondary)) > (
        L11_BASELINE["validated"] + L11_BASELINE["secondary"]
    )
    gaps_classified = len(auth_queue) > 0 and sum(access_modes.values()) > 0

    if material_ab and gaps_classified and (hunt_ok or not refresh_hunt):
        verdict = "PHASE_L12_AUTH_HISTORY_RECOVERY_WORKING"
    elif (to_a + to_b + to_c) > 0 or gaps_classified or (refresh_hunt and hunt_ok):
        verdict = "PHASE_L12_PARTIAL_AUTH_HISTORY_RECOVERY"
    else:
        verdict = "PHASE_L12_AUTH_HISTORY_RECOVERY_FAILED"

    bottleneck_parts = []
    if to_a + to_b == 0:
        bottleneck_parts.append("zero exact Gov A/B from public buyer paths — evidence still behind registration/auth")
    free_n = int(access_modes.get(FREE_REGISTRATION_REQUIRED, 0))
    vendor_n = int(access_modes.get(VENDOR_ACCOUNT_REQUIRED, 0))
    anti_n = int(access_modes.get(PUBLIC_ANTI_BOT_BLOCKED, 0))
    if free_n + vendor_n + anti_n:
        bottleneck_parts.append(
            f"owner registration on high-value platforms ({free_n} free-reg, {vendor_n} vendor, {anti_n} anti-bot)"
        )
    if remain_d > 50:
        bottleneck_parts.append(f"{remain_d} Gov D remain after buyer-pivot exhaustion")
    bottleneck_parts.append("no accounts created; no outreach; final bid gate unchanged")
    bottleneck = "; ".join(bottleneck_parts)

    # Prefer precise next move
    next_move = "owner registration on high-value platforms"
    if reg_priorities and reg_priorities[0].get("platform") == "BidNet":
        next_move = "owner registration on high-value platforms"
    elif to_a + to_b >= 3 and len(validated) <= L11_BASELINE["validated"]:
        next_move = "quantity/configuration recovery"
    elif free_n + vendor_n == 0 and anti_n == 0 and remain_d > 50 and not reg_priorities:
        next_move = "discovery coverage expansion"
    elif to_a + to_b == 0 and (vendor_n or anti_n):
        next_move = "manual authenticated history retrieval"
    elif len(validated) + len(secondary) > 5:
        next_move = "supplier quote outreach"

    payload = {
        "kind": "PhaseL12AuthWalledHistoryRecoveryResult",
        "phase": "L.12",
        "build": BUILD,
        "generated_at": _utc(),
        "verdict": verdict,
        "seed_stats": seed_stats,
        "fresh_hunt": {
            "ran": refresh_hunt,
            "status": hunt_status,
            "discovery_meta": discovery_meta,
            "raw": (discovery_meta.get("live_runner") or {}).get("raw_count"),
            "accessible": len(rows),
            "stage1": int(stage_counts["stage1"]),
            "stage2": int(stage_counts["stage2"]),
            "stage3": len(stage3),
        },
        "gov_history_upgrades": {
            "starting_gov_d": starting_d,
            "to_A": to_a,
            "to_B": to_b,
            "to_C": to_c,
            "remain_D": remain_d,
            "old_corpus": dict(old_corpus_upgrades),
            "fresh_corpus": dict(fresh_corpus_upgrades),
            "current_gov_grades": {
                "A": int(gov_grades.get(GOV_VALUE_A, 0)),
                "B": int(gov_grades.get(GOV_VALUE_B, 0)),
                "C": int(gov_grades.get(GOV_VALUE_C, 0)),
                "D": int(gov_grades.get(GOV_VALUE_D, 0)),
            },
            "outcomes": dict(history_outcomes),
        },
        "recovery_source_productivity": dict(recovery_sources),
        "auth_barriers": {
            "free_registration": int(access_modes.get(FREE_REGISTRATION_REQUIRED, 0)),
            "vendor_account": int(access_modes.get(VENDOR_ACCOUNT_REQUIRED, 0)),
            "buyer_account": int(access_modes.get(BUYER_SPECIFIC_ACCOUNT_REQUIRED, 0)),
            "private_restricted": int(access_modes.get(PRIVATE_RESTRICTED, 0)),
            "anti_bot": int(access_modes.get(PUBLIC_ANTI_BOT_BLOCKED, 0)),
            "no_public_history": int(access_modes.get(NO_PUBLIC_HISTORY_FEATURE, 0)),
            "access_mode_distribution": dict(access_modes),
        },
        "registration_priorities": reg_priorities[:20],
        "supplier_upgrades": {
            "prior_awardees_recovered": len(prior_awardees),
            "moves": dict(supplier_upgrade_moves),
            "C_to_B": int(supplier_upgrade_moves.get("SUPPLIER_C→SUPPLIER_B", 0)),
            "C_to_A": int(supplier_upgrade_moves.get("SUPPLIER_C→SUPPLIER_A", 0)),
            "D_to_B": int(supplier_upgrade_moves.get("SUPPLIER_D→SUPPLIER_B", 0)),
            "D_to_A": int(supplier_upgrade_moves.get("SUPPLIER_D→SUPPLIER_A", 0)),
            "grades": {
                "A": int(supplier_grades.get(SUPPLIER_A, 0)),
                "B": int(supplier_grades.get(SUPPLIER_B, 0)),
                "C": int(supplier_grades.get("SUPPLIER_C", 0)),
                "D": int(supplier_grades.get("SUPPLIER_D", 0)),
            },
        },
        "quote_quality": {
            "before": {
                "validated": L11_BASELINE["validated"],
                "secondary": L11_BASELINE["secondary"],
            },
            "after": {
                "validated": len(validated),
                "secondary": len(secondary),
                "recon_only": len(recon_only),
            },
            "improved": quote_improved,
            "quality_state_distribution": dict(quality_states),
        },
        "competition_intelligence": {
            "bidder_counts_recovered": competition_bidder_counts,
            "bid_price_distributions_recovered": competition_distributions,
        },
        "queues": {
            "HISTORY_ACCESS_GAP_QUEUE": len(auth_queue),
            "PUBLIC_HISTORY_RECOVERY_QUEUE": len(public_queue),
        },
        "opportunity_coverage": {
            "stage1": int(stage_counts["stage1"]),
            "stage2": int(stage_counts["stage2"]),
            "stage3": len(stage3),
            "stage3_no_row_cap": STAGE3_NO_ROW_CAP,
        },
        "discovery_coverage": {
            "state": state_cov,
            "buyer_type": buyer_cov,
            "category": cat_cov,
            "weak_platforms": weak_plat,
        },
        "platform_history_inventory": platform_history_inventory(),
        "buyer_history_paths_count": len((buyer_paths.get("buyers") or {})),
        "legacy_cleanup": legacy_cleanup_report(),
        "remaining_bottleneck": bottleneck,
        "next_move": next_move,
        "stop_rules": {
            "no_phase_m": True,
            "no_outreach": True,
            "no_account_creation": True,
            "no_login": True,
            "no_captcha": True,
            "no_quotes": True,
            "no_bids": True,
            "evidence_standards_unchanged": True,
            "final_verification_unchanged": True,
        },
    }

    save_json(OUT / "l12_gov_history_upgrades.json", {"rows": upgrade_audits, "summary": payload["gov_history_upgrades"]})
    save_json(OUT / "l12_history_access_gaps.json", {"kind": "HISTORY_ACCESS_GAP_QUEUE", "rows": auth_queue})
    save_json(OUT / "l12_registration_priorities.json", {"kind": "HistoryAccessRegistrationPriority", "rows": reg_priorities})
    save_json(OUT / "l12_buyer_history_paths.json", buyer_paths)
    save_json(OUT / "l12_prior_awardees.json", {"rows": prior_awardees})
    save_json(OUT / "l12_supplier_upgrades.json", {"rows": supplier_upgrade_rows, "summary": payload["supplier_upgrades"]})
    save_json(OUT / "l12_validated_quote_targets.json", validated)
    save_json(OUT / "l12_secondary_quote_targets.json", secondary)
    save_json(OUT / "l12_fresh_hunt.json", payload["fresh_hunt"])
    save_json(OUT / "l12_public_history_recovery_queue.json", {"kind": "PUBLIC_HISTORY_RECOVERY_QUEUE", "rows": public_queue})
    save_json(OUT / "l12_summary.json", payload)

    print(
        f"[l12] verdict={verdict} D:{starting_d} →A={to_a} B={to_b} C={to_c} "
        f"gaps={len(auth_queue)} regs={len(reg_priorities)} val={len(validated)} sec={len(secondary)}",
        flush=True,
    )
    return payload


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--refresh-hunt", action="store_true")
    p.add_argument("--no-live", action="store_true")
    p.add_argument("--max-hunt-sources", type=int, default=20)
    p.add_argument("--reset-checkpoint", action="store_true")
    p.add_argument("--history-live-fetches", type=int, default=2)
    args = p.parse_args()
    run_phase_l12_auth_history(
        authorize_live=not args.no_live,
        refresh_hunt=args.refresh_hunt,
        max_hunt_sources=args.max_hunt_sources,
        reset_hunt_checkpoint=args.reset_checkpoint,
        history_live_fetches=0 if args.no_live else args.history_live_fetches,
    )
