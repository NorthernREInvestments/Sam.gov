"""Per-opportunity material line recovery pipeline."""

from __future__ import annotations

import time
from typing import Any

from material_line_identity_price_recovery.identity import (
    build_identity_clusters,
    is_usable,
    map_identity_confidence,
    validate_commercial_identity,
)
from material_line_identity_price_recovery.models import (
    A_EXACT_MPN,
    G_AMBIGUOUS,
    ITEM_DEADLINE_S,
    OPP_DEADLINE_S,
    P0_MAX_LIVE,
    P1_MAX_LIVE,
    USABLE_IDENTITY,
)
from material_line_identity_price_recovery.price import research_production_price
from material_line_identity_price_recovery.reconstruct import (
    match_source_to_material_line,
    reconstruct_opportunity_source,
)


def process_opportunity_material_lines(
    opportunity_id: str,
    material_lines: list[dict[str, Any]],
    *,
    stats: dict[str, Any] | None = None,
    deadline_s: float = OPP_DEADLINE_S,
) -> dict[str, Any]:
    stats = stats if stats is not None else {}
    started = time.time()
    # Adaptive deadline: more time when many material lines or spreadsheet-rich packages
    adaptive = deadline_s
    if len(material_lines) >= 40:
        adaptive = max(adaptive, 360.0)
    elif len(material_lines) >= 20:
        adaptive = max(adaptive, 300.0)
    deadline = started + adaptive

    print(f"[mlr] reconstruct {opportunity_id}", flush=True)
    source = reconstruct_opportunity_source(opportunity_id)
    source_idents = source.get("identities") or []

    # Boost live caps when package yields many exact identities
    exact_in_source = sum(
        1
        for i in source_idents
        if str(i.get("identity_type") or "") in {"EXACT_MPN", "EXACT_MODEL", "EXACT_CATALOG_NUMBER", "EXACT_NSN"}
    )
    local_p0_max = P0_MAX_LIVE + (6 if exact_in_source >= 50 else 0)
    local_p1_max = P1_MAX_LIVE + (8 if exact_in_source >= 50 else 0)

    # Reconstruction stats
    mfr_recovered = sum(1 for i in source_idents if i.get("manufacturer"))
    mpn_recovered = sum(1 for i in source_idents if i.get("part_number") or i.get("model"))
    nsn_recovered = sum(1 for i in source_idents if i.get("nsn"))

    recovered_lines: list[dict[str, Any]] = []
    for mat in material_lines:
        matched = match_source_to_material_line(mat, source_idents)
        row = {
            **mat,
            "description": mat.get("raw_solicitation_description"),
            "source_reconstructed": bool(matched) or source.get("package_found"),
            "spreadsheet_match": bool(matched and str((matched.get("source_kind") or "")).lower() in {"xlsx", "xls", "csv"}),
        }
        if matched:
            row.update(
                {
                    "manufacturer": matched.get("manufacturer") or mat.get("manufacturer_text"),
                    "mpn": matched.get("part_number") or matched.get("MPN") or mat.get("prior_mpn"),
                    "part_number": matched.get("part_number"),
                    "model": matched.get("model") or matched.get("MODEL"),
                    "nsn": matched.get("nsn") or matched.get("NSN") or mat.get("nsn"),
                    "upc": matched.get("upc"),
                    "quantity": matched.get("quantity") if matched.get("quantity") is not None else mat.get("quantity"),
                    "uom": matched.get("uom_normalized") or matched.get("uom") or mat.get("uom"),
                    "pack": matched.get("pack_size") or mat.get("pack"),
                    "equal_rule": matched.get("equal_allowed") or mat.get("brand_equal_language"),
                    "source_document": matched.get("source_document"),
                    "source_kind": matched.get("source_kind"),
                    "page_sheet_cell_provenance": {
                        "sheet": matched.get("_sheet"),
                        "row": matched.get("_row"),
                        "source_kind": matched.get("source_kind"),
                        "match_score": matched.get("match_score"),
                    },
                    "RAW_SOURCE_TEXT": matched.get("RAW_SOURCE_TEXT") or matched.get("raw_description"),
                    "product_identity_type": matched.get("identity_type"),
                    "salient_characteristics": matched.get("raw_description"),
                }
            )
            conf = map_identity_confidence(matched.get("identity_type"), source=matched)
        else:
            # Fall back: classify from frozen line text alone
            from product_identity.classifier import classify_identity
            from product_identity.normalizer import ProductIdentityNormalizer

            norm = ProductIdentityNormalizer().normalize_row(
                {
                    "manufacturer": mat.get("manufacturer_text"),
                    "part_number": mat.get("prior_mpn") or mat.get("part_model_text"),
                    "description": mat.get("raw_solicitation_description"),
                    "quantity": mat.get("quantity"),
                    "uom": mat.get("uom"),
                    "nsn": mat.get("nsn"),
                }
            )
            classified = classify_identity(norm)
            row.update(
                {
                    "manufacturer": classified.get("manufacturer") or mat.get("manufacturer_text"),
                    "mpn": classified.get("part_number") or mat.get("prior_mpn"),
                    "part_number": classified.get("part_number"),
                    "model": classified.get("model"),
                    "nsn": classified.get("nsn") or mat.get("nsn"),
                    "product_identity_type": classified.get("identity_type"),
                    "RAW_SOURCE_TEXT": mat.get("raw_solicitation_description"),
                }
            )
            conf = map_identity_confidence(classified.get("identity_type"), source=classified)

        row["identity_confidence"] = conf
        validation = validate_commercial_identity({**row, "identity_usable": is_usable(conf)})
        row["identity_validation"] = validation
        row["identity_validated"] = bool(validation.get("validated"))
        row["identity_usable"] = is_usable(conf) and row["identity_validated"]
        row["block_pricing"] = is_usable(conf) and not row["identity_validated"]
        recovered_lines.append(row)

    # Price research for validated A-E, materiality-first
    def _rank(ln: dict[str, Any]) -> tuple:
        tier = 0 if ln.get("materiality_tier") == "P0" else 1
        usable = 0 if ln.get("identity_usable") and not ln.get("block_pricing") else 1
        exact = 0 if ln.get("identity_confidence") in {A_EXACT_MPN, "B_EXACT_MODEL", "C_NSN_TO_EXACT_COMMERCIAL"} else 1
        return (tier, usable, exact, -float(ln.get("materiality_score") or 0), str(ln.get("line_id")))

    ordered = sorted(recovered_lines, key=_rank)
    live_p0 = 0
    live_p1 = 0
    priced_rows: list[dict[str, Any]] = []
    cluster_prices: dict[str, dict[str, Any]] = {}

    for ln in ordered:
        allow = False
        if (
            ln.get("identity_usable")
            and not ln.get("block_pricing")
            and ln.get("identity_confidence") in USABLE_IDENTITY
            and time.time() < deadline
        ):
            if ln.get("materiality_tier") == "P0" and live_p0 < local_p0_max:
                allow = True
            elif ln.get("materiality_tier") == "P1" and live_p1 < local_p1_max:
                allow = True

        # Cluster reuse
        from material_line_identity_price_recovery.identity import cluster_key
        import hashlib

        key = cluster_key(ln)
        if key:
            cid = "MIC-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
            ln["cluster_id"] = cid
            if cid in cluster_prices and ln.get("identity_usable"):
                shared = cluster_prices[cid]
                ln["acquisition_state"] = "PRICED_EXECUTABLE"
                ln["production_price"] = dict(shared)
                ln["price_propagated_from_cluster"] = True
                ln["unit_cost"] = shared.get("unit_cost")
                ln["price_origin"] = shared.get("price_origin")
                priced_rows.append(ln)
                continue

        item_deadline = min(deadline, time.time() + ITEM_DEADLINE_S)
        result = research_production_price(ln, deadline=item_deadline, stats=stats, allow_live=allow)
        if allow:
            if ln.get("materiality_tier") == "P0":
                live_p0 += 1
            else:
                live_p1 += 1
        if result.get("acquisition_state") == "PRICED_EXECUTABLE" and result.get("cluster_id"):
            cluster_prices[result["cluster_id"]] = result["production_price"]
        elif result.get("acquisition_state") == "PRICED_EXECUTABLE" and key:
            cid = result.get("cluster_id") or ("MIC-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10])
            cluster_prices[cid] = result["production_price"]
            result["cluster_id"] = cid
        priced_rows.append(result)

    # Rebuild clusters across this opportunity's lines
    clusters = build_identity_clusters(priced_rows)

    # Coverage metrics
    p0 = [l for l in priced_rows if l.get("materiality_tier") == "P0"]
    p1 = [l for l in priced_rows if l.get("materiality_tier") == "P1"]
    p0_usable = [l for l in p0 if l.get("identity_usable")]
    p1_usable = [l for l in p1 if l.get("identity_usable")]
    p0_priced = [l for l in p0 if l.get("acquisition_state") == "PRICED_EXECUTABLE"]
    p1_priced = [l for l in p1 if l.get("acquisition_state") == "PRICED_EXECUTABLE"]
    p0_quote = [l for l in p0 if l.get("acquisition_state") == "QUOTE_REQUIRED"]
    p1_quote = [l for l in p1 if l.get("acquisition_state") == "QUOTE_REQUIRED"]
    p0_terminal_price_or_quote = len(p0_priced) + len(p0_quote)

    material_n = len(priced_rows) or 1
    identified_n = sum(1 for l in priced_rows if l.get("identity_usable"))
    priced_n = sum(1 for l in priced_rows if l.get("acquisition_state") == "PRICED_EXECUTABLE")
    quote_n = sum(1 for l in priced_rows if l.get("acquisition_state") == "QUOTE_REQUIRED")
    # Material basket coverage = (priced + quote) / material  for progress; identity coverage separate
    material_identity_coverage = identified_n / material_n
    material_price_coverage = priced_n / material_n
    material_basket_coverage = (priced_n + quote_n) / material_n

    owner_view = {
        "material_lines": len(priced_rows),
        "identity_ready": identified_n,
        "production_priced": priced_n,
        "quote_required": quote_n,
        "ambiguous": sum(1 for l in priced_rows if not l.get("identity_usable")),
        "material_coverage": round(material_basket_coverage, 4),
        "revenue_confidence": "PROTECTED",  # revenue classifier active; profit hidden
        "next_blocker": _next_blocker(priced_rows, material_identity_coverage, material_price_coverage),
        "expected_profit": None,  # never show until ECONOMICS_READY
    }

    return {
        "opportunity_id": opportunity_id,
        "package_found": source.get("package_found"),
        "source_stats": {
            "lines_rebuilt_from_docs": len(source_idents),
            "spreadsheet_multi_column_recoveries": source.get("spreadsheet_recoveries") or 0,
            "continuation_row_recoveries": source.get("continuation_recoveries") or 0,
            "manufacturer_recovered": mfr_recovered,
            "mpn_model_recovered": mpn_recovered,
            "nsn_recovered": nsn_recovered,
        },
        "lines": priced_rows,
        "clusters_local": {k: clusters.get(k) for k in ("cluster_count", "repeated_lines_collapsed", "research_reuse_count")},
        "coverage": {
            "p0_total": len(p0),
            "p0_identified": len(p0_usable),
            "p0_priced": len(p0_priced),
            "p0_quote": len(p0_quote),
            "p0_price_or_quote_terminal": p0_terminal_price_or_quote,
            "p1_total": len(p1),
            "p1_identified": len(p1_usable),
            "p1_priced": len(p1_priced),
            "p1_quote": len(p1_quote),
            "MATERIAL_IDENTITY_COVERAGE": round(material_identity_coverage, 4),
            "MATERIAL_PRICE_COVERAGE": round(material_price_coverage, 4),
            "MATERIAL_BASKET_COVERAGE": round(material_basket_coverage, 4),
        },
        "owner_view": owner_view,
        "elapsed_s": round(time.time() - started, 2),
    }


def _next_blocker(lines: list[dict[str, Any]], id_cov: float, price_cov: float) -> str:
    amb = sum(1 for l in lines if not l.get("identity_usable"))
    if id_cov < 0.6:
        return f"Recover identity on {amb} ambiguous material lines"
    if price_cov < 0.4:
        return "Obtain production prices or quotes for identified material lines"
    return "Deep-complete remaining material gaps"
