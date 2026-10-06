"""Full-universe funnel sweep → END_OF_FUNNEL_READY measurement.

Build target: 20261003-m3-full-funnel-sweep-v1

Uses current production paths only:
  discovery (BidNet / OpenGov / expansion / SAM+DLA snapshot)
  → reconcile + classify
  → free package chase (BidNet discovery-only) + OpenGov recovery
  → document quality gate
  → identity / gov-value / acquisition-cost / profit_first
  → END_OF_FUNNEL_READY report

No paid Euna nationwide. No BidNet membership unlock.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any, Callable
from uuid import uuid4

from application_clock import now_utc

log = logging.getLogger("govtracker.full_funnel_sweep")

BUILD_TARGET = "20261003-m3-full-funnel-sweep-v1"
CHECKPOINT = "m3_full_funnel_sweep_checkpoint.json"
REPORT = "m3_full_funnel_sweep_last_report.json"

PROFIT_BUCKETS = (
    (0, "gt_0"),
    (1000, "gte_1k"),
    (2500, "gte_2_5k"),
    (5000, "gte_5k"),
    (10000, "gte_10k"),
    (25000, "gte_25k"),
    (50000, "gte_50k"),
    (75000, "gte_75k"),
)

END_OF_FUNNEL_READY = "END_OF_FUNNEL_READY"


def _paths():
    from m3_data_root import data_path

    return data_path(CHECKPOINT), data_path(REPORT)


def _load_ck() -> dict[str, Any]:
    path, _ = _paths()
    if not path.exists():
        return {"kind": "FullFunnelSweepCheckpoint", "completed_stages": [], "phases": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"kind": "FullFunnelSweepCheckpoint", "completed_stages": [], "phases": {}}


def _save_ck(payload: dict[str, Any]) -> None:
    path, _ = _paths()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = now_utc().isoformat()
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _save_report(payload: dict[str, Any]) -> None:
    _, path = _paths()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _platform_bucket(rec: dict[str, Any]) -> str:
    plat = str(rec.get("platform") or rec.get("platform_family") or "").lower()
    sid = str(rec.get("source_id") or "").lower()
    blob = f"{plat} {sid}"
    if "bidnet" in blob:
        return "BidNet"
    if "opengov" in blob or "live_structured" in blob:
        return "OpenGov"
    if "sam" in blob or ("federal" in blob and "dla" not in blob):
        return "SAM"
    if "dla" in blob or "dibbs" in blob:
        return "DLA"
    return "Other"


def _profit_val(rec: dict[str, Any]) -> float | None:
    pf = rec.get("profit_first") if isinstance(rec.get("profit_first"), dict) else {}
    for key in ("expected_profit", "post_financing_profit"):
        v = pf.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    card = pf.get("owner_card") if isinstance(pf.get("owner_card"), dict) else {}
    v = card.get("expected_profit")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _has_real_package(rec: dict[str, Any]) -> tuple[bool, str]:
    """Return (usable_package, quality_label)."""
    pq = rec.get("package_quality") if isinstance(rec.get("package_quality"), dict) else {}
    br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
    br_pq = br.get("package_quality") if isinstance(br.get("package_quality"), dict) else {}
    quality = str(
        pq.get("package_quality")
        or br_pq.get("package_quality")
        or ""
    )
    if quality in {"VALID_SOLICITATION_PACKAGE", "PARTIAL_SOLICITATION_PACKAGE"}:
        return True, quality
    if quality in {"UNRELATED_DOCUMENTS", "AMBIGUOUS_DOCUMENTS"}:
        return False, quality

    chase = br.get("free_package_chase") if isinstance(br.get("free_package_chase"), dict) else {}
    if chase.get("false_package"):
        return False, "UNRELATED_DOCUMENTS"

    # Lazily quality-gate FREE_PACKAGE_FOUND / non-BidNet attachments during report
    atts = rec.get("attachments_metadata") or []
    scored_inputs: list[dict[str, Any]] = []
    for d in atts:
        if not isinstance(d, dict):
            continue
        u = str(d.get("document_url") or d.get("url") or "")
        low = u.lower()
        name = str(d.get("document_name") or d.get("name") or "")
        if not u.startswith("http") and not d.get("local_path"):
            continue
        if "abstract" in low or ("bidnet" in low and not d.get("free_chase")):
            continue
        text = ""
        if d.get("local_path"):
            try:
                from document_quality import extract_pdf_text

                text = extract_pdf_text(str(d["local_path"]))
            except Exception:
                text = ""
        if not text:
            text = name
        scored_inputs.append({"document_name": name, "text": text})

    if scored_inputs:
        try:
            from document_quality import classify_package_documents

            agg = classify_package_documents(
                scored_inputs,
                title=rec.get("title"),
                buyer=rec.get("buyer"),
                solicitation_number=rec.get("solicitation_number") or br.get("solicitation_number"),
            )
            q = str(agg.get("package_quality") or "AMBIGUOUS_DOCUMENTS")
            return bool(agg.get("usable")), q
        except Exception:
            if str(chase.get("status") or "") == "FREE_PACKAGE_FOUND":
                return False, "AMBIGUOUS_DOCUMENTS"
            return False, "AMBIGUOUS_DOCUMENTS"

    if str(chase.get("status") or "") == "FREE_PACKAGE_FOUND":
        return False, "AMBIGUOUS_DOCUMENTS"
    return False, "NO_REAL_PACKAGE"


def _identity_bucket(identity_class: str) -> str:
    ic = str(identity_class or "").upper()
    if ic in {"EXACT_PART_NUMBER", "EXACT_MPN", "EXACT_NSN", "EXACT_MODEL"}:
        if "NSN" in ic:
            return "EXACT_NSN"
        if "MODEL" in ic:
            return "EXACT_MODEL"
        return "EXACT_MPN"
    if ic in {"BRAND_OR_EQUAL", "PERMITTED_EQUAL"}:
        return "PERMITTED_EQUAL"
    if ic in {"GENERIC_SPEC", "STRONG_GENERIC_SPEC"}:
        return "STRONG_GENERIC_SPEC"
    if ic in {"PARTIAL_IDENTITY", "WEAK_IDENTITY"}:
        return "WEAK_IDENTITY"
    return "NO_IDENTITY"


def build_funnel_completion_report(
    *,
    phase_results: dict[str, Any],
    canonical_before: int,
    run_id: str,
) -> dict[str, Any]:
    """Scan live store into the exact owner completion structure."""
    from document_quality import (
        AMBIGUOUS_DOCUMENTS,
        PARTIAL_SOLICITATION_PACKAGE,
        UNRELATED_DOCUMENTS,
        VALID_SOLICITATION_PACKAGE,
    )
    from m3_canonical_discovery_bridge import available_count
    from phase_l.l23_full_population_funnel import load_store
    from phase_l.owner_ui_service import _is_available_rec
    from universe_pass.classify import (
        ELIGIBLE_FOR_PROFIT,
        MIXED_PRODUCT_SERVICE,
        PURE_SERVICE,
        TANGIBLE_PRODUCT,
        UNKNOWN,
    )

    store = load_store()
    canonical_after = available_count(store)

    try:
        from line_item_economics.engine import load_analysis

        _load_lie = load_analysis
    except Exception:
        _load_lie = lambda _oid: None  # noqa: E731

    class_c: Counter = Counter()
    pkg_c: Counter = Counter()
    id_line_c: Counter = Counter()
    id_opp_c: Counter = Counter()
    status_c: Counter = Counter()
    drop: Counter = Counter()
    src_live: Counter = Counter()
    src_prod: Counter = Counter()
    src_pkg: Counter = Counter()
    src_id: Counter = Counter()
    src_gov: Counter = Counter()
    src_acq: Counter = Counter()
    src_both: Counter = Counter()
    src_eof: Counter = Counter()
    src_prof: Counter = Counter()
    src_10k: Counter = Counter()
    buckets: Counter = Counter()
    multi: dict[str, Counter] = {
        "10": Counter(),
        "20": Counter(),
        "40": Counter(),
    }

    gov_known = acq_known = both = 0
    public_retail = 0
    eof_ready = 0
    with_lines = 0
    total_lines = 0
    pkg_attempted = 0
    owner_candidates: list[dict[str, Any]] = []
    stage_prior = {
        "product_eligible": 0,
        "real_package": 0,
        "product_identity": 0,
        "gov_value": 0,
        "acq_cost": 0,
        "both_sides": 0,
        "eof": 0,
    }

    for cid, rec in store.items():
        if not isinstance(rec, dict) or not _is_available_rec(rec):
            continue
        src = _platform_bucket(rec)
        src_live[src] += 1
        cls = str(rec.get("universe_class") or rec.get("product_service_classification") or UNKNOWN)
        class_c[cls] += 1
        product_eligible = cls in ELIGIBLE_FOR_PROFIT or bool(rec.get("eligible_for_profit_research"))
        if not product_eligible:
            continue
        stage_prior["product_eligible"] += 1
        src_prod[src] += 1

        br = rec.get("bidnet_recovery") if isinstance(rec.get("bidnet_recovery"), dict) else {}
        chase = br.get("free_package_chase") if isinstance(br.get("free_package_chase"), dict) else {}
        if chase.get("status") or br.get("last_recovery_attempt") or rec.get("package_quality"):
            pkg_attempted += 1

        usable_pkg, pkg_label = _has_real_package(rec)
        if pkg_label == "NO_REAL_PACKAGE":
            pkg_c["NO_REAL_PACKAGE"] += 1
        elif pkg_label == VALID_SOLICITATION_PACKAGE:
            pkg_c["VALID"] += 1
        elif pkg_label == PARTIAL_SOLICITATION_PACKAGE:
            pkg_c["PARTIAL"] += 1
        elif pkg_label == UNRELATED_DOCUMENTS:
            pkg_c["UNRELATED"] += 1
        else:
            pkg_c["AMBIGUOUS"] += 1

        chase_st = str(chase.get("status") or "")
        if chase_st == "PACKAGE_UNAVAILABLE_FREE":
            pkg_c["UNAVAILABLE_FREE"] += 1
        elif chase_st == "PACKAGE_RECOVERY_RETRYABLE":
            pkg_c["RETRYABLE"] += 1
        elif chase_st == "PACKAGE_MATCH_AMBIGUOUS":
            pkg_c["AMBIGUOUS_MATCH"] += 1

        if not usable_pkg:
            if pkg_label == UNRELATED_DOCUMENTS:
                drop["FALSE_PACKAGE_MATCH"] += 1
            else:
                drop["NO_REAL_PACKAGE"] += 1
            continue
        stage_prior["real_package"] += 1
        src_pkg[src] += 1

        lie = None
        try:
            lie = _load_lie(cid)
        except Exception:
            lie = None
        lines = []
        if isinstance(lie, dict):
            lines = lie.get("lines") or lie.get("line_items") or []
        if not isinstance(lines, list):
            lines = []
        nlines = len(lines)
        if nlines:
            with_lines += 1
            total_lines += nlines
        exactish = 0
        for ln in lines:
            if not isinstance(ln, dict):
                continue
            bucket = _identity_bucket(str(ln.get("identity_class") or ""))
            id_line_c[bucket] += 1
            if bucket.startswith("EXACT") or bucket == "PERMITTED_EQUAL":
                exactish += 1
        has_identity = exactish > 0 or any(
            isinstance(ln, dict)
            and (
                ln.get("nsn")
                or ln.get("part_number")
                or (ln.get("model") and (ln.get("manufacturer") or ln.get("brand")))
            )
            for ln in lines
        )
        if has_identity:
            id_opp_c["with_identity"] += 1
            stage_prior["product_identity"] += 1
            src_id[src] += 1
        else:
            id_opp_c["no_identity"] += 1
            drop["NO_PRODUCT_IDENTITY"] += 1
            continue

        pf = rec.get("profit_first") if isinstance(rec.get("profit_first"), dict) else {}
        card = pf.get("owner_card") if isinstance(pf.get("owner_card"), dict) else {}
        pe = rec.get("product_economics") if isinstance(rec.get("product_economics"), dict) else {}
        rev = card.get("expected_revenue")
        if rev is None:
            rev = pf.get("expected_revenue") or pe.get("gov_value")
        cost = card.get("product_cost")
        if cost is None:
            cost = pf.get("product_cost") or pe.get("acquisition_cost")
        freight = card.get("freight")
        if freight is None:
            freight = pf.get("freight")
        financing = card.get("financing")
        if financing is None:
            financing = pf.get("financing")
        st = str(pf.get("profit_status") or "")
        if st:
            status_c[st] += 1
        signals = set(pf.get("proof_signals") or [])
        if "PROFITABLE_AT_PUBLIC_RETAIL" in signals:
            public_retail += 1

        has_gov = rev is not None or bool(pe.get("both_sides_known") and pe.get("gov_value") is not None)
        has_acq = cost is not None
        if isinstance(lie, dict):
            if lie.get("TOTAL_KNOWN_HISTORICAL_VALUE") is not None or lie.get("government_value"):
                has_gov = True
            if lie.get("product_cost") is not None or lie.get("public_retail_total") is not None:
                has_acq = True
        if has_gov:
            gov_known += 1
            stage_prior["gov_value"] += 1
            src_gov[src] += 1
        else:
            drop["NO_GOV_VALUE"] += 1
            continue
        if has_acq:
            acq_known += 1
            stage_prior["acq_cost"] += 1
            src_acq[src] += 1
        else:
            drop["NO_ACQUISITION_PRICE"] += 1
            continue
        both += 1
        stage_prior["both_sides"] += 1
        src_both[src] += 1

        # Multi-line buckets
        for thresh, key in ((10, "10"), (20, "20"), (40, "40")):
            if nlines >= thresh:
                multi[key]["count"] += 1
                multi[key]["packages_valid"] += 1
                multi[key]["fully_extracted"] += 1
                multi[key]["both_sides"] += 1

        freight_ok = freight is not None  # modeled or bounded (0 allowed)
        financing_ok = financing is not None
        if not freight_ok:
            drop["FREIGHT_UNKNOWN"] += 1
        if not financing_ok:
            drop["FINANCING_UNKNOWN"] += 1

        blockers = list(br.get("blockers") or [])
        exec_blocked = st == "EXECUTION_BLOCKED" or any(
            b in {"VENDOR_INELIGIBLE", "REGISTRATION_REQUIRED", "BRAND_AUTHORIZATION", "CAGE_REQUIRED"}
            for b in blockers
        )
        if exec_blocked and st == "EXECUTION_BLOCKED":
            drop["EXECUTION_BLOCKED"] += 1

        profit = _profit_val(rec)
        eof = bool(
            usable_pkg
            and has_identity
            and has_gov
            and has_acq
            and freight_ok
            and financing_ok
            and st
            and not exec_blocked
        )
        if eof:
            eof_ready += 1
            stage_prior["eof"] += 1
            src_eof[src] += 1
            if st in {"PROVEN_PROFITABLE", "LIKELY_PROFITABLE", "POSSIBLE_PROFIT"} or (
                profit is not None and profit > 0
            ):
                src_prof[src] += 1
            if profit is not None and profit >= 10000:
                src_10k[src] += 1
            if profit is not None:
                for threshold, bkey in PROFIT_BUCKETS:
                    if (profit > threshold) if threshold == 0 else (profit >= threshold):
                        buckets[bkey] += 1
            for thresh, key in ((10, "10"), (20, "20"), (40, "40")):
                if nlines >= thresh and profit is not None and profit > 0:
                    multi[key]["profitable"] += 1
                    multi[key]["fully_mostly_priced"] += 1
        else:
            if st == "UNPROFITABLE":
                drop["ACTUALLY_UNPROFITABLE"] += 1
            elif not freight_ok or not financing_ok:
                pass  # already counted
            elif not st:
                drop["OTHER"] += 1

        if st in {"PROVEN_PROFITABLE", "LIKELY_PROFITABLE"}:
            owner_candidates.append(
                {
                    "opportunity_id": cid,
                    "title": (rec.get("title") or "")[:160],
                    "buyer": rec.get("buyer") or rec.get("agency"),
                    "source": src,
                    "close_date": rec.get("deadline") or rec.get("due_date"),
                    "product_basket": f"{nlines} lines" if nlines else "unknown",
                    "package_status": pkg_label,
                    "gov_value_evidence": rev,
                    "acquisition_cost_evidence": cost,
                    "expected_revenue": rev,
                    "product_cost": cost,
                    "freight": freight,
                    "financing": financing,
                    "expected_profit": profit,
                    "margin": card.get("margin") or pf.get("margin"),
                    "confidence": card.get("confidence") or pf.get("ranking_score"),
                    "execution_status": card.get("execution") or st,
                    "documents": len(
                        [
                            d
                            for d in (rec.get("attachments_metadata") or [])
                            if isinstance(d, dict) and (d.get("document_url") or d.get("url"))
                        ]
                    ),
                    "next_action": pf.get("research_priority") or card.get("research_next"),
                    "profit_status": st,
                }
            )

    owner_candidates.sort(
        key=lambda x: (
            0 if x.get("profit_status") == "PROVEN_PROFITABLE" else 1,
            -(float(x.get("expected_profit") or 0)),
        )
    )

    # Drop-off % of prior stage
    drop_rows = []
    pe = max(1, stage_prior["product_eligible"])
    for reason, count in drop.most_common():
        drop_rows.append(
            {
                "reason": reason,
                "count": count,
                "pct_of_product_eligible": round(100.0 * count / pe, 2),
            }
        )

    def _src_row(name: str) -> dict[str, Any]:
        return {
            "live": int(src_live.get(name) or 0),
            "product_eligible": int(src_prod.get(name) or 0),
            "real_packages": int(src_pkg.get(name) or 0),
            "product_identified": int(src_id.get(name) or 0),
            "gov_value_known": int(src_gov.get(name) or 0),
            "acquisition_cost_known": int(src_acq.get(name) or 0),
            "both_sides_known": int(src_both.get(name) or 0),
            "end_of_funnel_ready": int(src_eof.get(name) or 0),
            "profitable": int(src_prof.get(name) or 0),
            "gte_10k_profit": int(src_10k.get(name) or 0),
        }

    source_to_end = {s: _src_row(s) for s in ("BidNet", "OpenGov", "SAM", "DLA", "Other")}

    top_src = max(source_to_end.items(), key=lambda kv: kv[1]["end_of_funnel_ready"])[0]
    biggest_drop = drop_rows[0]["reason"] if drop_rows else "NONE"

    questions = {
        "1_canonical_live": canonical_after,
        "2_product_eligible": stage_prior["product_eligible"],
        "3_real_solicitation_package": stage_prior["real_package"],
        "4_usable_product_identity": stage_prior["product_identity"],
        "5_gov_value_evidence": stage_prior["gov_value"],
        "6_acquisition_cost_evidence": stage_prior["acq_cost"],
        "7_both_sides": stage_prior["both_sides"],
        "8_end_of_funnel_ready": eof_ready,
        "9_proven_profitable": int(status_c.get("PROVEN_PROFITABLE") or 0),
        "10_likely_profitable": int(status_c.get("LIKELY_PROFITABLE") or 0),
        "11_profitable_at_public_retail": public_retail,
        "12_gte_5k_profit": int(buckets.get("gte_5k") or 0),
        "13_gte_10k_profit": int(buckets.get("gte_10k") or 0),
        "14_top_eof_source": top_src,
        "15_biggest_bottleneck": biggest_drop,
        "16_repeatable_pool_or_exceptions": (
            "REPEATABLE_POOL"
            if eof_ready >= 10 or (status_c.get("PROVEN_PROFITABLE", 0) + status_c.get("LIKELY_PROFITABLE", 0)) >= 5
            else "ISOLATED_EXCEPTIONS"
        ),
        "17_highest_leverage_change": (
            "Increase free solicitation-package recovery yield (agency/OpenGov/public PDF match quality) — "
            "NO_REAL_PACKAGE dominates before identity/economics can run"
            if biggest_drop in {"NO_REAL_PACKAGE", "FALSE_PACKAGE_MATCH"}
            else (
                "Improve government-value evidence recovery on packages with identity"
                if biggest_drop == "NO_GOV_VALUE"
                else (
                    "Improve public acquisition-cost recovery on identified lines"
                    if biggest_drop == "NO_ACQUISITION_PRICE"
                    else f"Address {biggest_drop}"
                )
            )
        ),
    }

    recon = phase_results.get("reconcile") if isinstance(phase_results.get("reconcile"), dict) else {}
    report = {
        "kind": "FullFunnelSweepReport",
        "build_target": BUILD_TARGET,
        "run_id": run_id,
        "completed_at": now_utc().isoformat(),
        "full_universe": {
            "canonical_before": canonical_before,
            "raw_discovered": recon.get("raw_total")
            or sum(
                int((phase_results.get(k) or {}).get("raw_opportunities") or (phase_results.get(k) or {}).get("retrieved_unique") or 0)
                for k in ("bidnet", "opengov", "expansion")
                if isinstance(phase_results.get(k), dict)
            ),
            "net_new": recon.get("net_new")
            or sum(
                int((phase_results.get(k) or {}).get("net_new") or 0)
                for k in ("bidnet", "opengov", "expansion")
                if isinstance(phase_results.get(k), dict)
            ),
            "duplicates_removed": recon.get("duplicates_removed"),
            "expired_removed": recon.get("expired_removed"),
            "cancelled_removed": recon.get("cancelled_removed"),
            "canonical_after": canonical_after,
        },
        "source_counts": {
            "BidNet": phase_results.get("bidnet") or {},
            "OpenGov": phase_results.get("opengov") or {},
            "SAM": phase_results.get("sam") or {},
            "DLA": phase_results.get("dla") or {},
            "Other": phase_results.get("expansion") or {},
        },
        "product_funnel": {
            "TANGIBLE_PRODUCT": int(class_c.get(TANGIBLE_PRODUCT) or 0),
            "LIKELY_PRODUCT": int(class_c.get("LIKELY_PRODUCT") or 0),
            "MIXED": int(class_c.get(MIXED_PRODUCT_SERVICE) or 0),
            "SERVICE": int(class_c.get(PURE_SERVICE) or 0),
            "UNKNOWN": int(class_c.get(UNKNOWN) or 0),
            "CONSTRUCTION": int(class_c.get("CONSTRUCTION") or 0),
            "product_eligible": stage_prior["product_eligible"],
            "class_counts": dict(class_c),
        },
        "package_funnel": {
            "attempted": pkg_attempted,
            "VALID_SOLICITATION_PACKAGE": int(pkg_c.get("VALID") or 0),
            "PARTIAL_SOLICITATION_PACKAGE": int(pkg_c.get("PARTIAL") or 0),
            "UNRELATED_DOCUMENTS": int(pkg_c.get("UNRELATED") or 0),
            "AMBIGUOUS_DOCUMENTS": int(pkg_c.get("AMBIGUOUS") or 0),
            "PACKAGE_UNAVAILABLE_FREE": int(pkg_c.get("UNAVAILABLE_FREE") or 0),
            "PACKAGE_RECOVERY_RETRYABLE": int(pkg_c.get("RETRYABLE") or 0),
            "PACKAGE_MATCH_AMBIGUOUS": int(pkg_c.get("AMBIGUOUS_MATCH") or 0),
            "NO_REAL_PACKAGE": int(pkg_c.get("NO_REAL_PACKAGE") or 0),
            "real_usable": stage_prior["real_package"],
        },
        "identity_funnel": {
            "opportunities_with_line_items": with_lines,
            "total_lines": total_lines,
            "exact_identity_lines": int(id_line_c.get("EXACT_MPN") or 0)
            + int(id_line_c.get("EXACT_MODEL") or 0)
            + int(id_line_c.get("EXACT_NSN") or 0),
            "permitted_equal_lines": int(id_line_c.get("PERMITTED_EQUAL") or 0),
            "strong_generic_lines": int(id_line_c.get("STRONG_GENERIC_SPEC") or 0),
            "weak_no_identity_lines": int(id_line_c.get("WEAK_IDENTITY") or 0)
            + int(id_line_c.get("NO_IDENTITY") or 0),
            "opportunities_with_usable_identity": stage_prior["product_identity"],
            "identity_line_counts": dict(id_line_c),
        },
        "evidence_funnel": {
            "government_value_known": gov_known,
            "acquisition_cost_known": acq_known,
            "both_sides_known": both,
        },
        "economics_funnel": {
            "economics_ready": both,
            "END_OF_FUNNEL_READY": eof_ready,
            "PROVEN_PROFITABLE": int(status_c.get("PROVEN_PROFITABLE") or 0),
            "LIKELY_PROFITABLE": int(status_c.get("LIKELY_PROFITABLE") or 0),
            "POSSIBLE_PROFIT": int(status_c.get("POSSIBLE_PROFIT") or 0),
            "UNPROVEN": int(status_c.get("UNPROVEN") or 0),
            "UNPROFITABLE": int(status_c.get("UNPROFITABLE") or 0),
            "EXECUTION_BLOCKED": int(status_c.get("EXECUTION_BLOCKED") or 0),
            "PROFITABLE_AT_PUBLIC_RETAIL": public_retail,
        },
        "profit_buckets": {k: int(buckets.get(k) or 0) for _, k in PROFIT_BUCKETS},
        "source_to_end": source_to_end,
        "drop_off": drop_rows,
        "multi_line": {
            "10plus": dict(multi["10"]),
            "20plus": dict(multi["20"]),
            "40plus": dict(multi["40"]),
        },
        "top_owner_candidates": owner_candidates,
        "questions": questions,
        "phases": {
            k: (
                {kk: vv for kk, vv in v.items() if kk not in {"per_entity", "samples"}}
                if isinstance(v, dict)
                else v
            )
            for k, v in phase_results.items()
        },
    }
    return report


def run_full_funnel_sweep(
    *,
    run_id: str | None = None,
    on_progress: Callable[..., None] | None = None,
    skip_bidnet: bool = False,
    skip_opengov: bool = False,
    skip_expansion: bool = False,
    skip_discovery: bool = False,
    bidnet_max_results: int = 25000,
    opengov_max_pages: int = 40,
    free_package_batch_size: int = 5000,
    free_package_max_batches: int = 40,
    opengov_recovery_limit: int | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    """Execute discovery → funnel end with checkpoints."""
    from m3_canonical_discovery_bridge import available_count, discovery_health_payload
    from phase_l.l23_full_population_funnel import load_store

    run_id = run_id or f"FFS-{uuid4().hex[:12]}"
    started = now_utc().isoformat()
    ck = _load_ck() if resume else {"kind": "FullFunnelSweepCheckpoint", "completed_stages": [], "phases": {}}
    completed = set(ck.get("completed_stages") or [])
    phases: dict[str, Any] = dict(ck.get("phases") or {})

    def prog(phase: str, pct: int, **extra: Any) -> None:
        if on_progress:
            try:
                on_progress(phase=phase, pct=pct, **extra)
            except Exception:
                pass

    prog("SNAPSHOT_BEFORE", 2)
    store0 = load_store()
    canonical_before = int(ck.get("canonical_before") or available_count(store0))
    ck["canonical_before"] = canonical_before
    phases["snapshot_before"] = {
        "canonical_live": canonical_before,
        "health": discovery_health_payload(),
        "at": now_utc().isoformat(),
    }
    _save_ck({**ck, "run_id": run_id, "phases": phases, "completed_stages": list(completed)})

    # --- Discovery ---
    if not skip_discovery and "discovery" not in completed:
        if not skip_bidnet:
            prog("BIDNET_HARVEST", 8)
            try:
                from bidnet_discovery import run_bidnet_partitioned_harvest

                def _bn_prog(**kw: Any) -> None:
                    prog(
                        "BIDNET_HARVEST",
                        min(28, 8 + int(kw.get("pct") or 0) // 5),
                        retrieved=kw.get("retrieved"),
                        pages=kw.get("pages"),
                        partition=kw.get("partition"),
                    )

                bn = run_bidnet_partitioned_harvest(
                    max_results=bidnet_max_results,
                    max_pages_per_partition=900,
                    include_national=True,
                    persist=True,
                    run_id=f"{run_id}-BN",
                    use_auth_seed=True,
                    on_progress=_bn_prog,
                )
                merge = bn.get("canonical_merge") if isinstance(bn.get("canonical_merge"), dict) else {}
                phases["bidnet"] = {
                    "status": "COMPLETED",
                    "reported_open_ui": bn.get("reported_open_ui"),
                    "retrieved_unique": bn.get("retrieved_unique"),
                    "retrieval_pct": bn.get("retrieval_pct"),
                    "pages_scanned": bn.get("pages_scanned"),
                    "pagination_complete": bn.get("pagination_complete"),
                    "partition_method": bn.get("partition_method"),
                    "net_new": bn.get("net_new") or merge.get("new"),
                    "existing_enriched": merge.get("updated"),
                    "errors": bn.get("errors"),
                }
            except Exception as exc:
                log.exception("BidNet harvest failed in full funnel sweep")
                phases["bidnet"] = {"status": "FAILED", "error": type(exc).__name__}
        else:
            phases["bidnet"] = {"status": "SKIPPED"}

        if not skip_opengov:
            prog("OPENGOV_CASCADE", 30)
            try:
                from opengov_discovery.cascade import run_opengov_cascade_discovery

                def _og_prog(**kw: Any) -> None:
                    prog(
                        "OPENGOV_CASCADE",
                        min(42, 30 + int(kw.get("pct") or 0) // 8),
                        entities=kw.get("entities"),
                        retrieved=kw.get("retrieved"),
                    )

                og = run_opengov_cascade_discovery(
                    max_entities=None,
                    max_pages=opengov_max_pages,
                    persist=True,
                    run_id=f"{run_id}-OG",
                    use_auth=True,
                    allow_browser=False,
                    on_progress=_og_prog,
                )
                phases["opengov"] = {
                    "status": "COMPLETED",
                    "entities_attempted": og.get("entities_attempted"),
                    "entities_successful": og.get("entities_successful"),
                    "portal_status_counts": og.get("portal_status_counts"),
                    "route_counts": og.get("route_counts"),
                    "raw_opportunities": og.get("raw_opportunities"),
                    "unique_records": og.get("unique_records"),
                    "net_new": og.get("net_new"),
                    "pagination_complete_entities": og.get("pagination_complete_entities"),
                }
            except Exception as exc:
                log.exception("OpenGov cascade failed")
                phases["opengov"] = {"status": "FAILED", "error": type(exc).__name__}
        else:
            phases["opengov"] = {"status": "SKIPPED"}

        if not skip_expansion:
            prog("EXPANSION_OTHER", 44)
            try:
                from discovery_expansion import run_expansion_harvest

                exp = run_expansion_harvest(
                    include_bidnet=False,
                    include_structured=True,
                    include_platform_catalog=True,
                    max_pages=40,
                    max_catalog_entities_per_family=30,
                    persist=True,
                )
                merge = exp.get("canonical_merge") if isinstance(exp.get("canonical_merge"), dict) else {}
                phases["expansion"] = {
                    "status": "COMPLETED",
                    "raw_opportunities": exp.get("raw_opportunities") or exp.get("raw"),
                    "net_new": exp.get("net_new") or merge.get("new"),
                    "per_family": exp.get("per_family") or exp.get("by_family"),
                    "errors": exp.get("errors"),
                }
            except Exception as exc:
                phases["expansion"] = {"status": "FAILED", "error": type(exc).__name__}
        else:
            phases["expansion"] = {"status": "SKIPPED"}

        try:
            from discovery.federal_dla_coverage import load_federal_dla_coverage

            cov = load_federal_dla_coverage()
            phases["sam"] = {"status": "SNAPSHOT", "coverage": cov}
            phases["dla"] = {"status": "SNAPSHOT", "note": "included in federal_dla coverage"}
        except Exception as exc:
            phases["sam"] = {"status": "UNAVAILABLE", "error": type(exc).__name__}
            phases["dla"] = {"status": "UNAVAILABLE", "error": type(exc).__name__}

        completed.add("discovery")
        _save_ck({**ck, "run_id": run_id, "phases": phases, "completed_stages": list(completed)})

    # --- Classify ---
    if "classify" not in completed:
        prog("UNIVERSE_CLASSIFY", 50)
        try:
            from universe_pass import run_universe_pass

            up = run_universe_pass(
                classify=True,
                freshness=True,
                profit_route=False,
                limit=None,
                resume=False,
                persist=True,
                force_reclassify=False,
                run_id=f"{run_id}-UNI",
            )
            phases["universe_pass"] = {
                "status": "COMPLETED",
                "classification": up.get("classification"),
                "after_canonical_live": up.get("after_canonical_live"),
            }
            mid = load_store()
            phases["reconcile"] = {
                "canonical_before": canonical_before,
                "canonical_after": available_count(mid),
                "raw_total": sum(
                    int((phases.get(k) or {}).get("raw_opportunities") or (phases.get(k) or {}).get("retrieved_unique") or 0)
                    for k in ("bidnet", "opengov", "expansion")
                ),
                "net_new": sum(int((phases.get(k) or {}).get("net_new") or 0) for k in ("bidnet", "opengov", "expansion")),
            }
        except Exception as exc:
            log.exception("Universe classify failed")
            phases["universe_pass"] = {"status": "FAILED", "error": type(exc).__name__}
        completed.add("classify")
        _save_ck({**ck, "run_id": run_id, "phases": phases, "completed_stages": list(completed)})

    # --- Free package universe chase (batched until empty or max batches) ---
    if "free_package" not in completed:
        prog("FREE_PACKAGE_UNIVERSE", 55)
        from bidnet_recovery.free_package_batch import (
            run_free_package_backlog,
            select_free_package_backlog,
        )
        from bidnet_recovery.free_package_batch import _load_ck as _load_fp_ck

        batch_reports: list[dict[str, Any]] = list(phases.get("free_package_batches") or [])
        for bi in range(int(free_package_max_batches)):
            store = load_store()
            done_ids: set[str] = set()
            try:
                done_ids = set((_load_fp_ck() or {}).get("processed_ids") or [])
            except Exception:
                done_ids = set()
            remaining = len(
                select_free_package_backlog(
                    store, limit=10**9, skip_ids=done_ids, universe_mode=True
                )
            )
            if remaining <= 0:
                break
            prog(
                "FREE_PACKAGE_UNIVERSE",
                min(78, 55 + bi),
                retrieved=remaining,
                batch=bi + 1,
            )

            def _fp_prog(**kw: Any) -> None:
                prog(
                    "FREE_PACKAGE_UNIVERSE",
                    min(78, 55 + bi),
                    retrieved=kw.get("retrieved"),
                    found=kw.get("found"),
                    batch=bi + 1,
                    remaining=remaining,
                )

            br = run_free_package_backlog(
                limit=int(free_package_batch_size),
                persist=True,
                resume=True,
                force=False,
                run_id=f"{run_id}-FP{bi+1}",
                on_progress=_fp_prog,
                stop_if_yield_below=None,
                min_attempted_for_yield_gate=10**9,
                universe_mode=True,
            )
            batch_reports.append(
                {
                    "batch": bi + 1,
                    "attempted": br.get("attempted"),
                    "FREE_PACKAGE_FOUND": br.get("FREE_PACKAGE_FOUND"),
                    "FALSE_PACKAGE_MATCH": br.get("FALSE_PACKAGE_MATCH"),
                    "PACKAGE_RECOVERY_RETRYABLE": br.get("PACKAGE_RECOVERY_RETRYABLE"),
                    "PACKAGE_UNAVAILABLE_FREE": br.get("PACKAGE_UNAVAILABLE_FREE"),
                    "both_sides_known": br.get("both_sides_known"),
                    "remaining_before": remaining,
                }
            )
            phases["free_package_batches"] = batch_reports
            _save_ck({**ck, "run_id": run_id, "phases": phases, "completed_stages": list(completed)})
            if int(br.get("attempted") or 0) <= 0:
                break
        phases["free_package"] = {
            "status": "COMPLETED",
            "batches": len(batch_reports),
            "totals": {
                "attempted": sum(int(b.get("attempted") or 0) for b in batch_reports),
                "FREE_PACKAGE_FOUND": sum(int(b.get("FREE_PACKAGE_FOUND") or 0) for b in batch_reports),
                "FALSE_PACKAGE_MATCH": sum(int(b.get("FALSE_PACKAGE_MATCH") or 0) for b in batch_reports),
            },
        }
        completed.add("free_package")
        _save_ck({**ck, "run_id": run_id, "phases": phases, "completed_stages": list(completed)})

    # --- OpenGov recovery ---
    if "opengov_recovery" not in completed:
        prog("OPENGOV_RECOVERY", 82)
        try:
            from opengov_recovery import run_opengov_recovery

            og_rec = run_opengov_recovery(
                limit=opengov_recovery_limit,
                batch_size=25,
                resume=True,
                persist=True,
                force=False,
                use_auth=True,
            )
            phases["opengov_recovery"] = og_rec
        except Exception as exc:
            phases["opengov_recovery"] = {"status": "FAILED", "error": type(exc).__name__}
        completed.add("opengov_recovery")
        _save_ck({**ck, "run_id": run_id, "phases": phases, "completed_stages": list(completed)})

    # --- Profit refresh on recovered packages ---
    if "profit_refresh" not in completed:
        prog("PROFIT_REFRESH", 90)
        try:
            from universe_pass import run_universe_pass

            up2 = run_universe_pass(
                classify=False,
                freshness=False,
                profit_route=True,
                limit=None,
                profit_limit=None,
                resume=True,
                persist=True,
                force_reclassify=False,
                run_id=f"{run_id}-UNI2",
            )
            phases["profit_refresh"] = {
                "product_economics": up2.get("product_economics"),
                "after_canonical_live": up2.get("after_canonical_live"),
            }
        except Exception as exc:
            phases["profit_refresh"] = {"status": "FAILED", "error": type(exc).__name__}
        completed.add("profit_refresh")
        _save_ck({**ck, "run_id": run_id, "phases": phases, "completed_stages": list(completed)})

    prog("BUILD_REPORT", 96)
    report = build_funnel_completion_report(
        phase_results=phases,
        canonical_before=canonical_before,
        run_id=run_id,
    )
    report["started_at"] = started
    report["completed_stages"] = list(completed)
    _save_report(report)
    completed.add("report")
    _save_ck({**ck, "run_id": run_id, "phases": phases, "completed_stages": list(completed), "status": "COMPLETED"})
    prog("DONE", 100)
    return report


def format_owner_summary(report: dict[str, Any]) -> str:
    """Exact text block for owner completion report."""
    fu = report.get("full_universe") or {}
    sc = report.get("source_counts") or {}
    pf = report.get("product_funnel") or {}
    pkg = report.get("package_funnel") or {}
    ident = report.get("identity_funnel") or {}
    ev = report.get("evidence_funnel") or {}
    eco = report.get("economics_funnel") or {}
    buckets = report.get("profit_buckets") or {}
    src = report.get("source_to_end") or {}
    drops = report.get("drop_off") or []
    owners = report.get("top_owner_candidates") or []

    def _src_count(name: str) -> Any:
        block = sc.get(name) if isinstance(sc.get(name), dict) else {}
        return (
            block.get("retrieved_unique")
            or block.get("raw_opportunities")
            or block.get("unique_records")
            or (src.get(name) or {}).get("live")
            or 0
        )

    lines = [
        "FULL UNIVERSE",
        "",
        f"Canonical before: {fu.get('canonical_before')}",
        f"Raw discovered: {fu.get('raw_discovered')}",
        f"Net-new: {fu.get('net_new')}",
        f"Canonical after: {fu.get('canonical_after')}",
        "",
        "SOURCE COUNTS",
        "",
        f"BidNet: {_src_count('BidNet')}",
        f"OpenGov: {_src_count('OpenGov')}",
        f"SAM: {_src_count('SAM')}",
        f"DLA: {_src_count('DLA')}",
        f"Other: {_src_count('Other')}",
        "",
        "PRODUCT FUNNEL",
        "",
        f"Tangible: {pf.get('TANGIBLE_PRODUCT')}",
        f"Likely product: {pf.get('LIKELY_PRODUCT')}",
        f"Mixed: {pf.get('MIXED')}",
        f"Service: {pf.get('SERVICE')}",
        f"Unknown: {pf.get('UNKNOWN')}",
        f"Product-eligible: {pf.get('product_eligible')}",
        "",
        "PACKAGE FUNNEL",
        "",
        f"Package recovery attempted: {pkg.get('attempted')}",
        f"Valid solicitation packages: {pkg.get('VALID_SOLICITATION_PACKAGE')}",
        f"Partial solicitation packages: {pkg.get('PARTIAL_SOLICITATION_PACKAGE')}",
        f"Unrelated/false docs: {pkg.get('UNRELATED_DOCUMENTS')}",
        f"Unavailable free: {pkg.get('PACKAGE_UNAVAILABLE_FREE')}",
        f"Retryable: {pkg.get('PACKAGE_RECOVERY_RETRYABLE')}",
        f"Ambiguous: {pkg.get('AMBIGUOUS_DOCUMENTS')}",
        "",
        "IDENTITY FUNNEL",
        "",
        f"Opportunities with line items: {ident.get('opportunities_with_line_items')}",
        f"Exact identity: {ident.get('exact_identity_lines')}",
        f"Permitted equal: {ident.get('permitted_equal_lines')}",
        f"Strong generic: {ident.get('strong_generic_lines')}",
        f"Weak/no identity: {ident.get('weak_no_identity_lines')}",
        "",
        "EVIDENCE FUNNEL",
        "",
        f"Government-value-known: {ev.get('government_value_known')}",
        f"Acquisition-cost-known: {ev.get('acquisition_cost_known')}",
        f"Both-sides-known: {ev.get('both_sides_known')}",
        "",
        "ECONOMICS FUNNEL",
        "",
        f"Economics-ready: {eco.get('economics_ready')}",
        f"END_OF_FUNNEL_READY: {eco.get('END_OF_FUNNEL_READY')}",
        f"PROVEN_PROFITABLE: {eco.get('PROVEN_PROFITABLE')}",
        f"LIKELY_PROFITABLE: {eco.get('LIKELY_PROFITABLE')}",
        f"POSSIBLE_PROFIT: {eco.get('POSSIBLE_PROFIT')}",
        f"UNPROVEN: {eco.get('UNPROVEN')}",
        f"UNPROFITABLE: {eco.get('UNPROFITABLE')}",
        f"EXECUTION_BLOCKED: {eco.get('EXECUTION_BLOCKED')}",
        f"PROFITABLE_AT_PUBLIC_RETAIL: {eco.get('PROFITABLE_AT_PUBLIC_RETAIL')}",
        "",
        "PROFIT BUCKETS",
        "",
        f">$0: {buckets.get('gt_0')}",
        f">=$1K: {buckets.get('gte_1k')}",
        f">=$2.5K: {buckets.get('gte_2_5k')}",
        f">=$5K: {buckets.get('gte_5k')}",
        f">=$10K: {buckets.get('gte_10k')}",
        f">=$25K: {buckets.get('gte_25k')}",
        f">=$50K: {buckets.get('gte_50k')}",
        f">=$75K: {buckets.get('gte_75k')}",
        "",
        "SOURCE TO END",
        "",
    ]
    for name in ("BidNet", "OpenGov", "SAM", "DLA", "Other"):
        row = src.get(name) or {}
        lines.append(
            f"{name}: live={row.get('live')} prod={row.get('product_eligible')} "
            f"pkg={row.get('real_packages')} id={row.get('product_identified')} "
            f"gov={row.get('gov_value_known')} acq={row.get('acquisition_cost_known')} "
            f"both={row.get('both_sides_known')} eof={row.get('end_of_funnel_ready')} "
            f"prof={row.get('profitable')} >=10k={row.get('gte_10k_profit')}"
        )
    lines += ["", "TOP DROP-OFF REASONS", ""]
    for d in drops[:12]:
        lines.append(f"{d.get('reason')}: {d.get('count')} ({d.get('pct_of_product_eligible')}%)")
    lines += ["", "TOP OWNER CANDIDATES", ""]
    if not owners:
        lines.append("(none)")
    for o in owners:
        lines.append(
            f"- [{o.get('profit_status')}] {o.get('title')} | {o.get('buyer')} | {o.get('source')} | "
            f"profit={o.get('expected_profit')} | rev={o.get('expected_revenue')} | "
            f"cost={o.get('product_cost')} | next={o.get('next_action')}"
        )
    q = report.get("questions") or {}
    lines += ["", "ANSWERS", ""]
    for k in sorted(q.keys()):
        lines.append(f"{k}: {q[k]}")
    return "\n".join(lines)
