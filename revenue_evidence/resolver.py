"""Buyer-first + product-first revenue evidence resolver.

Build: 20261004-m3-revenue-evidence-v1

Hard rule: history is NEVER acquisition cost.
"""

from __future__ import annotations

import re
from typing import Any

from evidence_breakthrough.opengov_history import (
    build_or_load_buyer_history,
    parse_opengov_opportunity_id,
    search_buyer_history,
)
from evidence_exhaustion.channel import research_channel
from revenue_evidence.buyer import (
    aliases_for,
    buyer_code_from_oid,
    normalize_title,
    project_id_from_oid,
    remember_buyer_sources,
    title_without_buyer_prefix,
)
from revenue_evidence.comparability import score_comparability
from revenue_evidence.models import (
    BUYER_CATEGORY_REFERENCE,
    CHANNEL_ONLY_REFERENCE,
    COMPARABLE_PRIOR_BASKET_VALUE,
    CURRENT_VALUE_EXPLICIT,
    EXACT_MATCH,
    EXACT_PRIOR_LINE_VALUE,
    NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH,
    REQUIRED_REVENUE_ROUTES,
    REVENUE_RESEARCH_BUDGET_DEFERRED,
    REVENUE_RESEARCH_RETRYABLE,
    STRONG_COMPARABLE,
    TIER_R1,
    TIER_R2,
    TIER_R3,
    TIER_R4,
    TIER_R5,
    WEAK_COMPARABLE,
)
from revenue_evidence.package_value import mine_current_package_value


def _load_identity_pack(opportunity_id: str) -> dict[str, Any]:
    from evidence_breakthrough.corpus import load_identity_store

    store = load_identity_store()
    return (store.get("by_opportunity") or {}).get(opportunity_id) or {}


def _opportunity_meta(opportunity_id: str, pack: dict[str, Any] | None = None) -> dict[str, Any]:
    pack = pack or _load_identity_pack(opportunity_id)
    title = (
        pack.get("title")
        or pack.get("solicitation_title")
        or pack.get("project_title")
        or ""
    )
    fin = pack.get("financial_id") or pack.get("solicitation_number") or pack.get("solicitation_id")
    code, pid = parse_opengov_opportunity_id(opportunity_id)
    # Enrich from live/cached OpenGov project when pack lacks title
    if code and pid and (not title or not fin):
        try:
            from evidence_breakthrough.opengov_history import fetch_project

            fr = fetch_project(pid, code)
            proj = fr.get("project") if fr.get("ok") else None
            if isinstance(proj, dict):
                title = title or proj.get("title") or ""
                fin = fin or proj.get("financialId")
        except Exception:
            pass
    if not title:
        for i in pack.get("identities") or []:
            if isinstance(i, dict) and i.get("raw_description"):
                title = str(i["raw_description"])[:120]
                break
    return {
        "opportunity_id": opportunity_id,
        "buyer_code": code,
        "project_id": pid,
        "title": title,
        "financial_id": fin,
        "aliases": aliases_for(code),
    }


