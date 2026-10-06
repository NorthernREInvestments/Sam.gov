"""Per-opportunity canonical funnel evaluation — fail-closed, cache-first, no invented economics."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from large_production_test.models import DROP_CLASS, STAGES
from m3_data_root import data_path


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


class EvidenceIndex:
    """Lazy-ish caches for large stores loaded once per run."""

    def __init__(self) -> None:
        self.identity = (_load_identity())
        self.elig = _load("m3_eligibility_file_mine_store.json").get("by_opportunity") or {}
        self.elig_audit = (
            _load("m3_eligibility_applicability_audit_store.json").get("by_opportunity") or {}
        )
        self.revenue = _load("m3_revenue_evidence_v1_store.json").get("by_opportunity") or {}
        self.acq = _load("m3_acquisition_scale_v1_checkpoint.json").get("by_opportunity") or {}
        self.ee = _load("m3_evidence_exhaustion_v1_checkpoint.json").get("by_opportunity") or {}
        self.basket = (
            _load("m3_basket_full_funnel_reconcile_v1_checkpoint.json").get("opportunities") or {}
        )
        self.mlr = (
            _load("m3_material_line_identity_price_recovery_v1_checkpoint.json").get("opportunities")
            or {}
        )
        self.bid_ready = (_load("m3_bid_ready_state_v1.json").get("by_opportunity") or {})
        self.pkg_index = (_load("m3_package_provenance_index_v1.json").get("opportunities") or {})
        self.line_econ = _load("m3_line_item_economics_store.json").get("by_opportunity") or {}
        self.scale_profit = (
            _load("m3_scale_evidence_profit_store.json").get("by_opportunity") or {}
        )

    def package_folder(self, oid: str) -> Path | None:
        if not oid.startswith("opengov:"):
            return None
        parts = oid.split(":")
        if len(parts) < 3:
            return None
        folder = data_path(f"opengov_public_docs/documents/{parts[1]}/{parts[2]}")
        return folder if folder.exists() else None


def _load_identity() -> dict[str, Any]:
    try:
        from evidence_breakthrough.corpus import load_identity_store

        return load_identity_store().get("by_opportunity") or {}
    except Exception:
        return {}


def _identity_grade(ident: dict[str, Any]) -> str:
    g = str(ident.get("confidence_grade") or ident.get("grade") or "").upper()
    if g in {"A", "B", "C", "D", "E", "F", "G"}:
        return g
    if ident.get("part_number") or ident.get("mpn") or ident.get("exact_mpn"):
        return "A"
    if ident.get("model") or ident.get("exact_model"):
        return "B"
    if ident.get("nsn"):
        return "C"
    if ident.get("brand_or_equal") or ident.get("permitted_equal"):
        return "D"
    if ident.get("generic_spec") or (ident.get("raw_description") and len(str(ident.get("raw_description"))) > 40):
        return "E"
    if ident.get("family_only"):
        return "F"
    return "G"


def _usable_identity(grade: str) -> bool:
    return grade in {"A", "B", "C", "D", "E"}


def _revenue_usable(rev_row: dict[str, Any], acq_row: dict[str, Any]) -> dict[str, Any]:
    rev = rev_row.get("revenue") if isinstance(rev_row.get("revenue"), dict) else rev_row
    types = set(rev.get("evidence_types") or [])
    # Hard exclude contamination categories
    contaminated = False
    notes = []
    for t in types:
        tu = str(t).upper()
        if any(x in tu for x in ("BOND", "INSURANCE_THRESHOLD", "GRANT_TOTAL", "BUYER_CATEGORY")):
            contaminated = True
            notes.append(tu)
    has_def = bool(
        rev.get("has_defensible_revenue")
        or rev.get("defensible")
        or rev.get("has_strong_r1_r3")
        or acq_row.get("both_sides")
        or acq_row.get("revenue_ref")
    )
    value = None
    for key in ("value", "current_value", "expected_revenue"):
        if rev.get(key) is not None:
            try:
                value = float(rev.get(key))
                break
            except (TypeError, ValueError):
                pass
    if value is None and isinstance(acq_row.get("revenue_ref"), dict):
        try:
            value = float((acq_row.get("revenue_ref") or {}).get("value"))
        except (TypeError, ValueError):
            value = None
    # ECONOMIC_REVENUE_USABLE requires defensible non-contaminated current-ish evidence
    usable = bool(has_def and not contaminated and value is not None and value > 0)
    # If strong flag without numeric value — still not ECONOMICS usable for profit bands
    return {
        "ECONOMIC_REVENUE_USABLE": "YES" if usable else "NO",
        "has_defensible": has_def,
        "contaminated": contaminated,
        "contamination_notes": notes,
        "value": value,
        "types": sorted(types),
    }


def evaluate_opportunity(meta: dict[str, Any], idx: EvidenceIndex, *, budgets: dict[str, Any]) -> dict[str, Any]:
    oid = meta["opportunity_id"]
    stages_hit: list[str] = ["DISCOVERED", "CANONICALIZED"]
    drop_reason = None
    exit_bucket = "ADVANCED"
    stage_exits: dict[str, dict[str, Any]] = {}

    # PRODUCT
    if meta.get("universe_class") in {"PURE_SERVICE", "CONSTRUCTION"}:
        drop_reason = "NOT_PRODUCT"
        exit_bucket = "TERMINAL"
        return _finalize(meta, stages_hit, drop_reason, exit_bucket, {}, idx, budgets)
    stages_hit.append("PRODUCT_QUALIFIED")

    # PACKAGE
    pkg_folder = idx.package_folder(oid) if oid.startswith("opengov:") else None
    pkg_row = idx.pkg_index.get(oid) or {}
    docs = []
    if pkg_folder and pkg_folder.exists():
        docs = [f for f in pkg_folder.iterdir() if f.is_file() and not f.name.endswith(".meta.json")]
    if docs or pkg_row.get("documents") or meta.get("package_backed"):
        stages_hit.append("PACKAGE_ACQUIRED")
        # Provenance gate — COMPLETE status or all docs hashed
        status = pkg_row.get("status")
        doc_recs = pkg_row.get("documents") or []
        if status == "PACKAGE_PROVENANCE_COMPLETE":
            stages_hit.append("PACKAGE_VERIFIED")
        elif doc_recs and all(d.get("content_hash") for d in doc_recs):
            stages_hit.append("PACKAGE_VERIFIED")
        elif docs:
            hashed = 0
            for f in docs:
                side = f.with_suffix(f.suffix + ".meta.json")
                if side.exists():
                    try:
                        meta_j = json.loads(side.read_text(encoding="utf-8"))
                        if meta_j.get("content_hash"):
                            hashed += 1
                    except Exception:
                        pass
            if hashed == len(docs) and len(docs) > 0:
                stages_hit.append("PACKAGE_VERIFIED")
            else:
                drop_reason = "PACKAGE_INCOMPLETE"
                exit_bucket = "RETRYABLE"
        else:
            drop_reason = "PACKAGE_INCOMPLETE"
            exit_bucket = "RETRYABLE"
    else:
        drop_reason = "PACKAGE_UNAVAILABLE_FREE"
        exit_bucket = "RETRYABLE"
        return _finalize(meta, stages_hit, drop_reason, exit_bucket, {"package_docs": 0}, idx, budgets)

    if "PACKAGE_VERIFIED" not in stages_hit:
        return _finalize(
            meta,
            stages_hit,
            drop_reason or "PACKAGE_INCOMPLETE",
            exit_bucket,
            {"package_docs": len(docs)},
            idx,
            budgets,
        )

    # ELIGIBILITY
    elig = idx.elig.get(oid) or idx.elig_audit.get(oid) or {}
    estatus = (
        elig.get("eligibility_status")
        or elig.get("status")
        or elig.get("result")
        or meta.get("eligibility_pre")
        or "ELIGIBILITY_UNKNOWN"
    )
    estatus_u = str(estatus).upper()
    if "INELIGIBLE" in estatus_u or estatus_u == "BID_INELIGIBLE":
        drop_reason = "BID_INELIGIBLE"
        exit_bucket = "TERMINAL"
        return _finalize(meta, stages_hit, drop_reason, exit_bucket, {"eligibility": estatus}, idx, budgets)
    if "ACTION" in estatus_u or estatus_u == "BID_ELIGIBLE_WITH_ACTION":
        # can continue but mark owner action — still "cleared with action" for funnel advance
        stages_hit.append("ELIGIBILITY_CLEARED")
        eligibility_class = "BID_ELIGIBLE_WITH_ACTION"
    elif "ELIGIBLE" in estatus_u or estatus_u in {"PASS", "CLEARED", "BID_ELIGIBLE"}:
        stages_hit.append("ELIGIBILITY_CLEARED")
        eligibility_class = "BID_ELIGIBLE"
    elif estatus_u in {"ELIGIBILITY_UNKNOWN", "UNKNOWN", ""}:
        # Do not implicit-pass; stop
        drop_reason = "ELIGIBILITY_UNKNOWN"
        exit_bucket = "RETRYABLE"
        return _finalize(meta, stages_hit, drop_reason, exit_bucket, {"eligibility": estatus}, idx, budgets)
    else:
        stages_hit.append("ELIGIBILITY_CLEARED")
        eligibility_class = estatus_u

    # LINES / IDENTITY
    id_pack = idx.identity.get(oid) or {}
    identities = [i for i in (id_pack.get("identities") or []) if isinstance(i, dict)]
    mlr = idx.mlr.get(oid) or {}
    line_econ = idx.line_econ.get(oid) or {}
    total_lines = (
        len(identities)
        or int((line_econ.get("extraction") or {}).get("line_count") or 0)
        or int((mlr.get("owner_view") or {}).get("material_lines") or 0)
        or int(meta.get("identity_count") or 0)
    )
    if total_lines <= 0 and not identities:
        drop_reason = "NO_LINES"
        exit_bucket = "RETRYABLE"
        return _finalize(meta, stages_hit, drop_reason, exit_bucket, {"eligibility": eligibility_class}, idx, budgets)

    stages_hit.append("LINES_EXTRACTED")

    grades = [_identity_grade(i) for i in identities] if identities else []
    usable = [g for g in grades if _usable_identity(g)]
    ambiguous = sum(1 for g in grades if g in {"F", "G"})
    # Materiality heuristic: P0 = A/B with PN, P1 = C/D/E, else lower
    p0 = sum(1 for i, g in zip(identities, grades) if g in {"A", "B"})
    p1 = sum(1 for g in grades if g in {"C", "D", "E"})
    if usable:
        stages_hit.append("COMMERCIAL_IDENTITY_READY")
    else:
        drop_reason = "IDENTITY_AMBIGUOUS" if ambiguous or identities else "WEAK_IDENTITY"
        exit_bucket = "RETRYABLE" if drop_reason == "IDENTITY_AMBIGUOUS" else "TERMINAL"
        return _finalize(
            meta,
            stages_hit,
            drop_reason,
            exit_bucket,
            {
                "eligibility": eligibility_class,
                "total_lines": total_lines,
                "p0": p0,
                "p1": p1,
                "usable_identities": len(usable),
                "ambiguous": ambiguous,
            },
            idx,
            budgets,
        )

    # REVENUE
    rev_info = _revenue_usable(idx.revenue.get(oid) or {}, idx.acq.get(oid) or {})
    if rev_info["ECONOMIC_REVENUE_USABLE"] == "YES" or rev_info["has_defensible"]:
        if rev_info["contaminated"]:
            # contamination blocks economic use
            drop_reason = "NO_USABLE_REVENUE"
            exit_bucket = "TERMINAL"
            return _finalize(meta, stages_hit, drop_reason, exit_bucket, {"revenue": rev_info}, idx, budgets)
        stages_hit.append("REVENUE_EVIDENCE_READY")
    else:
        drop_reason = "NO_USABLE_REVENUE"
        exit_bucket = "RETRYABLE"
        return _finalize(
            meta,
            stages_hit,
            drop_reason,
            exit_bucket,
            {
                "eligibility": eligibility_class,
                "total_lines": total_lines,
                "usable_identities": len(usable),
                "revenue": rev_info,
            },
            idx,
            budgets,
        )

    # ACQUISITION / QUOTE
    acq = idx.acq.get(oid) or {}
    ee = idx.ee.get(oid) or {}
    basket = idx.basket.get(oid) or {}
    lines_priced = int(acq.get("lines_priced") or ee.get("cost_lines") or 0)
    priced_from_basket = int((basket.get("lines") or {}).get("PRICED_LINES") or 0)
    quote_lines = int((basket.get("lines") or {}).get("QUOTE_REQUIRED_LINES") or 0)
    public_ready = lines_priced > 0 or priced_from_basket > 0
    quote_required = False
    if public_ready:
        stages_hit.append("ACQUISITION_COST_READY")
    else:
        # Strong identity + revenue but no public price → quote reserve (legitimate)
        quote_required = True
        stages_hit.append("QUOTE_REQUIRED")
        drop_reason = "QUOTE_REQUIRED"
        exit_bucket = "QUOTE_RESERVE"
        # Continue to measure basket/quote burden but do NOT mark acquisition-ready

    # BASKET coverage
    coverage = float(acq.get("coverage") or 0)
    if basket.get("basket"):
        coverage = max(coverage, float((basket.get("basket") or {}).get("line_coverage") or 0))
    if not coverage and total_lines and (lines_priced or priced_from_basket):
        coverage = (lines_priced or priced_from_basket) / max(total_lines, 1)
    material_cov = {
        "gte_25": coverage >= 0.25,
        "gte_50": coverage >= 0.50,
        "gte_75": coverage >= 0.75,
        "gte_90": coverage >= 0.90,
        "eq_100": coverage >= 0.999,
        "coverage": round(coverage, 4),
    }
    basket_class = (basket.get("basket") or {}).get("basket_class")
    basket_ready = basket_class in {
        "BASKET_READY_PUBLIC_PRICE",
        "BASKET_READY_QUOTE_DEPENDENT",
    } or (coverage >= 0.9 and public_ready and not quote_required)

    if quote_required and not public_ready:
        # Quote reserve terminal for this path — still report coverage
        return _finalize(
            meta,
            stages_hit,
            drop_reason,
            exit_bucket,
            {
                "eligibility": eligibility_class,
                "total_lines": total_lines,
                "p0": p0,
                "p1": p1,
                "usable_identities": len(usable),
                "ambiguous": ambiguous,
                "production_public_prices": lines_priced or priced_from_basket,
                "quote_required_lines": max(quote_lines, len(usable)),
                "revenue": rev_info,
                "material_coverage": material_cov,
                "quote_required": True,
            },
            idx,
            budgets,
        )

    if basket_ready:
        stages_hit.append("BASKET_READY")
    else:
        drop_reason = "INSUFFICIENT_BASKET_COVERAGE"
        exit_bucket = "RETRYABLE"
        return _finalize(
            meta,
            stages_hit,
            drop_reason,
            exit_bucket,
            {
                "eligibility": eligibility_class,
                "total_lines": total_lines,
                "usable_identities": len(usable),
                "production_public_prices": lines_priced or priced_from_basket,
                "revenue": rev_info,
                "material_coverage": material_cov,
            },
            idx,
            budgets,
        )

    # FREIGHT / FINANCING
    freight = (basket.get("economics") or {}).get("freight_class") or "FREIGHT_ESTIMATED_CONSERVATIVE"
    if freight == "FREIGHT_NOT_READY":
        drop_reason = "FREIGHT_NOT_READY"
        exit_bucket = "RETRYABLE"
        return _finalize(meta, stages_hit, drop_reason, exit_bucket, {}, idx, budgets)
    stages_hit.append("FREIGHT_READY")

    financing = (basket.get("economics") or {}).get("financing_class") or acq.get("financing") or "FINANCEABLE_MODELED"
    if financing == "FINANCING_BLOCKED":
        drop_reason = "FINANCING_BLOCKED"
        exit_bucket = "TERMINAL"
        return _finalize(meta, stages_hit, drop_reason, exit_bucket, {}, idx, budgets)
    stages_hit.append("FINANCING_READY")

    # ECONOMICS — fail closed
    econ = basket.get("economics") or {}
    profit = econ.get("net_expected_profit")
    if profit is None:
        profit = acq.get("profit")
    terminal = econ.get("economic_terminal") or acq.get("profit_status")
    economics_ready = False
    profit_class = "PROFIT_UNPROVEN"
    if rev_info["ECONOMIC_REVENUE_USABLE"] == "YES" and basket_ready and profit is not None:
        economics_ready = True
        stages_hit.append("ECONOMICS_READY")
        try:
            pf = float(profit)
        except (TypeError, ValueError):
            pf = None
            economics_ready = False
        if pf is not None:
            if terminal in {"PROVEN_PROFITABLE", "PROFIT_PROVEN"} or (
                pf > 0 and lines_priced > 0 and coverage >= 0.9
            ):
                # Only PROVEN if explicit proven terminal or both-sides high coverage
                if terminal in {"PROVEN_PROFITABLE", "PROFIT_PROVEN"} or acq.get("both_sides"):
                    profit_class = "PROFIT_PROVEN"
                else:
                    profit_class = "PROFIT_LIKELY"
            elif pf > 0:
                profit_class = "PROFIT_POSSIBLE"
            else:
                profit_class = "UNPROFITABLE"
                drop_reason = "UNPROFITABLE"
                exit_bucket = "TERMINAL"
    else:
        drop_reason = "INSUFFICIENT_BASKET_COVERAGE" if not basket_ready else "NO_USABLE_REVENUE"
        exit_bucket = "RETRYABLE"
        return _finalize(
            meta,
            stages_hit,
            drop_reason,
            exit_bucket,
            {
                "revenue": rev_info,
                "material_coverage": material_cov,
                "profit": profit,
                "profit_class": profit_class,
                "economics_ready": False,
            },
            idx,
            budgets,
        )

    if profit_class == "UNPROFITABLE":
        return _finalize(
            meta,
            stages_hit,
            "UNPROFITABLE",
            "TERMINAL",
            {"profit": profit, "profit_class": profit_class, "economics_ready": True},
            idx,
            budgets,
        )

    stages_hit.append("EXECUTION_CHECKED")

    # LENDER_READY — not merely positive profit
    lender_ready = (
        economics_ready
        and profit_class in {"PROFIT_PROVEN", "PROFIT_LIKELY"}
        and rev_info["ECONOMIC_REVENUE_USABLE"] == "YES"
        and basket_ready
        and "PACKAGE_VERIFIED" in stages_hit
        and financing not in {"FINANCING_BLOCKED", "FINANCING_UNKNOWN"}
    )
    if lender_ready:
        stages_hit.append("LENDER_READY")

    # BID_READY — use stored 17/17 only if true; else evaluate lightly without override
    br = idx.bid_ready.get(oid) or {}
    bid_ready = bool(br.get("BID_READY"))
    if bid_ready and lender_ready:
        stages_hit.append("BID_READY")
    elif not bid_ready:
        drop_reason = drop_reason or "BID_NOT_READY"
        exit_bucket = "OWNER_ACTION_REQUIRED"

    return _finalize(
        meta,
        stages_hit,
        drop_reason,
        exit_bucket,
        {
            "eligibility": eligibility_class,
            "total_lines": total_lines,
            "p0": p0,
            "p1": p1,
            "usable_identities": len(usable),
            "ambiguous": ambiguous,
            "production_public_prices": lines_priced or priced_from_basket,
            "quote_required_lines": quote_lines,
            "revenue": rev_info,
            "material_coverage": material_cov,
            "profit": profit,
            "profit_class": profit_class,
            "economics_ready": economics_ready,
            "freight": freight,
            "financing": financing,
            "bid_ready": bid_ready,
            "lender_ready": lender_ready,
            "package_docs": len(docs),
        },
        idx,
        budgets,
    )


def _finalize(
    meta: dict[str, Any],
    stages_hit: list[str],
    drop_reason: str | None,
    exit_bucket: str,
    detail: dict[str, Any],
    idx: EvidenceIndex,
    budgets: dict[str, Any],
) -> dict[str, Any]:
    oid = meta["opportunity_id"]
    furthest = stages_hit[-1] if stages_hit else "DISCOVERED"
    # Stage conservation: entered each hit stage; exit from furthest
    if drop_reason and exit_bucket == "ADVANCED":
        exit_bucket = DROP_CLASS.get(drop_reason, "BLOCKED")

    next_action = _next_action(drop_reason, furthest, detail)
    return {
        "opportunity_id": oid,
        "buyer": meta.get("buyer"),
        "title": meta.get("title"),
        "deadline": meta.get("deadline"),
        "source_bucket": meta.get("source_bucket"),
        "jurisdiction_bucket": meta.get("jurisdiction_bucket"),
        "category_bucket": meta.get("category_bucket"),
        "line_bucket": meta.get("line_bucket"),
        "package_backed": meta.get("package_backed"),
        "stages_hit": stages_hit,
        "furthest": furthest,
        "drop_reason": drop_reason,
        "exit_bucket": exit_bucket,
        "drop_class": DROP_CLASS.get(drop_reason) if drop_reason else None,
        "detail": detail,
        "next_action": next_action,
        "budgets_snapshot": {
            "sam_calls": budgets.get("sam_calls", 0),
            "ai_calls": budgets.get("ai_calls", 0),
        },
    }


def _next_action(drop: str | None, furthest: str, detail: dict[str, Any]) -> str:
    if drop == "PACKAGE_UNAVAILABLE_FREE":
        return "Recover free package or mark portal-auth required"
    if drop == "PACKAGE_INCOMPLETE":
        return "Complete package provenance (hash/URL/amendments)"
    if drop == "ELIGIBILITY_UNKNOWN":
        return "Run eligibility engine on package"
    if drop == "BID_INELIGIBLE":
        return "Skip — fatal eligibility blocker"
    if drop == "IDENTITY_AMBIGUOUS":
        return "Clarify manufacturer/MPN on material lines"
    if drop == "NO_USABLE_REVENUE":
        return "Find current line/basket/contract value evidence"
    if drop == "QUOTE_REQUIRED":
        return "Create owner channel quote packet (no auto-send)"
    if drop == "INSUFFICIENT_BASKET_COVERAGE":
        return "Price remaining material lines or obtain quotes"
    if drop == "BID_NOT_READY":
        return "Clear remaining BID_READY 17/17 gates"
    if furthest == "BID_READY":
        return "Owner bid prep / submission review"
    return f"Advance from {furthest}"
