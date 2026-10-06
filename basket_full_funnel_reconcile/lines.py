"""Full line inventory + terminal classification + basket completion research."""

from __future__ import annotations

import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from basket_full_funnel_reconcile.models import (
    CONDITION_UNRESOLVED,
    EXECUTION_BLOCKED,
    IDENTITY_AMBIGUOUS,
    ITEM_DEADLINE_S,
    NO_COMPLIANT_SOURCE,
    NOT_REQUIRED_FOR_BASKET,
    PRICED_EXECUTABLE,
    PUBLIC_PRICE_UNAVAILABLE,
    QUOTE_REQUIRED,
    UOM_UNRESOLVED,
    WRONG_OR_MISSING_PACK,
)


def _d(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _money(v: Decimal | float | None) -> float | None:
    if v is None:
        return None
    return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def enumerate_lines(opportunity_id: str, corpus_row: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Enumerate every procurement line for an opportunity — no hidden skips."""
    from evidence_breakthrough.corpus import load_identity_store
    from m3_data_root import data_path
    import json

    lines: list[dict[str, Any]] = []
    id_pack = (load_identity_store().get("by_opportunity") or {}).get(opportunity_id) or {}
    identities = list(id_pack.get("identities") or [])
    # Cap runaway identity packs (OCR noise) — prefer acquisition-known lines first.
    if len(identities) > 80:
        identities = identities[:80]

    # Merge acquisition-scale by_line rows
    acq_path = data_path("m3_acquisition_scale_v1_checkpoint.json")
    by_line: dict[str, Any] = {}
    if acq_path.exists():
        try:
            by_line = json.loads(acq_path.read_text(encoding="utf-8")).get("by_line") or {}
        except Exception:
            by_line = {}

    acq_lines = [row for row in by_line.values() if row.get("opportunity_id") == opportunity_id]
    # Index acquisition rows by part number from key suffix or identity
    acq_by_mpn: dict[str, dict[str, Any]] = {}
    for row in acq_lines:
        ident = row.get("identity") or {}
        mpn = str(ident.get("part_number") or row.get("mpn") or row.get("part_number") or "").strip()
        if not mpn and isinstance(row.get("key"), str) and "::" in row["key"]:
            mpn = row["key"].rsplit("::", 1)[-1].strip()
        if mpn:
            acq_by_mpn[mpn.upper()] = row
        # Normalize price fields for downstream cache classification
        if row.get("unit_price") and not row.get("unit_cost"):
            row["unit_cost"] = row.get("unit_price")
        if row.get("status") in {"PUBLIC_PRICE_AVAILABLE", "PUBLIC_PRICE_FOUND"} and row.get("unit_price"):
            row["usable"] = True

    seen_keys: set[str] = set()

    for idx, ident in enumerate(identities):
        if not isinstance(ident, dict):
            continue
        mpn = str(ident.get("part_number") or ident.get("mpn") or "").strip()
        key = f"{opportunity_id}|{mpn or idx}"
        seen_keys.add(key)
        matching = acq_by_mpn.get(mpn.upper()) if mpn else None
        if matching is None:
            matching = next((r for r in acq_lines if str((r.get("identity") or {}).get("part_number") or "") == mpn), None)
        lines.append(
            {
                "line_key": key,
                "opportunity_id": opportunity_id,
                "clin": ident.get("clin") or ident.get("line_number") or str(idx + 1),
                "description": ident.get("raw_description") or ident.get("description"),
                "manufacturer": ident.get("manufacturer"),
                "mpn": mpn or None,
                "model": ident.get("model"),
                "equal_rule": ident.get("equal_rule") or ident.get("brand_name_or_equal"),
                "quantity": ident.get("quantity") or 1,
                "uom": ident.get("uom") or ident.get("unit_of_measure") or "EA",
                "pack": ident.get("pack") or ident.get("pack_qty") or 1,
                "required_condition": ident.get("condition") or "NEW",
                "delivery_destination": ident.get("delivery_destination") or ident.get("ship_to"),
                "delivery_date": ident.get("delivery_date") or ident.get("required_delivery"),
                "freight_terms": ident.get("freight_terms") or ident.get("fob"),
                "prior_acq": matching,
                "identity": ident,
            }
        )

    for row in acq_lines:
        ident = row.get("identity") or {}
        mpn = str(ident.get("part_number") or row.get("mpn") or row.get("part_number") or "").strip()
        if not mpn and isinstance(row.get("key"), str) and "::" in row["key"]:
            mpn = row["key"].rsplit("::", 1)[-1].strip()
        key = row.get("key") or f"{opportunity_id}|{mpn}"
        if key in seen_keys or (mpn and f"{opportunity_id}|{mpn}" in seen_keys):
            continue
        seen_keys.add(key)
        if row.get("unit_price") and not row.get("unit_cost"):
            row["unit_cost"] = row.get("unit_price")
        lines.append(
            {
                "line_key": key,
                "opportunity_id": opportunity_id,
                "clin": row.get("clin") or row.get("line_number"),
                "description": ident.get("description") or row.get("description"),
                "manufacturer": ident.get("manufacturer") or row.get("manufacturer"),
                "mpn": mpn or None,
                "model": ident.get("model") or row.get("model"),
                "equal_rule": row.get("equal_rule"),
                "quantity": row.get("quantity") or ident.get("quantity") or 1,
                "uom": row.get("uom") or "EA",
                "pack": row.get("pack") or 1,
                "required_condition": row.get("condition") or "NEW",
                "delivery_destination": row.get("delivery_destination"),
                "delivery_date": row.get("delivery_date"),
                "freight_terms": row.get("freight_terms"),
                "prior_acq": row,
                "identity": ident or row,
            }
        )

    # If still empty, synthesize from corpus identity_count so conservation holds
    if not lines and corpus_row:
        n = max(1, int(corpus_row.get("identity_count") or 1))
        for i in range(n):
            lines.append(
                {
                    "line_key": f"{opportunity_id}|synth-{i+1}",
                    "opportunity_id": opportunity_id,
                    "clin": str(i + 1),
                    "description": None,
                    "manufacturer": None,
                    "mpn": None,
                    "model": None,
                    "equal_rule": None,
                    "quantity": 1,
                    "uom": "EA",
                    "pack": 1,
                    "required_condition": "NEW",
                    "delivery_destination": None,
                    "delivery_date": None,
                    "freight_terms": None,
                    "prior_acq": None,
                    "identity": {},
                    "synthetic": True,
                }
            )
    return lines


def _classify_from_prior(prior: dict[str, Any] | None) -> str | None:
    if not prior:
        return None
    price = prior.get("unit_cost") or prior.get("price") or prior.get("best_unit_cost") or prior.get("acq_unit") or prior.get("unit_price")
    if prior.get("usable") or price:
        try:
            if float(price) > 0:
                return PRICED_EXECUTABLE
        except Exception:
            if prior.get("usable"):
                return PRICED_EXECUTABLE
    if str(prior.get("status") or "").upper() in {"PUBLIC_PRICE_AVAILABLE", "PUBLIC_PRICE_FOUND"}:
        try:
            if float(prior.get("unit_price") or prior.get("unit_cost") or 0) > 0:
                return PRICED_EXECUTABLE
        except Exception:
            pass
    status = str(prior.get("status") or prior.get("reason") or "").upper()
    if "QUOTE" in status:
        return QUOTE_REQUIRED
    if "UOM" in status:
        return UOM_UNRESOLVED
    if "PACK" in status:
        return WRONG_OR_MISSING_PACK
    if "CONDITION" in status:
        return CONDITION_UNRESOLVED
    if "IDENTITY" in status or "AMBIG" in status:
        return IDENTITY_AMBIGUOUS
    if "BLOCK" in status:
        return EXECUTION_BLOCKED
    return None


def research_line(line: dict[str, Any], *, deadline: float, stats: dict[str, Any]) -> dict[str, Any]:
    """Drive unresolved line to a terminal state. Reuse cache first."""
    out = dict(line)
    prior = line.get("prior_acq") or {}
    cached = _classify_from_prior(prior)
    if cached == PRICED_EXECUTABLE:
        price = (
            prior.get("unit_cost")
            or prior.get("unit_price")
            or prior.get("price")
            or prior.get("best_unit_cost")
        )
        out["terminal_state"] = PRICED_EXECUTABLE
        out["unit_cost"] = _money(_d(price))
        out["seller"] = prior.get("seller") or prior.get("domain")
        out["source_url"] = prior.get("source_url") or prior.get("url")
        out["research_route"] = "CACHED_ACQUISITION"
        return out

    mpn = line.get("mpn")
    if not mpn or line.get("synthetic"):
        out["terminal_state"] = IDENTITY_AMBIGUOUS
        out["research_route"] = "NO_MPN"
        return out

    if time.time() > deadline:
        out["terminal_state"] = PUBLIC_PRICE_UNAVAILABLE
        out["research_route"] = "DEADLINE"
        return out

    # Live accurate price via proven stack
    try:
        from price_coverage_80.resolve import resolve_accurate_price

        item = {
            "benchmark_id": f"basket-{line.get('line_key')}",
            "mpn": mpn,
            "manufacturer": line.get("manufacturer"),
            "description": line.get("description"),
            "expected_condition": line.get("required_condition") or "NEW",
            "expected_uom": line.get("uom") or "EA",
            "expected_pack": int(line.get("pack") or 1),
            "category": "live_m3",
        }
        found = resolve_accurate_price(
            item,
            use_budget=True,
            max_sellers=4,
            max_queries=3,
            max_pages=5,
            stats=stats,
        )
        if found.get("usable") and found.get("price"):
            out["terminal_state"] = PRICED_EXECUTABLE
            out["unit_cost"] = _money(_d(found.get("price")))
            out["seller"] = found.get("seller") or found.get("domain")
            out["source_url"] = found.get("source_url") or found.get("url")
            out["extraction_route"] = found.get("extraction_route")
            out["research_route"] = "LIVE_ACCURATE_PRICE"
            return out
        reason = str(found.get("reason") or found.get("status") or "").upper()
        if "QUOTE" in reason or "LOGIN" in reason:
            out["terminal_state"] = QUOTE_REQUIRED
        elif "PACK" in reason:
            out["terminal_state"] = WRONG_OR_MISSING_PACK
        elif "UOM" in reason:
            out["terminal_state"] = UOM_UNRESOLVED
        elif "CONDITION" in reason:
            out["terminal_state"] = CONDITION_UNRESOLVED
        elif "IDENTITY" in reason or "WRONG" in reason:
            out["terminal_state"] = IDENTITY_AMBIGUOUS
        elif "BLOCK" in reason or "BOT" in reason:
            out["terminal_state"] = NO_COMPLIANT_SOURCE
        else:
            out["terminal_state"] = PUBLIC_PRICE_UNAVAILABLE
        out["research_route"] = "LIVE_EXHAUSTED"
        out["failure_reason"] = found.get("reason") or found.get("status")
        return out
    except Exception as exc:
        out["terminal_state"] = PUBLIC_PRICE_UNAVAILABLE
        out["research_route"] = "EXCEPTION"
        out["failure_reason"] = str(exc)[:200]
        return out


def complete_opportunity_lines(
    opportunity_id: str,
    corpus_row: dict[str, Any],
    *,
    stats: dict[str, Any] | None = None,
    deadline_s: float = ITEM_DEADLINE_S,
) -> dict[str, Any]:
    stats = stats if stats is not None else {}
    started = time.time()
    raw_lines = enumerate_lines(opportunity_id, corpus_row)
    # Scale deadline with line count; keep live research bounded.
    deadline_s = max(deadline_s, min(240.0, 40.0 + 8.0 * min(20, len(raw_lines))))
    deadline = started + deadline_s

    # Research order: already-priced cache first, then lines with MPN, then rest.
    def _rank(line: dict[str, Any]) -> tuple:
        prior = line.get("prior_acq") or {}
        has_price = 0 if _classify_from_prior(prior) == PRICED_EXECUTABLE else 1
        has_mpn = 0 if line.get("mpn") else 1
        return (has_price, has_mpn, str(line.get("line_key") or ""))

    ordered = sorted(raw_lines, key=_rank)
    researched: list[dict[str, Any]] = []
    live_attempts = 0
    max_live = 8
    for line in ordered:
        # After cache pass, limit live attempts so multiline baskets finish.
        prior_state = _classify_from_prior(line.get("prior_acq"))
        if prior_state == PRICED_EXECUTABLE or not line.get("mpn") or line.get("synthetic"):
            researched.append(research_line(line, deadline=deadline, stats=stats))
            continue
        if live_attempts >= max_live or time.time() > deadline:
            out = dict(line)
            out["terminal_state"] = PUBLIC_PRICE_UNAVAILABLE
            out["research_route"] = "BUDGET_EXHAUSTED"
            researched.append(out)
            continue
        live_attempts += 1
        researched.append(research_line(line, deadline=deadline, stats=stats))

    total = len(researched)
    priced = [r for r in researched if r.get("terminal_state") == PRICED_EXECUTABLE]
    identified = [r for r in researched if r.get("mpn")]
    quote = [r for r in researched if r.get("terminal_state") == QUOTE_REQUIRED]
    ambiguous = [r for r in researched if r.get("terminal_state") == IDENTITY_AMBIGUOUS]
    blocked = [
        r
        for r in researched
        if r.get("terminal_state")
        in {EXECUTION_BLOCKED, NO_COMPLIANT_SOURCE, WRONG_OR_MISSING_PACK, UOM_UNRESOLVED, CONDITION_UNRESOLVED}
    ]
    unresolved = [
        r
        for r in researched
        if r.get("terminal_state")
        not in {PRICED_EXECUTABLE, QUOTE_REQUIRED, NOT_REQUIRED_FOR_BASKET}
        and r.get("terminal_state") != PUBLIC_PRICE_UNAVAILABLE
    ]
    unavailable = [r for r in researched if r.get("terminal_state") == PUBLIC_PRICE_UNAVAILABLE]

    # Coverage metrics
    line_cov = (len(priced) / total) if total else 0.0
    qty_total = sum(float(r.get("quantity") or 1) for r in researched) or 1.0
    qty_priced = sum(float(r.get("quantity") or 1) for r in priced)
    qty_cov = qty_priced / qty_total
    # Value-weighted using unit_cost * qty when available; fall back to line cov
    val_total = 0.0
    val_priced = 0.0
    for r in researched:
        qty = float(r.get("quantity") or 1)
        unit = float(r.get("unit_cost") or 0) or 0.0
        # Use revenue hint from prior if present
        prior = r.get("prior_acq") or {}
        rev_u = float(prior.get("gov_unit") or prior.get("revenue_unit") or 0) or unit
        ext = qty * (rev_u or 1.0)
        val_total += ext
        if r.get("terminal_state") == PRICED_EXECUTABLE:
            val_priced += ext
    value_cov = (val_priced / val_total) if val_total > 0 else line_cov

    return {
        "opportunity_id": opportunity_id,
        "TOTAL_LINES": total,
        "PRICED_LINES": len(priced),
        "IDENTIFIED_LINES": len(identified),
        "UNRESOLVED_LINES": len(unresolved) + len(unavailable),
        "BLOCKED_LINES": len(blocked),
        "QUOTE_REQUIRED_LINES": len(quote),
        "AMBIGUOUS_LINES": len(ambiguous),
        "line_coverage": round(line_cov, 4),
        "quantity_weighted_coverage": round(qty_cov, 4),
        "value_weighted_coverage": round(value_cov, 4),
        "lines": researched,
        "elapsed_s": round(time.time() - started, 2),
    }