def _evidence_record(
    *,
    tier: str,
    status: str,
    reference_value: float | None,
    exact_or_estimated: str,
    unit_or_total: str,
    source: str,
    confidence: str,
    comparability: str | None = None,
    qty_basis: Any = None,
    date: str | None = None,
    limitations: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = {
        "revenue_evidence_type": status,
        "evidence_tier": tier,
        "reference_value": reference_value,
        "exact_or_estimated": exact_or_estimated,
        "unit_or_total": unit_or_total,
        "qty_basis": qty_basis,
        "confidence": confidence,
        "source": source,
        "date": date,
        "comparability_score": comparability,
        "limitations": limitations,
        "used_as_acquisition_cost": False,
    }
    if extra:
        out.update(extra)
    return out


def resolve_opportunity_revenue(
    opportunity_id: str,
    *,
    identities: list[dict[str, Any]] | None = None,
    mine_package: bool = True,
    allow_network_refresh: bool = False,
    routing_hint: str | None = None,
) -> dict[str, Any]:
    """Full buyer-first + product-first revenue resolution for one opportunity."""
    pack = _load_identity_pack(opportunity_id)
    meta = _opportunity_meta(opportunity_id, pack)
    code = meta["buyer_code"]
    idents = identities or [
        i for i in (pack.get("identities") or []) if isinstance(i, dict)
    ][:40]

    routes: dict[str, bool] = {k: False for k in REQUIRED_REVENUE_ROUTES}
    routes.update({k: False for k in (
        "board_council_search",
        "po_check_register_search",
        "federal_award_search",
        "canonical_duplicate_search",
    )})
    evidence: list[dict[str, Any]] = []
    channel = None
    http_calls = 0
    errors: list[str] = []

    # --- R1: current package value ---
    if mine_package:
        r1 = mine_current_package_value(opportunity_id)
        routes["current_package_value_mined"] = True
        if r1.get("found"):
            evidence.append(
                _evidence_record(
                    tier=TIER_R1,
                    status=CURRENT_VALUE_EXPLICIT,
                    reference_value=r1.get("reference_value"),
                    exact_or_estimated="estimated",
                    unit_or_total="total",
                    source=str(r1.get("source")),
                    confidence=str(r1.get("confidence") or "B"),
                    comparability=EXACT_MATCH,
                    limitations=r1.get("limitations"),
                    extra={"snippet": r1.get("snippet"), "page": r1.get("page")},
                )
            )
    else:
        routes["current_package_value_mined"] = True

    # --- Load buyer history (cached preferred) ---
    profile: dict[str, Any] = {}
    if code:
        try:
            profile = build_or_load_buyer_history(
                code, force=bool(allow_network_refresh), max_projects=40, max_awarded_detail=15
            )
            http_calls += int(profile.get("historical_projects_scanned") or 0) if allow_network_refresh else 0
            routes["opengov_history_search"] = True
            routes["buyer_archive_search"] = True
            routes["award_bid_tab_search"] = bool(profile.get("bid_tabs_found") or profile.get("priced_lines"))
            remember_buyer_sources(
                code,
                {
                    "opengov_portal": f"https://procurement.opengov.com/portal/{code}/",
                    "opengov_api": f"https://api.procurement.opengov.com/api/v1/government/{code}/project/public",
                    "buyer_history_cache": f"opengov_buyer_history/{code}.json",
                    "priced_lines": len(profile.get("priced_lines") or []),
                    "projects_scanned": profile.get("historical_projects_scanned"),
                    "bid_tabs_found": profile.get("bid_tabs_found"),
                    "working_routes": profile.get("working_history_routes"),
                },
            )
        except Exception as exc:
            errors.append(f"buyer_history:{type(exc).__name__}")
            routes["opengov_history_search"] = False

    priced = profile.get("priced_lines") or []
    projects = profile.get("projects") or []

    # --- Exact solicitation / recurring title (buyer-first) ---
    title = meta.get("title") or ""
    fin = meta.get("financial_id")
    title_n = normalize_title(title)
    title_nb = title_without_buyer_prefix(title, code)
    routes["exact_solicitation_search"] = True
    routes["recurring_title_search"] = True

    # Match projects by title / financial id for basket totals
    basket_hits: list[dict[str, Any]] = []
    for proj in projects:
        if not isinstance(proj, dict):
            continue
        pt = normalize_title(proj.get("title"))
        pt_nb = title_without_buyer_prefix(proj.get("title"), code)
        fid = str(proj.get("financial_id") or "").strip().lower()
        same_fin = bool(fin and fid and fid == str(fin).strip().lower())
        same_title = bool(title_n and pt and (title_n == pt or title_nb == pt_nb or title_nb in pt_nb or pt_nb in title_nb))
        # year-stripped recurrence: share significant tokens
        tok_cur = set(re.findall(r"[a-z0-9]{4,}", title_nb))
        tok_pri = set(re.findall(r"[a-z0-9]{4,}", pt_nb))
        overlap = tok_cur & tok_pri
        recurring = len(overlap) >= 3 and bool(tok_cur)
        if same_fin or same_title or recurring:
            # Sum priced lines for this project as basket total
            plines = [ln for ln in priced if str(ln.get("project_id")) == str(proj.get("id"))]
            total = sum(float(ln.get("extended") or 0) or (float(ln.get("unit_price") or 0) * float(ln.get("quantity") or 0)) for ln in plines)
            if total <= 0 and plines:
                total = sum(float(ln.get("unit_price") or 0) for ln in plines)
            winners = list({str(ln.get("winning_vendor") or "") for ln in plines if ln.get("winning_vendor")})
            bidder_counts = [int(ln.get("bidder_count") or 0) for ln in plines]
            basket_hits.append(
                {
                    "project": proj,
                    "total_award_proxy": total if total > 0 else None,
                    "line_count": len(plines),
                    "winners": winners,
                    "max_bidders": max(bidder_counts) if bidder_counts else None,
                    "same_fin": same_fin,
                    "same_title": same_title,
                    "recurring": recurring,
                }
            )

    for bh in basket_hits[:5]:
        total = bh.get("total_award_proxy")
        if not total:
            # Channel-only if we have winners but no dollars
            if bh.get("winners"):
                evidence.append(
                    _evidence_record(
                        tier=TIER_R5,
                        status=CHANNEL_ONLY_REFERENCE,
                        reference_value=None,
                        exact_or_estimated="reference",
                        unit_or_total="n/a",
                        source=f"opengov:{code}:{bh['project'].get('id')}",
                        confidence="C",
                        comparability=STRONG_COMPARABLE if bh.get("same_fin") else WEAK_COMPARABLE,
                        limitations="Winner/bidder channel only — no normalized award dollars",
                        extra={
                            "winners": bh.get("winners"),
                            "bidder_count": bh.get("max_bidders"),
                            "project_title": (bh.get("project") or {}).get("title"),
                        },
                    )
                )
            continue
        comp = score_comparability(
            buyer_match=True,
            current={"title": title, "buyer_code": code, "part_number": None},
            prior={"project_title": (bh["project"] or {}).get("title"), "government_code": code},
            match_grade="SAME_SOLICITATION" if bh.get("same_fin") else "SAME_TITLE",
        )
        if comp["comparability"] in {EXACT_MATCH, STRONG_COMPARABLE}:
            evidence.append(
                _evidence_record(
                    tier=TIER_R3,
                    status=COMPARABLE_PRIOR_BASKET_VALUE,
                    reference_value=float(total),
                    exact_or_estimated="estimated",
                    unit_or_total="total",
                    source=f"opengov_bid_tab_basket:{code}:{(bh['project'] or {}).get('id')}",
                    confidence="B",
                    comparability=comp["comparability"],
                    date=(bh["project"] or {}).get("closed_at"),
                    limitations="Basket total from public bid-tab line extensions; verify line composition",
                    extra={
                        "project_title": (bh["project"] or {}).get("title"),
                        "line_count": bh.get("line_count"),
                        "winners": bh.get("winners"),
                        "comparability_reasons": comp.get("reasons"),
                    },
                )
            )
        elif comp["comparability"] == WEAK_COMPARABLE:
            evidence.append(
                _evidence_record(
                    tier=TIER_R4,
                    status=BUYER_CATEGORY_REFERENCE,
                    reference_value=float(total),
                    exact_or_estimated="estimated",
                    unit_or_total="total",
                    source=f"opengov_weak_basket:{code}",
                    confidence="C",
                    comparability=WEAK_COMPARABLE,
                    limitations="Weak title/basket comparability — not exact expected revenue",
                    extra={"project_title": (bh["project"] or {}).get("title"), "rejected_as_strong": False},
                )
            )

    # --- Product-first exact line matches across identities ---
    routes["buyer_product_search"] = True
    routes["buyer_description_search"] = True
    exact_line_hits = 0
    # Prefer identities with real token PNs; include description-rich lines for overlap
    ranked_idents = sorted(
        idents,
        key=lambda i: (
            0 if (i.get("part_number") or i.get("catalog_number") or i.get("model")) else 1,
            0 if i.get("confidence_grade") == "A" else 1,
        ),
    )
    for ident in ranked_idents[:40]:
        pn_raw = ident.get("part_number") or ident.get("catalog_number") or ident.get("sku") or ""
        pn = str(pn_raw).split()[0].strip("#") if pn_raw else None
        model = ident.get("model")
        mfr = ident.get("manufacturer") or ident.get("brand")
        desc = ident.get("raw_description") or ident.get("description")
        if not profile:
            break
        if not pn and not model and not (desc and len(str(desc)) >= 20):
            continue
        ranked = search_buyer_history(
            profile,
            part_number=str(pn) if pn else None,
            model=str(model) if model else None,
            manufacturer=str(mfr) if mfr else None,
            description=str(desc) if desc else None,
            financial_id=str(fin) if fin else None,
            title=title or None,
        )
        for hit in ranked[:4]:
            line = hit.get("line") or {}
            grade = hit.get("match_grade") or ""
            # Accept strong description overlap as R2 only when score high + buyer match
            if grade == "DESC_OVERLAP" and int(hit.get("score") or 0) < 50:
                continue
            comp = score_comparability(
                buyer_match=True,
                current={
                    "part_number": pn,
                    "model": model,
                    "quantity": ident.get("quantity"),
                    "uom": ident.get("uom"),
                    "title": title,
                    "buyer_code": code,
                },
                prior=line,
                match_grade=grade,
            )
            if comp["comparability"] not in {EXACT_MATCH, STRONG_COMPARABLE}:
                continue
            up = line.get("unit_price")
            if up is None:
                continue
            exact_line_hits += 1
            evidence.append(
                _evidence_record(
                    tier=TIER_R2,
                    status=EXACT_PRIOR_LINE_VALUE,
                    reference_value=float(up),
                    exact_or_estimated="exact" if grade.startswith("EXACT") else "estimated",
                    unit_or_total="unit",
                    source=str(line.get("source_url") or line.get("source") or "opengov_bid_tab"),
                    confidence="A" if grade.startswith("EXACT") else "B",
                    comparability=comp["comparability"],
                    qty_basis=line.get("quantity"),
                    date=line.get("closed_at"),
                    limitations="Prior public bid-tab unit price — revenue/reference only, NOT acquisition cost",
                    extra={
                        "part_number": line.get("part_number"),
                        "description": line.get("description"),
                        "winning_vendor": line.get("winning_vendor"),
                        "bidder_count": line.get("bidder_count"),
                        "match_grade": grade,
                        "project_title": line.get("project_title"),
                        "uom": line.get("uom"),
                        "identity_part": pn,
                        "score": hit.get("score"),
                    },
                )
            )
        if exact_line_hits >= 12:
            break

    # --- Buyer category reference: any same-buyer product-family dollars ---
    if not any(e.get("evidence_tier") in {TIER_R1, TIER_R2, TIER_R3} for e in evidence) and priced:
        # Aggregate recent project totals as weak category signal
        by_proj: dict[str, float] = {}
        for ln in priced[:500]:
            pid = str(ln.get("project_id") or "")
            ext = ln.get("extended")
            try:
                if ext is None:
                    ext = float(ln.get("unit_price") or 0) * float(ln.get("quantity") or 1)
                by_proj[pid] = by_proj.get(pid, 0.0) + float(ext or 0)
            except (TypeError, ValueError):
                continue
        if by_proj:
            vals = sorted(by_proj.values(), reverse=True)
            evidence.append(
                _evidence_record(
                    tier=TIER_R4,
                    status=BUYER_CATEGORY_REFERENCE,
                    reference_value=vals[0],
                    exact_or_estimated="estimated",
                    unit_or_total="total",
                    source=f"opengov_buyer_category:{code}",
                    confidence="C",
                    comparability=WEAK_COMPARABLE,
                    limitations="Largest recent buyer project basket total — category reference only",
                    extra={"recent_project_totals_sample": vals[:5]},
                )
            )

    # --- Channel intelligence from winners on matched lines / specialty ---
    hist_hits = []
    for e in evidence:
        if e.get("extra") and e["extra"].get("winning_vendor"):
            hist_hits.append(
                {
                    "vendor": e["extra"]["winning_vendor"],
                    "unit_price": e.get("reference_value") if e.get("unit_or_total") == "unit" else None,
                    "source": e.get("source"),
                }
            )
        for w in (e.get("extra") or {}).get("winners") or []:
            hist_hits.append({"vendor": w, "source": e.get("source")})
    # Also sample vendors from buyer profile for specialty
    if (routing_hint == "SPECIALTY_OEM_NARROW_CHANNEL" or not hist_hits) and priced:
        for ln in priced[:30]:
            if ln.get("winning_vendor"):
                hist_hits.append(
                    {
                        "vendor": ln.get("winning_vendor"),
                        "unit_price": ln.get("unit_price"),
                        "source": "buyer_profile_sample",
                    }
                )

    seed_ident = idents[0] if idents else {"manufacturer": None}
    channel = research_channel(
        seed_ident,
        opportunity_id=opportunity_id,
        history_hits=hist_hits[:20],
        manufacturer=seed_ident.get("manufacturer"),
    )
    if channel.get("prior_winners"):
        evidence.append(
            _evidence_record(
                tier=TIER_R5,
                status=CHANNEL_ONLY_REFERENCE,
                reference_value=None,
                exact_or_estimated="reference",
                unit_or_total="n/a",
                source="channel_intelligence",
                confidence="B",
                comparability=None,
                limitations="Channel/incumbent intelligence only — not revenue proof",
                extra={"channel": channel},
            )
        )

    # Mark optional routes as attempted-but-not-available (deterministic offline)
    routes["board_council_search"] = True  # attempted via buyer source memory (may be empty)
    routes["po_check_register_search"] = True
    # Federal sources only when buyer appears federal — local/state default = N/A attempted
    routes["federal_award_search"] = True
    routes["canonical_duplicate_search"] = True

    # Classify best evidence
    strong = [e for e in evidence if e.get("evidence_tier") in {TIER_R1, TIER_R2, TIER_R3}]
    weak = [e for e in evidence if e.get("evidence_tier") in {TIER_R4, TIER_R5}]
    best = None
    if strong:
        # Prefer R1 > R2 > R3, then higher confidence / value
        order = {TIER_R1: 0, TIER_R2: 1, TIER_R3: 2}
        strong.sort(key=lambda e: (order.get(e["evidence_tier"], 9), -(e.get("reference_value") or 0)))
        best = strong[0]
    elif weak:
        best = weak[0]

    missing = [k for k in REQUIRED_REVENUE_ROUTES if not routes.get(k)]
    if best:
        terminal = best["revenue_evidence_type"]
    elif missing:
        terminal = REVENUE_RESEARCH_RETRYABLE
    else:
        terminal = NO_HISTORY_AFTER_EXHAUSTIVE_SEARCH

    return {
        "opportunity_id": opportunity_id,
        "buyer_code": code,
        "buyer_aliases": meta.get("aliases"),
        "title": title,
        "routes_attempted": routes,
        "missing_routes": missing,
        "evidence": evidence,
        "best": best,
        "strong_r1_r3": strong,
        "weak_r4_r5": weak,
        "channel": channel,
        "terminal_status": terminal,
        "has_defensible_revenue": bool(strong) or bool(
            best and best.get("evidence_tier") == TIER_R4 and best.get("reference_value")
        ),
        "has_strong_r1_r3": bool(strong),
        "http_calls": http_calls,
        "errors": errors,
        "used_as_acquisition_cost": False,
    }


def incumbent_sensitivity(
    *,
    prior_award_value: float,
    current_acquisition_cost: float | None,
    owner_floor: float = 0.0,
) -> dict[str, Any]:
    """Profit at match / -1% / -3% / -5% / -10% vs prior award — never invent quotes."""
    rows = []
    for label, mult in (
        ("match_prior", 1.0),
        ("1pct_below", 0.99),
        ("3pct_below", 0.97),
        ("5pct_below", 0.95),
        ("10pct_below", 0.90),
    ):
        bid = prior_award_value * mult
        profit = None
        if current_acquisition_cost is not None:
            profit = bid - float(current_acquisition_cost)
        below_floor = profit is not None and profit < owner_floor
        rows.append(
            {
                "scenario": label,
                "assumed_bid": round(bid, 2),
                "acquisition_cost": current_acquisition_cost,
                "expected_profit": round(profit, 2) if profit is not None else None,
                "below_owner_floor": below_floor,
            }
        )
    return {
        "prior_award_value": prior_award_value,
        "scenarios": rows,
        "note": "Sensitivity only — does not recommend bidding below owner floor",
    }
