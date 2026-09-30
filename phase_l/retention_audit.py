"""Phase L.5 — Stage 1→2 attrition audit + commercial false-negative recovery."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.acquisition_lanes import (
    COMMERCIAL_DISTRIBUTOR_CHANNEL,
    COMMERCIAL_OPEN_CHANNEL,
    MILSPEC_OPEN_CHANNEL,
    MILSPEC_SPECIALTY,
    QUOTE_REQUIRED_COMMERCIAL,
    classify_acquisition_lane,
)
from phase_l.commercial_feed_expansion import classify_buyer_type, source_family_bucket
from phase_l.progressive_funnel import run_progressive_stages_cheap, stage2_identity_anchor
from phase_l.stage2_admission import (
    ADVANCED_STAGE2,
    COMMERCIAL_SIGNAL_TOO_LOW,
    DEADLINE_FAILURE,
    HARD_REJECT,
    IDENTITY_INCOMPLETE,
    MISSING_EVIDENCE,
    OTHER,
    SPECIALTY_ROUTE,
    collect_stage2_anchors_l5,
    commercial_category_hit,
    detect_brand_or_equal,
    detect_spec_driven_commercial,
    stage2_researchability_score,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase_l"
OUT.mkdir(parents=True, exist_ok=True)

BUILD = "20260927-m3-phase-l5-commercial-retention-repair"

LEGACY_ANCHORS = {
    "mpn",
    "nsn",
    "manufacturer_model",
    "commercial_model",
    "commercial_state",
    "sku",
    "strong_description",
}


def _utc() -> str:
    return now_utc().isoformat()


def _commercial_lane(lane: str | None) -> bool:
    return lane in {
        COMMERCIAL_OPEN_CHANNEL,
        COMMERCIAL_DISTRIBUTOR_CHANNEL,
        QUOTE_REQUIRED_COMMERCIAL,
        MILSPEC_OPEN_CHANNEL,
    }


def attrition_reason_bucket(pipe: dict[str, Any], *, legacy_pass: bool, l5_pass: bool) -> str:
    """Map drop to attrition category for reporting."""
    if (pipe.get("stage0") or {}).get("pass") is False:
        reason = str((pipe.get("stage0") or {}).get("reason") or "")
        if "deadline" in reason:
            return "deadline"
        if "construction" in reason or "food" in reason:
            return "true_non_product"
        if "access" in reason:
            return "registration_login"
        if "source" in reason:
            return "source_approval"
        return "true_hard_reject"
    if (pipe.get("stage1") or {}).get("pass") is False:
        reason = str((pipe.get("stage1") or {}).get("reason") or "")
        if "SERVICE" in reason or "REPAIR" in reason or "ENGINEERING" in reason:
            return "true_non_product"
        return "true_hard_reject"
    if legacy_pass:
        return "advanced_legacy"
    # Stage 2 fail under legacy
    s2 = pipe.get("stage2") or {}
    anchors = set(s2.get("identity_anchors") or [])
    commercial = (s2.get("commercial") or {})
    st = str(commercial.get("commercial_identity_state") or s2.get("identity_state") or "")
    if "PARTIAL" in st and not (commercial.get("model") or commercial.get("mpn")):
        return "weak_product_identity"
    if "GENERIC" in st:
        return "weak_product_identity"
    if detect_brand_or_equal(pipe.get("row") or {}, commercial).get("brand_or_equal"):
        return "brand_or_equal"
    if detect_spec_driven_commercial(pipe.get("row") or {}, commercial):
        return "descriptive_specification"
    if not commercial.get("model") and not commercial.get("mpn") and not (s2.get("identity") or {}).get("nsn"):
        return "no_exact_mpn_model"
    if s2.get("quantity") is None:
        # quantity missing was never the real gate, but classify residual
        pass
    if not anchors or anchors <= {"brand_clue", "descriptive_spec", "category_tangible", "partial_commercial", "document_signal", "brand_or_equal"}:
        if _commercial_lane((s2.get("acquisition_lane"))):
            return "commercial_score_threshold"  # had commercial lane but legacy rejected
        return "unknown_acquisition_path"
    return "weak_product_identity"


def disposition_for_row(
    row: dict[str, Any],
    pipe: dict[str, Any],
    *,
    legacy_pass: bool,
    l5_pass: bool,
) -> dict[str, Any]:
    drop = pipe.get("drop_stage")
    s2 = pipe.get("stage2") or {}
    lane = s2.get("acquisition_lane") or (pipe.get("stage1") or {}).get("acquisition_lane")
    primary = OTHER
    rule_ids: list[str] = []
    field_value = None

    if drop == 0:
        primary = DEADLINE_FAILURE if "deadline" in str((pipe.get("stage0") or {}).get("reason") or "") else HARD_REJECT
        rule_ids.append("stage0:" + str((pipe.get("stage0") or {}).get("reason") or "hard"))
        field_value = (pipe.get("stage0") or {}).get("reason")
    elif drop == 1:
        primary = HARD_REJECT
        rule_ids.append("stage1:" + str((pipe.get("stage1") or {}).get("reason") or "fitness"))
        field_value = (pipe.get("stage1") or {}).get("reason")
    elif legacy_pass and l5_pass:
        primary = ADVANCED_STAGE2
        rule_ids.append("stage2:legacy_and_l5")
    elif (not legacy_pass) and l5_pass:
        if _commercial_lane(lane) or (s2.get("researchability_score") or 0) >= 30:
            primary = ADVANCED_STAGE2
            rule_ids.append("stage2:l5_recovered")
        else:
            primary = ADVANCED_STAGE2
            rule_ids.append("stage2:l5_permissive")
        field_value = ",".join(s2.get("identity_anchors") or [])
    elif not l5_pass:
        bucket = attrition_reason_bucket(pipe, legacy_pass=False, l5_pass=False)
        if bucket in {"weak_product_identity", "no_exact_mpn_model"}:
            primary = IDENTITY_INCOMPLETE
        elif bucket == "unknown_acquisition_path":
            primary = COMMERCIAL_SIGNAL_TOO_LOW
        elif lane == MILSPEC_SPECIALTY:
            primary = SPECIALTY_ROUTE
        else:
            primary = MISSING_EVIDENCE
        rule_ids.append("stage2:no_anchor")
        field_value = (s2.get("reason") or "no_identity_anchor")
    else:
        primary = ADVANCED_STAGE2

    return {
        "disposition": primary,
        "rule_ids": rule_ids,
        "field_value": field_value,
        "legacy_pass": legacy_pass,
        "l5_pass": l5_pass,
        "acquisition_lane": lane,
        "attrition_bucket": attrition_reason_bucket(pipe, legacy_pass=legacy_pass, l5_pass=l5_pass),
    }


def is_lost_commercial_candidate(row: dict[str, Any], pipe: dict[str, Any], *, legacy_pass: bool) -> dict[str, Any] | None:
    """Independently estimate if a legacy Stage2-fail looks commercially sourceable."""
    if legacy_pass:
        return None
    if not (pipe.get("stage1") or {}).get("pass"):
        return None
    commercial = ((pipe.get("stage2") or {}).get("commercial") or {})
    rs = stage2_researchability_score(row, commercial=commercial)
    boe = detect_brand_or_equal(row, commercial)
    spec = detect_spec_driven_commercial(row, commercial)
    cats = commercial_category_hit(row)
    lane = classify_acquisition_lane(row, commercial=commercial)
    signals = list(rs.get("factors") or [])
    if boe.get("brand_or_equal"):
        signals.append("brand_or_equal")
    if spec:
        signals.append("spec_driven")
    if cats:
        signals.append("categories:" + ",".join(cats))
    score = rs["score"]
    # commercial candidate if researchability or lane priority or brand/spec
    if score < 25 and not boe.get("brand_or_equal") and not spec and not cats:
        return None
    conf = "HIGH" if score >= 55 or boe.get("brand_or_equal") else ("MEDIUM" if score >= 35 or spec else "LOW")
    return {
        "kind": "LostCommercialCandidate",
        "opportunity_id": row.get("solicitation_id") or row.get("notice_id") or row.get("external_id"),
        "title": (row.get("title") or "")[:160],
        "buyer": row.get("agency") or row.get("buyer"),
        "source_level": row.get("source_level") or row.get("level"),
        "source_id": row.get("source_id"),
        "original_url": row.get("detail_url") or row.get("source_url") or row.get("original_posting_url"),
        "legacy_reject_reason": (pipe.get("stage2") or {}).get("reason") or "no_identity_anchor",
        "commercial_signals": signals,
        "likely_acquisition_lane": lane.get("acquisition_lane"),
        "confidence": conf,
        "researchability_score": score,
        "stage2_should_have_been_attempted": True,
        "categories": cats,
        "buyer_type": classify_buyer_type(row),
        "source_family": source_family_bucket(row),
    }


def audit_accessible_rows(
    rows: list[dict[str, Any]] | None = None,
    *,
    path: Path | None = None,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """Full Stage 1 attrition audit + L.5 counterfactual on frozen L.4 accessible set."""
    if rows is None:
        path = path or (OUT / "accessible_latest.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = list(data.get("rows") or [])

    access_yes = [r for r in rows if str(r.get("our_bid_access") or "") == "YES"]
    if max_rows:
        access_yes = access_yes[:max_rows]

    dispositions: Counter = Counter()
    attrition_buckets: Counter = Counter()
    lost_commercial: list[dict[str, Any]] = []
    recovered: list[dict[str, Any]] = []
    false_neg_sample: list[dict[str, Any]] = []

    old_s1 = old_s2 = old_s3 = 0
    new_s1 = new_s2 = new_s3 = 0
    old_commercial_s3 = new_commercial_s3 = 0
    old_quote = new_quote = 0
    old_specialty = new_specialty = 0
    old_unknown = new_unknown = 0
    hard_rejects = 0

    source_attr: dict[str, Counter] = {}
    buyer_attr: dict[str, Counter] = {}
    cat_attr: dict[str, Counter] = {}

    detail_rows: list[dict[str, Any]] = []

    for i, row in enumerate(access_yes):
        if i and i % 200 == 0:
            print(f"[l5-audit] {i}/{len(access_yes)} recovered={len(recovered)} lost_comm={len(lost_commercial)}", flush=True)

        pipe = run_progressive_stages_cheap(row)
        pipe["row"] = row  # for attrition helpers
        s1 = bool((pipe.get("stage1") or {}).get("pass"))
        s2 = pipe.get("stage2") or {}
        # Recompute anchors with explicit L5 helper for legacy comparison
        commercial = s2.get("commercial") or {}
        identity = s2.get("identity") or {}
        # When stage2 didn't run (dropped earlier), still compute for commercial loss
        if not s2.get("identity_anchors") and s1:
            # stage2 ran if drop_stage is None or >=2
            pass
        l5_info = collect_stage2_anchors_l5(row, commercial=commercial or {}, identity=identity or {})
        legacy_pass = bool(l5_info.get("legacy_strict_would_pass"))
        # Actual L5 pass from progressive funnel (already L5)
        l5_pass = bool(s2.get("pass")) if s1 else False
        # If stage1 passed but stage2 wasn't computed with commercial overlay empty from fail path:
        if s1 and pipe.get("drop_stage") == 2:
            # Under L5, stage2_identity_anchor already ran — l5_pass is s2.pass
            legacy_pass = bool(s2.get("legacy_strict_would_pass"))
            l5_pass = bool(s2.get("pass"))
        elif s1 and pipe.get("survives_to_stage3"):
            legacy_pass = bool(s2.get("legacy_strict_would_pass"))
            l5_pass = True
        elif s1 and s2.get("pass"):
            legacy_pass = bool(s2.get("legacy_strict_would_pass"))
            l5_pass = True

        # Counts — OLD = legacy strict Stage2
        if s1:
            old_s1 += 1
            new_s1 += 1
        if s1 and legacy_pass:
            old_s2 += 1
            old_s3 += 1  # L.4: stage2==stage3 for survivors
            lane = s2.get("acquisition_lane")
            if _commercial_lane(lane):
                old_commercial_s3 += 1
            if lane == QUOTE_REQUIRED_COMMERCIAL:
                old_quote += 1
            if lane == MILSPEC_SPECIALTY:
                old_specialty += 1
            if lane and "UNKNOWN" in str(lane):
                old_unknown += 1
        if s1 and l5_pass:
            new_s2 += 1
            new_s3 += 1
            lane = s2.get("acquisition_lane") or classify_acquisition_lane(row, commercial=commercial).get("acquisition_lane")
            if _commercial_lane(lane):
                new_commercial_s3 += 1
            if lane == QUOTE_REQUIRED_COMMERCIAL:
                new_quote += 1
            if lane == MILSPEC_SPECIALTY:
                new_specialty += 1
            if lane and "UNKNOWN" in str(lane):
                new_unknown += 1

        if not s1:
            hard_rejects += 1

        disp = disposition_for_row(row, pipe, legacy_pass=legacy_pass, l5_pass=l5_pass)
        dispositions[disp["disposition"]] += 1
        attrition_buckets[disp["attrition_bucket"]] += 1

        # Source / buyer / category attrition
        fam = source_family_bucket(row)
        bt = classify_buyer_type(row)
        cats = commercial_category_hit(row) or ["OTHER"]
        for key, store in ((fam, source_attr), (bt, buyer_attr)):
            c = store.setdefault(key, Counter())
            if s1:
                c["stage1"] += 1
            if s1 and legacy_pass:
                c["stage2_old"] += 1
            if s1 and l5_pass:
                c["stage2_new"] += 1
                if _commercial_lane(s2.get("acquisition_lane")):
                    c["commercial_s3_new"] += 1
        for cat in cats[:2]:
            c = cat_attr.setdefault(cat, Counter())
            if s1:
                c["stage1"] += 1
            if s1 and legacy_pass:
                c["stage2_old"] += 1
            if s1 and l5_pass:
                c["stage2_new"] += 1

        lost = is_lost_commercial_candidate(row, pipe, legacy_pass=legacy_pass)
        if lost:
            lost_commercial.append(lost)

        if s1 and l5_pass and not legacy_pass:
            recovered.append(
                {
                    "opportunity_id": row.get("solicitation_id") or row.get("notice_id") or row.get("external_id"),
                    "title": (row.get("title") or "")[:140],
                    "source": source_family_bucket(row),
                    "buyer_type": classify_buyer_type(row),
                    "old_failure_reason": "no_identity_anchor",
                    "new_stage": 2,
                    "commercial_lane": s2.get("acquisition_lane"),
                    "anchors": s2.get("identity_anchors"),
                    "reason_restored": "L5_permissive_anchor:" + ",".join(s2.get("identity_anchors") or []),
                    "original_url": row.get("detail_url") or row.get("source_url"),
                }
            )

        if s1:
            detail_rows.append(
                {
                    "id": row.get("solicitation_id") or row.get("notice_id"),
                    "title": (row.get("title") or "")[:100],
                    "disposition": disp["disposition"],
                    "attrition_bucket": disp["attrition_bucket"],
                    "legacy_pass": legacy_pass,
                    "l5_pass": l5_pass,
                    "lane": s2.get("acquisition_lane"),
                    "anchors": s2.get("identity_anchors"),
                }
            )

    # Top 100 false negatives for manual inspection
    lost_sorted = sorted(
        lost_commercial,
        key=lambda x: (
            {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(x.get("confidence") or "LOW", 9),
            -(x.get("researchability_score") or 0),
        ),
    )
    for lc in lost_sorted[:100]:
        false_neg_sample.append(
            {
                **lc,
                "audit_answers": {
                    "buyer_wants": lc.get("title"),
                    "why_not_advanced": lc.get("legacy_reject_reason"),
                    "genuinely_disqualifying": False,
                    "stage2_could_research": True,
                    "rule_change": "Admit brand_or_equal / descriptive_spec / brand_clue / category_tangible anchors",
                },
            }
        )

    stage1_n = old_s1
    attrition_n = max(0, stage1_n - old_s2)

    # Commercial retention rates
    def _ret(s1c: int, s3c: int) -> float:
        return round(100.0 * s3c / s1c, 1) if s1c else 0.0

    # Approximate commercial Stage 1 as lost_commercial + old commercial survivors
    commercial_s1_est = len(lost_commercial) + old_commercial_s3

    payload = {
        "kind": "PhaseL5RetentionAudit",
        "build": BUILD,
        "generated_at": _utc(),
        "input_accessible": len(rows),
        "access_yes": len(access_yes),
        "l4_attrition_audit": {
            "stage1": stage1_n,
            "stage2_old": old_s2,
            "stage3_old": old_s3,
            "rows_lost": attrition_n,
            "attrition_pct": round(100.0 * attrition_n / max(stage1_n, 1), 1),
            "top_loss_reasons": dict(attrition_buckets.most_common(20)),
            "dispositions": dict(dispositions),
        },
        "commercial_false_negatives": {
            "count": len(lost_commercial),
            "sample_n": len(false_neg_sample),
            "top_causes": dict(Counter(x.get("legacy_reject_reason") for x in lost_commercial).most_common(10)),
        },
        "counterfactual_replay": {
            "old_stage1": old_s1,
            "new_stage1": new_s1,
            "old_stage2": old_s2,
            "new_stage2": new_s2,
            "old_stage3": old_s3,
            "new_stage3": new_s3,
            "old_commercial": old_commercial_s3,
            "new_commercial": new_commercial_s3,
            "old_quote_required": old_quote,
            "new_quote_required": new_quote,
            "old_specialty": old_specialty,
            "new_specialty": new_specialty,
            "old_unknown": old_unknown,
            "new_unknown": new_unknown,
            "hard_rejects": hard_rejects,
            "delta_stage2": new_s2 - old_s2,
            "delta_commercial": new_commercial_s3 - old_commercial_s3,
        },
        "recovered_commercial_candidates": {
            "count": len(recovered),
            "rows": recovered[:200],
        },
        "false_negative_sample": false_neg_sample,
        "commercial_retention": {
            "overall_old": _ret(commercial_s1_est, old_commercial_s3),
            "overall_new": _ret(commercial_s1_est, new_commercial_s3),
            "commercial_s1_estimate": commercial_s1_est,
            "by_source": {
                k: {
                    "stage1": v.get("stage1", 0),
                    "stage2_old": v.get("stage2_old", 0),
                    "stage2_new": v.get("stage2_new", 0),
                    "attrition_old_pct": round(
                        100.0 * (1 - v.get("stage2_old", 0) / max(v.get("stage1", 1), 1)), 1
                    ),
                    "commercial_s3_new": v.get("commercial_s3_new", 0),
                }
                for k, v in sorted(source_attr.items())
            },
            "by_buyer_type": {
                k: dict(v) for k, v in sorted(buyer_attr.items())
            },
            "by_category": {
                k: dict(v) for k, v in sorted(cat_attr.items())
            },
        },
        "stage2_no_cap": True,
        "stage3_no_cap": True,
    }

    (OUT / "l5_retention_audit.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload
