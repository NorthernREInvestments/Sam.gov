"""Phase L.12 — auth-walled award recovery via buyer-specific public paths."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from application_clock import now_utc
from phase_l.auth_access import (
    BUILD,
    BUYER_SPECIFIC_ACCOUNT_REQUIRED,
    CAPTCHA_PRESENT,
    FREE_REGISTRATION_REQUIRED,
    HISTORY_NOT_AVAILABLE,
    NO_PUBLIC_HISTORY_FEATURE,
    PLATFORM_HISTORY_BLOCKED,
    PRIVATE_RESTRICTED,
    PUBLIC_ANTI_BOT_BLOCKED,
    UNKNOWN_ACCESS_MODE,
    VENDOR_ACCOUNT_REQUIRED,
    classify_access_mode,
)
from phase_l.buyer_history_paths import (
    discover_buyer_path_urls,
    get_buyer_history_path,
    record_successful_recovery,
    update_platform_memory,
)
from phase_l.exact_history_recovery import (
    GOV_UPGRADED_A,
    GOV_UPGRADED_B,
    GOV_UPGRADED_C,
    HISTORY_BUYER_RECORDS_NOT_FOUND,
    HISTORY_DOCUMENT_MISSING,
    HISTORY_EVIDENCE_EXHAUSTED,
    LOT_PRICE,
    document_title_queries,
    grade_recovered_award,
    solicitation_search_variants,
)
from phase_l.history_graphs import link_product_history, link_supplier, normalize_award_tabulation
from phase_l.platform_history import detect_platform
from phase_l.quality_audit import GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C, GOV_VALUE_D, grade_freshness
from phase_l.quote_economics import _f
from phase_l.supplier_upgrade import classify_prior_awardee_role

# Public evidence exhaustion order (L.12 §19)
PUBLIC_EVIDENCE_ORDER = (
    "buyer_procurement_page",
    "buyer_historical_solicitations",
    "award_result_page",
    "bid_tab_tabulation_files",
    "board_council_records",
    "po_check_register",
    "open_data_expenditure",
    "contract_register",
    "state_archive",
    "cooperative_schedule_history",
    "exact_solicitation_web_search",
    "exact_product_buyer_history",
    "registration_required_classification",
    "manual_auth_required_classification",
)


def _utc() -> str:
    return now_utc().isoformat()


def _norm(s: Any) -> str:
    return str(s or "").strip().upper()


def match_bid_tab_line(
    lines: list[dict[str, Any]],
    *,
    commercial: dict[str, Any] | None = None,
    title: str | None = None,
) -> dict[str, Any] | None:
    """Match exact product line — never use whole-contract total."""
    commercial = commercial or {}
    model = _norm(commercial.get("model") or commercial.get("mpn"))
    mfr = _norm(commercial.get("manufacturer"))
    title_n = _norm(title)
    best = None
    for line in lines:
        blob = _norm(
            " ".join(
                str(x or "")
                for x in (line.get("item"), line.get("description"), line.get("model"), line.get("mpn"))
            )
        )
        score = 0
        if model and model in blob:
            score += 3
        if mfr and mfr.split()[0] in blob:
            score += 2
        if title_n and title_n[:20] and title_n[:20] in blob:
            score += 1
        if score >= 3:
            matched = {**line, "line_matched": True, "match_score": score}
            if best is None or score > best.get("match_score", 0):
                best = matched
    return best


def extract_competition_from_bid_tab(lines: list[dict[str, Any]]) -> dict[str, Any] | None:
    prices = []
    vendors = []
    for line in lines:
        p = _f(line.get("unit_price") or line.get("bid_price") or line.get("price"))
        if p:
            prices.append(p)
        v = line.get("vendor") or line.get("bidder")
        if v and v not in vendors:
            vendors.append(v)
    if not prices and not vendors:
        return None
    prices_sorted = sorted(prices)
    return {
        "bidder_count": len(vendors) or len(prices),
        "awarded_vendor": vendors[0] if vendors else None,
        "bidder_prices_public": prices_sorted,
        "low": prices_sorted[0] if prices_sorted else None,
        "median": prices_sorted[len(prices_sorted) // 2] if prices_sorted else None,
        "high": prices_sorted[-1] if prices_sorted else None,
        "spread": (prices_sorted[-1] - prices_sorted[0]) if len(prices_sorted) >= 2 else None,
        "inferred_hidden_bids": False,
    }


def winning_price_distribution(awards: list[dict[str, Any]]) -> dict[str, Any] | None:
    prices = [_f(a.get("unit_price") or a.get("unit_value")) for a in awards]
    prices = [p for p in prices if p]
    if not prices:
        return None
    s = sorted(prices)
    return {
        "latest_winning_price": prices[-1],
        "median": s[len(s) // 2],
        "minimum": s[0],
        "maximum": s[-1],
        "n": len(s),
        "price_dispersion": s[-1] - s[0] if len(s) > 1 else 0,
    }


def registration_opportunity(
    *,
    platform: str,
    buyer: str,
    access_mode: str,
    registration_url: str | None = None,
    blocked_opportunities: int = 1,
    potential_value: float = 0.0,
    buyers_on_platform: int = 1,
) -> dict[str, Any]:
    """REGISTRATION_HISTORY_OPPORTUNITY — identify only, never create accounts."""
    free = access_mode == FREE_REGISTRATION_REQUIRED
    return {
        "kind": "REGISTRATION_HISTORY_OPPORTUNITY",
        "platform": platform,
        "buyer": buyer,
        "registration_url": registration_url,
        "registration_cost": 0.0 if free else None,
        "account_type": access_mode,
        "documents_unlocked": "unknown_until_registered",
        "award_history_unlocked": None,
        "bid_tabs_unlocked": None,
        "estimated_setup_effort": "low" if free else "medium",
        "number_of_current_blocked_opportunities": blocked_opportunities,
        "potential_quote_target_value_blocked": potential_value,
        "buyers_on_same_platform": buyers_on_platform,
        "create_account": False,
        "auto_register": False,
    }


def history_access_registration_priority(reg: dict[str, Any]) -> float:
    """HistoryAccessRegistrationPriority score."""
    n = float(reg.get("number_of_current_blocked_opportunities") or 0)
    value = float(reg.get("potential_quote_target_value_blocked") or 0)
    buyers = float(reg.get("buyers_on_same_platform") or 1)
    cost = float(reg.get("registration_cost") or 0)
    effort = {"low": 1.0, "medium": 0.6, "high": 0.3}.get(str(reg.get("estimated_setup_effort") or "medium"), 0.5)
    # Platform-wide leverage: one account → many buyers
    leverage = 1.0 + min(buyers, 50) / 10.0
    score = (n * 10 + value / 1000.0 + buyers * 5) * effort * leverage
    if cost > 0:
        score *= 0.5
    return round(score, 2)


def run_auth_walled_history_recovery(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    buyer_memory: dict[str, Any] | None = None,
    authorize_live: bool = False,
    max_live_fetches: int = 3,
    platform_blocked: bool = True,
) -> dict[str, Any]:
    """
    PLATFORM_HISTORY_BLOCKED → exhaust buyer public paths before HISTORY_NOT_AVAILABLE.
    Never creates accounts / solves CAPTCHA / logs in.
    """
    commercial = dict(commercial or {})
    buyer = str(row.get("agency") or row.get("buyer") or "")
    platform = detect_platform(row)
    sid = str(row.get("solicitation_id") or row.get("notice_id") or "")
    attempts: list[dict[str, Any]] = []
    awards: list[dict[str, Any]] = []
    competition = None
    recovery_source = None
    best_grade = GOV_VALUE_D
    best_gov = None
    best_rule = None
    vendor_intel = None
    access_info = classify_access_mode(
        "bidnet anti-bot" if "bidnet" in platform.lower() else f"{platform} {row.get('source_portal') or ''}",
        status="PLATFORM_BLOCKED" if platform_blocked else None,
        http_status=202 if "bidnet" in platform.lower() else None,
        platform=platform,
    )
    # SAM/federal: platform block often means awards are elsewhere (USAspending), not free-reg
    if platform.upper() == "SAM" and access_info["access_mode"] == UNKNOWN_ACCESS_MODE:
        access_info = {
            "access_mode": NO_PUBLIC_HISTORY_FEATURE
            if not platform_blocked
            else UNKNOWN_ACCESS_MODE,
            "platform_history_blocked": bool(platform_blocked),
            "history_not_available": False,
            "rule": "L12_SAM_PIVOT_TO_PUBLIC_RECORDS",
        }
    access_mode = access_info["access_mode"]

    # Fill commercial identity from title when thin
    if not commercial.get("model"):
        try:
            from phase_l.commercial_identity import extract_commercial_model, infer_manufacturer

            blob = f"{row.get('title') or ''} {row.get('description') or ''}"
            mh = extract_commercial_model(blob)
            if mh:
                commercial["model"] = mh.get("model")
            if not commercial.get("manufacturer"):
                mf = infer_manufacturer(blob, model=commercial.get("model"))
                if mf:
                    commercial["manufacturer"] = mf.get("manufacturer")
        except Exception:
            pass

    # Mark platform blocked → must pivot (not HISTORY_NOT_AVAILABLE)
    attempts.append(
        {
            "step": "platform_block_classification",
            "platform": platform,
            "state": PLATFORM_HISTORY_BLOCKED if access_info["platform_history_blocked"] else HISTORY_NOT_AVAILABLE,
            "access_mode": access_mode,
            "rule": access_info.get("rule"),
        }
    )
    update_platform_memory(
        platform,
        access_mode=access_mode,
        anti_bot=access_mode == PUBLIC_ANTI_BOT_BLOCKED,
        public_award_access=False,
    )

    # BidNet: use metadata only, pivot immediately
    if "bidnet" in platform.lower():
        attempts.append(
            {
                "step": "bidnet_metadata_only",
                "buyer": buyer,
                "solicitation_id": sid,
                "title": (row.get("title") or "")[:80],
                "pivot": True,
                "note": "BidNet anti-bot must not terminate exact-history recovery",
            }
        )

    # Memory-first buyer paths
    memory = get_buyer_history_path(buyer)
    attempts.append({"step": "buyer_history_memory", "hit": bool(memory.get("last_successful_recovery")), "path": memory})

    path_disc = discover_buyer_path_urls(
        buyer, solicitation=sid or None, model=str(commercial.get("model") or "") or None
    )
    attempts.append({"step": "buyer_path_discovery", "urls": (path_disc.get("urls") or [])[:8]})

    # Exact solicitation variants (before product-family)
    sol_variants = solicitation_search_variants(sid)
    attempts.append({"step": "exact_solicitation_search", "variants": sol_variants[:6]})

    # Walk public evidence order (deterministic audit trail)
    for step in PUBLIC_EVIDENCE_ORDER:
        hit = False
        note = None
        if step == "buyer_procurement_page":
            hit = bool(path_disc.get("urls"))
            note = "seeded"
        elif step == "exact_solicitation_web_search":
            hit = bool(sol_variants)
            note = "variants_prepared"
        elif step == "exact_product_buyer_history":
            # buyer memory / product graph
            from phase_l.history_graphs import query_product_history

            gq = query_product_history(
                str(commercial.get("model") or commercial.get("mpn") or "") or None,
                manufacturer=str(commercial.get("manufacturer") or "") or None,
                model=str(commercial.get("model") or "") or None,
            )
            hit = bool(gq.get("hit"))
            if hit:
                for ar in (gq["node"].get("awards") or [])[-5:]:
                    aw = normalize_award_tabulation(
                        {
                            "buyer": ar.get("buyer") or buyer,
                            "vendor": ar.get("vendor"),
                            "unit_price": ar.get("price"),
                            "quantity": ar.get("quantity"),
                            "award_date": ar.get("date"),
                            "solicitation_id": ar.get("solicitation") or sid,
                            "manufacturer": commercial.get("manufacturer"),
                            "model": commercial.get("model"),
                            "source": "exact_product_buyer_history",
                            "item": row.get("title"),
                            **(ar.get("award") or {}),
                        }
                    )
                    graded = grade_recovered_award(aw, row=row, commercial=commercial)
                    if graded.get("gov") and graded["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
                        awards.append(aw)
                        recovery_source = "exact_product_buyer_history"
                        best_grade, best_gov, best_rule = graded["grade"], graded["gov"], graded.get("rule_id")
        elif step == "registration_required_classification":
            if access_mode in {FREE_REGISTRATION_REQUIRED, VENDOR_ACCOUNT_REQUIRED, BUYER_SPECIFIC_ACCOUNT_REQUIRED}:
                hit = True
                note = access_mode
        elif step == "manual_auth_required_classification":
            if access_mode in {PRIVATE_RESTRICTED, CAPTCHA_PRESENT, PUBLIC_ANTI_BOT_BLOCKED}:
                hit = True
                note = access_mode
        elif step in {
            "board_council_records",
            "open_data_expenditure",
            "po_check_register",
            "award_result_page",
            "bid_tab_tabulation_files",
        }:
            # Prefer remembered URLs
            mem_url = None
            if step == "board_council_records":
                mem_url = memory.get("board_agenda_system")
            elif step == "open_data_expenditure":
                mem_url = memory.get("finance_open_data_system")
            elif step == "po_check_register":
                mem_url = memory.get("po_check_register_location")
            elif step == "bid_tab_tabulation_files":
                mem_url = memory.get("bid_tab_url_path")
            elif step == "award_result_page":
                mem_url = memory.get("award_results_url")
            hit = bool(mem_url)
            note = mem_url

        attempts.append({"step": step, "attempted": True, "hit": hit, "note": note})

    # Buyer memory price entries (L.6 memory)
    if buyer_memory:
        from phase_l.exact_history_recovery import _memory_scan, BUYER_FIRST_ORDER

        for step in BUYER_FIRST_ORDER:
            hit = _memory_scan(row, commercial=commercial, buyer_memory=buyer_memory, step=step)
            if hit:
                aw = normalize_award_tabulation(
                    {**hit, "source": f"buyer_memory:{step}", "item": hit.get("item") or row.get("title")}
                )
                graded = grade_recovered_award(aw, row=row, commercial=commercial)
                attempts.append({"step": f"memory:{step}", "grade": graded["grade"]})
                if graded.get("gov") and graded["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
                    awards.append(aw)
                    recovery_source = "buyer_procurement"
                    best_grade, best_gov, best_rule = graded["grade"], graded["gov"], graded.get("rule_id")
                    break

    # Optional live board/open-data (bounded, no login)
    if authorize_live and max_live_fetches > 0 and best_grade == GOV_VALUE_D:
        try:
            from phase_l.board_records import PublicBoardPurchaseAdapter, OpenDataPurchaseAdapter
            from phase_l.resilient_fetch import DomainCircuitBreaker, FETCH_OK, FETCH_403, FETCH_BOT_BLOCKED, resilient_fetch

            breaker = DomainCircuitBreaker()
            search_id = {
                "model": commercial.get("model"),
                "manufacturer": commercial.get("manufacturer"),
                "primary_mpn": commercial.get("mpn"),
            }
            # Board
            board = PublicBoardPurchaseAdapter()
            bres = board.research(
                search_id=search_id, row=row, breaker=breaker, max_fetches=min(2, max_live_fetches)
            )
            attempts.append({"step": "live_board_council", "hits": len(bres.get("records") or [])})
            for rec in bres.get("records") or []:
                aw = normalize_award_tabulation(
                    {
                        "buyer": buyer,
                        "vendor": rec.get("vendor"),
                        "manufacturer": commercial.get("manufacturer") or rec.get("manufacturer"),
                        "model": commercial.get("model") or rec.get("model"),
                        "unit_price": rec.get("unit_price") or rec.get("price"),
                        "quantity": rec.get("quantity"),
                        "award_date": rec.get("date"),
                        "source": "board_council",
                        "item": rec.get("description") or row.get("title"),
                        "solicitation_id": sid,
                        "line_matched": True,
                    }
                )
                graded = grade_recovered_award(aw, row=row, commercial=commercial)
                if graded.get("gov") and graded["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
                    awards.append(aw)
                    recovery_source = "board_council"
                    best_grade, best_gov, best_rule = graded["grade"], graded["gov"], graded.get("rule_id")
                    record_successful_recovery(buyer, evidence_type="board_award", source_url=rec.get("url"))

            # Open data
            if best_grade == GOV_VALUE_D:
                oda = OpenDataPurchaseAdapter()
                ores = oda.research(
                    search_id=search_id, breaker=breaker, max_datasets=min(2, max_live_fetches)
                )
                attempts.append({"step": "live_open_data", "hits": len(ores.get("records") or [])})
                for rec in ores.get("records") or []:
                    aw = normalize_award_tabulation(
                        {
                            "buyer": buyer,
                            "vendor": rec.get("vendor"),
                            "unit_price": rec.get("unit_price") or rec.get("price"),
                            "quantity": rec.get("quantity"),
                            "model": commercial.get("model"),
                            "manufacturer": commercial.get("manufacturer"),
                            "source": "open_data",
                            "item": row.get("title"),
                            "solicitation_id": sid,
                        }
                    )
                    graded = grade_recovered_award(aw, row=row, commercial=commercial)
                    if graded.get("gov") and graded["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
                        awards.append(aw)
                        recovery_source = "open_data"
                        best_grade, best_gov, best_rule = graded["grade"], graded["gov"], graded.get("rule_id")
                        record_successful_recovery(
                            buyer, evidence_type="open_data_payment", source_url=rec.get("url")
                        )

            # Probe buyer URLs lightly
            for url in (path_disc.get("urls") or [])[:max_live_fetches]:
                if not breaker.allow(url):
                    continue
                fr = resilient_fetch(url, breaker=breaker, retries=0, read_timeout=8.0)
                attempts.append({"step": "buyer_url_probe", "url": url, "status": fr.status, "http": fr.status_code})
                if fr.status in {FETCH_403, FETCH_BOT_BLOCKED} or fr.status_code in {401, 403, 202}:
                    access_info = classify_access_mode(
                        fr.text[:400] if fr.text else "",
                        status=fr.status,
                        http_status=fr.status_code,
                        platform=platform,
                    )
                    access_mode = access_info["access_mode"]
                if fr.status == FETCH_OK and fr.text and len(fr.text) > 200:
                    # Heuristic dollar+model window
                    model = str(commercial.get("model") or "")
                    if model and model.lower() in fr.text.lower():
                        dollars = re.findall(
                            r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})?)", fr.text[:100000]
                        )
                        if dollars:
                            unit = float(dollars[0].replace(",", ""))
                            aw = normalize_award_tabulation(
                                {
                                    "buyer": buyer,
                                    "unit_price": unit,
                                    "model": commercial.get("model"),
                                    "manufacturer": commercial.get("manufacturer"),
                                    "source": "buyer_procurement_page",
                                    "item": row.get("title"),
                                    "solicitation_id": sid,
                                    "quantity": 1,
                                }
                            )
                            graded = grade_recovered_award(aw, row=row, commercial=commercial)
                            if graded.get("gov") and graded["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
                                awards.append(aw)
                                recovery_source = "buyer_procurement"
                                best_grade, best_gov, best_rule = (
                                    graded["grade"],
                                    graded["gov"],
                                    graded.get("rule_id"),
                                )
                                record_successful_recovery(
                                    buyer, evidence_type="procurement_page", source_url=url
                                )
                                break
        except Exception as exc:
            attempts.append({"step": "live_buyer_paths", "error": str(exc)[:160]})

    # Persist awards + vendor intel
    for aw in awards:
        link_product_history(
            product_key=str(commercial.get("model") or commercial.get("mpn") or row.get("title") or "")[:80],
            manufacturer=commercial.get("manufacturer"),
            mpn_model=str(commercial.get("model") or commercial.get("mpn") or ""),
            buyer=str(aw.get("buyer") or buyer),
            solicitation=str(aw.get("solicitation_id") or sid),
            award=aw,
            vendor=aw.get("vendor"),
            quantity=aw.get("quantity"),
            price=aw.get("unit_price") or aw.get("total"),
            date=aw.get("award_date"),
        )
        if aw.get("vendor"):
            role = classify_prior_awardee_role({"name": aw["vendor"]})
            link_supplier(
                supplier=str(aw["vendor"]),
                manufacturer=str(commercial.get("manufacturer") or ""),
                exact_model=str(commercial.get("model") or "") or None,
                past_gov_awards=[
                    {"buyer": aw.get("buyer"), "price": aw.get("unit_price"), "date": aw.get("award_date")}
                ],
            )
            vendor_intel = {
                "vendor": aw["vendor"],
                "role": role,
                "note": "channel_intel_not_assumed_wholesale",
            }

    if awards:
        competition = extract_competition_from_bid_tab(awards)
        update_platform_memory(platform, buyer_pivot_success=True)

    # Outcome
    if best_grade == GOV_VALUE_A:
        outcome = GOV_UPGRADED_A
    elif best_grade == GOV_VALUE_B:
        outcome = GOV_UPGRADED_B
    elif best_grade == GOV_VALUE_C:
        outcome = GOV_UPGRADED_C
    elif access_info.get("history_not_available") or access_mode == NO_PUBLIC_HISTORY_FEATURE:
        outcome = HISTORY_NOT_AVAILABLE
    elif access_info.get("platform_history_blocked") and not awards:
        # Platform blocked but buyer paths exhausted without hit
        outcome = HISTORY_EVIDENCE_EXHAUSTED if attempts else HISTORY_BUYER_RECORDS_NOT_FOUND
        # Keep PLATFORM_HISTORY_BLOCKED as state marker
    else:
        outcome = HISTORY_BUYER_RECORDS_NOT_FOUND

    freshness = grade_freshness((best_gov or {}).get("date") if best_gov else None)

    reg = None
    if access_mode in {
        FREE_REGISTRATION_REQUIRED,
        VENDOR_ACCOUNT_REQUIRED,
        BUYER_SPECIFIC_ACCOUNT_REQUIRED,
        PUBLIC_ANTI_BOT_BLOCKED,
    } or (
        access_mode == UNKNOWN_ACCESS_MODE
        and "bidnet" in platform.lower()
    ):
        reg_url = None
        account = access_mode
        if "bidnet" in platform.lower() or access_mode == PUBLIC_ANTI_BOT_BLOCKED:
            reg_url = "https://www.bidnetdirect.com/"
            account = FREE_REGISTRATION_REQUIRED
        if platform.upper() != "SAM":
            reg = registration_opportunity(
                platform=platform if platform != "unknown" else "BidNet",
                buyer=buyer,
                access_mode=account if account != UNKNOWN_ACCESS_MODE else FREE_REGISTRATION_REQUIRED,
                registration_url=reg_url or "https://www.bidnetdirect.com/",
                blocked_opportunities=1,
            )
            reg["priority_score"] = history_access_registration_priority(reg)

    return {
        "kind": "AuthWalledHistoryRecoveryResult",
        "build": BUILD,
        "platform": platform,
        "platform_state": PLATFORM_HISTORY_BLOCKED
        if access_info.get("platform_history_blocked")
        else HISTORY_NOT_AVAILABLE,
        "access_mode": access_mode,
        "outcome": outcome,
        "grade_after": best_grade,
        "rule_id": best_rule,
        "gov": best_gov,
        "freshness": freshness,
        "recovery_source": recovery_source,
        "attempts": attempts,
        "awards_found": len(awards),
        "awards": awards[:5],
        "competition": competition,
        "vendor_intel": vendor_intel,
        "registration_opportunity": reg,
        "buyer_history_path": get_buyer_history_path(buyer),
        "document_queries": document_title_queries(row, commercial)[:6],
        "solicitation_variants": sol_variants,
        "create_account": False,
        "auto_register": False,
        "outreach": False,
        "timestamp": _utc(),
    }


def build_registration_priorities(reg_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate by platform for owner registration roadmap."""
    by_plat: dict[str, dict[str, Any]] = {}
    for r in reg_rows:
        if not r:
            continue
        plat = str(r.get("platform") or "unknown")
        agg = by_plat.setdefault(
            plat,
            {
                "platform": plat,
                "buyers": set(),
                "opportunities_blocked": 0,
                "potential_value": 0.0,
                "registration_url": r.get("registration_url"),
                "account_type": r.get("account_type") or r.get("access_mode"),
                "estimated_setup_effort": r.get("estimated_setup_effort") or "medium",
                "registration_cost": r.get("registration_cost") or 0,
                "create_account": False,
            },
        )
        agg["buyers"].add(str(r.get("buyer") or "")[:80])
        agg["opportunities_blocked"] += int(r.get("number_of_current_blocked_opportunities") or 1)
        agg["potential_value"] += float(r.get("potential_quote_target_value_blocked") or 0)

    out = []
    for plat, agg in by_plat.items():
        buyers_n = len(agg["buyers"])
        rec = {
            "platform": plat,
            "buyers_unlocked": buyers_n,
            "opportunities_blocked": agg["opportunities_blocked"],
            "likely_history_records_unlocked": agg["opportunities_blocked"],
            "potential_economic_value_blocked": round(agg["potential_value"], 2),
            "recurring_buyer_value": buyers_n,
            "setup_burden": agg["estimated_setup_effort"],
            "registration_url": agg["registration_url"],
            "account_type": agg["account_type"],
            "registration_cost": agg["registration_cost"],
            "create_account": False,
            "recommendation": "owner_register_if_high_priority" if agg["opportunities_blocked"] >= 5 else "defer",
        }
        rec["priority_score"] = history_access_registration_priority(
            {
                "number_of_current_blocked_opportunities": rec["opportunities_blocked"],
                "potential_quote_target_value_blocked": rec["potential_economic_value_blocked"],
                "buyers_on_same_platform": buyers_n,
                "registration_cost": rec["registration_cost"],
                "estimated_setup_effort": rec["setup_burden"],
            }
        )
        out.append(rec)
    out.sort(key=lambda x: -x["priority_score"])
    return out
