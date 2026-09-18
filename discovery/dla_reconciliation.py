"""DLA source reconciliation, coverage matrix, sample gap test, gap queue."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from federal_dla_constants import (
    DLA_COVERAGE_ACCESS_CONSTRAINED,
    DLA_KNOWN_GAPS,
    DLA_PUBLIC_COVERAGE_PARTIAL,
    DLA_PUBLIC_COVERAGE_RECONCILED,
    GAP_DLA_DIBBS_AUTOMATION_BLOCKED,
    GAP_DLA_FAMILY_UNDERREPRESENTED,
    GAP_DLA_SAM_RECONCILIATION_GAP,
    GAP_FEDERAL_HANDOFF_BACKLOG,
    GAP_PIEE_AUTH_REQUIRED,
    GAP_SAM_COUNT_MISMATCH,
    GAP_SAM_PAGINATION_INCOMPLETE,
    REL_DIBBS_ONLY,
    REL_MULTI_SOURCE,
    REL_OTHER_OFFICIAL_DLA_ONLY,
    REL_SAM_AND_DIBBS,
    REL_SAM_ONLY,
    REL_UNKNOWN,
)

SPE_RE = re.compile(r"\b(SPE[0-9A-Z]{2,8}[-]?[0-9A-Z\-]{2,20})\b", re.I)
SPR_RE = re.compile(r"\b(SPR[0-9A-Z]{2,8}[-]?[0-9A-Z\-]{2,20})\b", re.I)
NSN_RE = re.compile(r"\b(\d{4}-\d{2}-\d{3}-\d{4})\b")


def _sol_key(row: dict[str, Any]) -> str:
    sol = str(row.get("solicitation_number") or row.get("external_id") or "").upper().strip()
    sol = re.sub(r"[^A-Z0-9]", "", sol)
    return sol[:40]


def _family(row: dict[str, Any]) -> str:
    sol = str(row.get("solicitation_number") or row.get("external_id") or "").upper()
    m = re.match(r"(SPE[0-9A-Z]{0,4}|SPR[0-9A-Z]{0,4})", sol)
    if m:
        return m.group(1)[:6]
    path = str(row.get("agency") or (row.get("organization") or {}).get("organization_path") or "").upper()
    if "LAND AND MARITIME" in path or "LAND-AND-MARITIME" in path:
        return "DLA_LAND_AND_MARITIME"
    if "AVIATION" in path:
        return "DLA_AVIATION"
    if "TROOP SUPPORT" in path:
        return "DLA_TROOP_SUPPORT"
    if "DISTRIBUTION" in path:
        return "DLA_DISTRIBUTION"
    if "DISPOSITION" in path:
        return "DLA_DISPOSITION"
    if "ENERGY" in path:
        return "DLA_ENERGY"
    return "DLA_OTHER"


def classify_dla_relationship(
    *,
    in_sam: bool,
    in_dibbs: bool,
    in_other_official: bool = False,
) -> str:
    if in_sam and in_dibbs:
        return REL_SAM_AND_DIBBS
    if in_sam and in_other_official:
        return REL_MULTI_SOURCE
    if in_sam:
        return REL_SAM_ONLY
    if in_dibbs:
        return REL_DIBBS_ONLY
    if in_other_official:
        return REL_OTHER_OFFICIAL_DLA_ONLY
    return REL_UNKNOWN


def build_dla_from_sam(opportunities: list[dict[str, Any]]) -> dict[str, Any]:
    dla_rows = [r for r in opportunities if r.get("is_dla") or "DEFENSE LOGISTICS" in str(r.get("agency") or "").upper()]
    from discovery.dla_product_extract import enrich_with_dla_structure, classify_federal_product_cheap

    enriched = []
    nsn = pn = qty = approved = bid_ready = product_likely = 0
    by_family: Counter[str] = Counter()
    for r in dla_rows:
        e = enrich_with_dla_structure(r)
        screen = classify_federal_product_cheap(e)
        e.update(screen)
        struct = e.get("dla_product_structure") or {}
        if struct.get("has_exact_nsn"):
            nsn += 1
        if struct.get("has_exact_pn"):
            pn += 1
        if struct.get("has_quantity"):
            qty += 1
        if struct.get("approved_source_signal"):
            approved += 1
        if e.get("bid_quote_ready") or e.get("notice_semantic_class") == "BID_OR_QUOTE_READY":
            bid_ready += 1
        if screen.get("federal_product_class") in {
            "FEDERAL_PRODUCT_LIKELY",
            "FEDERAL_PRODUCT_PLUS_MINOR_SERVICE",
        }:
            product_likely += 1
        by_family[_family(e)] += 1
        enriched.append(e)
    return {
        "kind": "DLA_FROM_SAM",
        "current_unique": len(enriched),
        "bid_ready": bid_ready,
        "product_likely": product_likely,
        "exact_nsn": nsn,
        "exact_pn": pn,
        "quantity": qty,
        "approved_source": approved,
        "by_family": dict(by_family.most_common()),
        "opportunities": enriched,
    }


def build_dla_source_reconciliation(
    *,
    sam_dla: list[dict[str, Any]],
    dibbs_rows: list[dict[str, Any]] | None = None,
    other_official: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    dibbs_rows = dibbs_rows or []
    other_official = other_official or []
    sam_keys = {_sol_key(r) for r in sam_dla if _sol_key(r)}
    dibbs_keys = {_sol_key(r) for r in dibbs_rows if _sol_key(r)}
    other_keys = {_sol_key(r) for r in other_official if _sol_key(r)}
    all_keys = sam_keys | dibbs_keys | other_keys
    rel_counts: Counter[str] = Counter()
    rows = []
    for key in sorted(all_keys):
        in_sam = key in sam_keys
        in_dibbs = key in dibbs_keys
        in_other = key in other_keys
        rel = classify_dla_relationship(in_sam=in_sam, in_dibbs=in_dibbs, in_other_official=in_other)
        rel_counts[rel] += 1
        rows.append(
            {
                "key": key,
                "relationship": rel,
                "in_sam": in_sam,
                "in_dibbs": in_dibbs,
                "in_other_official": in_other,
            }
        )
    return {
        "kind": "DLA_SOURCE_RECONCILIATION",
        "total_unique_keys": len(all_keys),
        "relationship_counts": dict(rel_counts),
        "sam_only": rel_counts[REL_SAM_ONLY],
        "dibbs_only": rel_counts[REL_DIBBS_ONLY],
        "sam_and_dibbs": rel_counts[REL_SAM_AND_DIBBS],
        "other_official_only": rel_counts[REL_OTHER_OFFICIAL_DLA_ONLY],
        "multi_source": rel_counts[REL_MULTI_SOURCE],
        "unknown_relationship": rel_counts[REL_UNKNOWN],
        "sample_rows": rows[:100],
    }


def build_dla_coverage_matrix(
    *,
    sam_dla: list[dict[str, Any]],
    dibbs_rows: list[dict[str, Any]] | None = None,
    dibbs_access_state: str | None = None,
) -> list[dict[str, Any]]:
    dibbs_rows = dibbs_rows or []
    by_fam_sam: dict[str, list] = defaultdict(list)
    by_fam_dibbs: dict[str, list] = defaultdict(list)
    for r in sam_dla:
        by_fam_sam[_family(r)].append(r)
    for r in dibbs_rows:
        by_fam_dibbs[_family(r)].append(r)
    families = sorted(set(by_fam_sam) | set(by_fam_dibbs))
    out = []
    for fam in families:
        s_keys = {_sol_key(r) for r in by_fam_sam.get(fam, []) if _sol_key(r)}
        d_keys = {_sol_key(r) for r in by_fam_dibbs.get(fam, []) if _sol_key(r)}
        overlap = s_keys & d_keys
        product = sum(
            1
            for r in by_fam_sam.get(fam, [])
            if (r.get("federal_product_class") or "").startswith("FEDERAL_PRODUCT")
            or r.get("product_classification") == "CORE_PRODUCT"
        )
        out.append(
            {
                "family_activity": fam,
                "sam": len(s_keys),
                "dla_specific": len(d_keys),
                "overlap": len(overlap),
                "m3": len(s_keys | d_keys),
                "missing_from_m3_if_dibbs_only": len(d_keys - s_keys),
                "product_count": product,
                "access_state": dibbs_access_state or "UNKNOWN",
            }
        )
    out.sort(key=lambda x: -int(x["m3"]))
    return out


def run_dla_coverage_sample(
    *,
    sample_ids: list[str],
    sam_dla: list[dict[str, Any]],
    m3_rows: list[dict[str, Any]] | None = None,
    dibbs_accessible: bool = False,
) -> dict[str, Any]:
    """Bounded empirical gap test — do NOT extrapolate national %."""
    sam_keys = {_sol_key(r) for r in sam_dla}
    m3_keys = {_sol_key(r) for r in (m3_rows or sam_dla)}
    found_sam = found_m3 = dla_only = 0
    families: Counter[str] = Counter()
    details = []
    for sid in sample_ids:
        key = re.sub(r"[^A-Z0-9]", "", sid.upper())[:40]
        fam_m = re.match(r"(SPE[0-9A-Z]{0,4}|SPR[0-9A-Z]{0,4})", key)
        fam = fam_m.group(1) if fam_m else "OTHER"
        families[fam] += 1
        in_sam = key in sam_keys or any(key in _sol_key(r) for r in sam_dla)
        in_m3 = key in m3_keys or any(key in _sol_key(r) for r in (m3_rows or []))
        if in_sam:
            found_sam += 1
        if in_m3:
            found_m3 += 1
        if not in_sam and not in_m3:
            dla_only += 1
        details.append(
            {
                "id": sid,
                "family": fam,
                "found_in_sam": in_sam,
                "found_in_m3": in_m3,
                "dibbs_only_candidate": (not in_sam) and dibbs_accessible,
            }
        )
    return {
        "kind": "DLA_COVERAGE_SAMPLE",
        "sample_size": len(sample_ids),
        "found_in_sam": found_sam,
        "found_in_m3": found_m3,
        "dla_only_or_missing": dla_only,
        "families_represented": dict(families),
        "details": details,
        "extrapolation_forbidden": True,
        "note": "Sample is diagnostic only — not a national completeness percentage",
    }


def build_federal_discovery_gap_queue(
    *,
    sam_result: dict[str, Any] | None = None,
    dibbs_probe: dict[str, Any] | None = None,
    recon: dict[str, Any] | None = None,
    matrix: list[dict[str, Any]] | None = None,
    handoff_backlog: int = 0,
    limit: int = 25,
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    sam_result = sam_result or {}
    if sam_result.get("pagination_incomplete"):
        gaps.append(
            {
                "gap_type": GAP_SAM_PAGINATION_INCOMPLETE,
                "expected_yield": 95,
                "detail": f"stop={sam_result.get('stop_reason')} calls={sam_result.get('LIVE_SAM_CALLS')}",
                "cause": (
                    "ACCESS"
                    if str(sam_result.get("stop_reason") or "").startswith("HTTP_")
                    or sam_result.get("stop_reason") == "SAM_API_BUDGET"
                    else "OUR_SOFTWARE"
                ),
            }
        )
    recon_counts = (sam_result.get("count_reconciliation") or {})
    if abs(int(recon_counts.get("DIFFERENCE") or 0)) > 100 and "truncation" in str(
        recon_counts.get("DIFFERENCE_REASON") or ""
    ):
        gaps.append(
            {
                "gap_type": GAP_SAM_COUNT_MISMATCH,
                "expected_yield": 90,
                "detail": recon_counts.get("DIFFERENCE_REASON"),
                "cause": "OUR_SOFTWARE",
            }
        )
    dibbs_state = str((dibbs_probe or {}).get("access_state") or "")
    if "BOT" in dibbs_state or "AUTH" in dibbs_state:
        gaps.append(
            {
                "gap_type": GAP_DLA_DIBBS_AUTOMATION_BLOCKED,
                "expected_yield": 92,
                "detail": dibbs_state,
                "cause": "ACCESS",
            }
        )
    if (recon or {}).get("dibbs_only", 0) > 0:
        gaps.append(
            {
                "gap_type": GAP_DLA_SAM_RECONCILIATION_GAP,
                "expected_yield": 85,
                "detail": f"dibbs_only={(recon or {}).get('dibbs_only')}",
                "cause": "UNKNOWN",
            }
        )
    for row in matrix or []:
        if int(row.get("missing_from_m3_if_dibbs_only") or 0) > 5:
            gaps.append(
                {
                    "gap_type": GAP_DLA_FAMILY_UNDERREPRESENTED,
                    "expected_yield": 80,
                    "target": row.get("family_activity"),
                    "detail": f"missing={row.get('missing_from_m3_if_dibbs_only')}",
                    "cause": "ACCESS" if "BOT" in dibbs_state else "OUR_SOFTWARE",
                }
            )
    if handoff_backlog > 1000:
        gaps.append(
            {
                "gap_type": GAP_FEDERAL_HANDOFF_BACKLOG,
                "expected_yield": 70,
                "detail": f"backlog={handoff_backlog}",
                "cause": "OUR_SOFTWARE",
            }
        )
    gaps.append(
        {
            "gap_type": GAP_PIEE_AUTH_REQUIRED,
            "expected_yield": 60,
            "detail": "PIEE public index often auth-gated for automation",
            "cause": "AUTH",
        }
    )
    gaps.sort(key=lambda g: -int(g.get("expected_yield") or 0))
    return gaps[:limit]


def classify_dla_coverage_state(
    *,
    dibbs_access: str,
    sam_dla_count: int,
    dibbs_only: int,
    pagination_incomplete: bool,
) -> str:
    if "BOT" in dibbs_access or "AUTH" in dibbs_access or "REGISTRATION" in dibbs_access:
        if sam_dla_count > 0:
            return DLA_COVERAGE_ACCESS_CONSTRAINED
        return DLA_COVERAGE_ACCESS_CONSTRAINED
    if pagination_incomplete or dibbs_only > 0:
        return DLA_PUBLIC_COVERAGE_PARTIAL
    if sam_dla_count > 0 and dibbs_only == 0:
        return DLA_PUBLIC_COVERAGE_RECONCILED
    return DLA_KNOWN_GAPS
