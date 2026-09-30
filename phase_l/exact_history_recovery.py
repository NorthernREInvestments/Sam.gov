"""Phase L.11 — exact award-history recovery state machine (no evidence bar lowering)."""

from __future__ import annotations

import re
from typing import Any

from application_clock import now_utc
from phase_l.history_graphs import (
    link_product_history,
    link_supplier,
    normalize_award_tabulation,
    query_product_history,
)
from phase_l.quality_audit import (
    GOV_VALUE_A,
    GOV_VALUE_B,
    GOV_VALUE_C,
    GOV_VALUE_D,
    grade_government_value,
)
from phase_l.quote_economics import GOV_VALUE_EXACT, GOV_VALUE_STRONG, GOV_VALUE_COMPARABLE, _f

BUILD = "20260928-m3-phase-l11-exact-award-history-recovery"

EXACT_HISTORY_RECOVERY = "EXACT_HISTORY_RECOVERY"
GOV_UPGRADED_A = "GOV_UPGRADED_A"
GOV_UPGRADED_B = "GOV_UPGRADED_B"
GOV_UPGRADED_C = "GOV_UPGRADED_C"
HISTORY_AUTH_REQUIRED = "HISTORY_AUTH_REQUIRED"
HISTORY_DOCUMENT_MISSING = "HISTORY_DOCUMENT_MISSING"
HISTORY_SOURCE_BLOCKED = "HISTORY_SOURCE_BLOCKED"
HISTORY_BUYER_RECORDS_NOT_FOUND = "HISTORY_BUYER_RECORDS_NOT_FOUND"
HISTORY_EVIDENCE_EXHAUSTED = "HISTORY_EVIDENCE_EXHAUSTED"

PUBLIC_REGISTRATION_REQUIRED = "PUBLIC_REGISTRATION_REQUIRED"
AUTH_ACCOUNT_REQUIRED = "AUTH_ACCOUNT_REQUIRED"
BUYER_VENDOR_ACCOUNT_REQUIRED = "BUYER_VENDOR_ACCOUNT_REQUIRED"
PRIVATE_RESTRICTED = "PRIVATE_RESTRICTED"
UNKNOWN_AUTH = "UNKNOWN_AUTH"

LOT_PRICE = "LOT_PRICE"

# Deterministic promotion rule IDs
RULE_GOV_A_SAME_BUYER_EXACT_AWARD = "L11_GOV_A_SAME_BUYER_EXACT_AWARD"
RULE_GOV_A_EXACT_BID_TAB_LINE = "L11_GOV_A_EXACT_BID_TAB_LINE"
RULE_GOV_A_BOARD_APPROVED = "L11_GOV_A_BOARD_APPROVED"
RULE_GOV_A_CURRENT_BUDGET = "L11_GOV_A_CURRENT_BUDGET"
RULE_GOV_A_NSN_EXACT = "L11_GOV_A_NSN_EXACT"
RULE_GOV_B_OTHER_GOV_EXACT_MODEL = "L11_GOV_B_OTHER_GOV_EXACT_MODEL"
RULE_GOV_B_SAME_BUYER_NEAR_CONFIG = "L11_GOV_B_SAME_BUYER_NEAR_CONFIG"
RULE_GOV_B_REPEATED_STABLE = "L11_GOV_B_REPEATED_STABLE"
RULE_GOV_C_NEAR_FAMILY = "L11_GOV_C_NEAR_FAMILY"
RULE_GOV_C_MODEL_BAND = "L11_GOV_C_MODEL_BAND"

BUYER_FIRST_ORDER = (
    "same_buyer_solicitation_family",
    "same_buyer_exact_manufacturer_model",
    "same_buyer_exact_mpn_sku",
    "same_buyer_exact_product_description",
    "same_buyer_prior_award",
    "same_buyer_bid_tab",
    "same_buyer_contract_award",
    "same_buyer_board_council",
    "same_buyer_purchase_order",
    "same_buyer_check_expenditure",
    "same_buyer_contract_renewal",
    "same_buyer_archived_solicitation",
)

DOC_TITLE_TERMS = (
    "bid tabulation",
    "bid tab",
    "award tab",
    "results",
    "notice of award",
    "recommendation of award",
    "purchase recommendation",
    "bid summary",
    "quote summary",
    "evaluation summary",
    "contract award",
    "council agenda",
    "board packet",
    "purchase order",
    "contract amendment",
    "renewal",
)


def _utc() -> str:
    return now_utc().isoformat()


def _norm(s: Any) -> str:
    return str(s or "").strip().upper()


def classify_auth_wall(blob: str | None, *, status: str | None = None) -> str:
    """Legacy L.11 surface — delegates to L.12 taxonomy, maps back for callers."""
    from phase_l.auth_access import (
        BUYER_SPECIFIC_ACCOUNT_REQUIRED as L12_BUYER,
        FREE_REGISTRATION_REQUIRED as L12_FREE,
        PRIVATE_RESTRICTED as L12_PRIVATE,
        PUBLIC_ANTI_BOT_BLOCKED as L12_ANTIBOT,
        VENDOR_ACCOUNT_REQUIRED as L12_VENDOR,
        CAPTCHA_PRESENT as L12_CAPTCHA,
        classify_access_mode,
    )

    mode = classify_access_mode(blob, status=status)["access_mode"]
    return {
        L12_FREE: PUBLIC_REGISTRATION_REQUIRED,
        L12_VENDOR: BUYER_VENDOR_ACCOUNT_REQUIRED,
        L12_BUYER: BUYER_VENDOR_ACCOUNT_REQUIRED,
        L12_PRIVATE: PRIVATE_RESTRICTED,
        L12_ANTIBOT: UNKNOWN_AUTH,
        L12_CAPTCHA: UNKNOWN_AUTH,
    }.get(mode, UNKNOWN_AUTH if mode else UNKNOWN_AUTH)


def solicitation_search_variants(solicitation_id: str | None) -> list[str]:
    sid = str(solicitation_id or "").strip()
    if not sid:
        return []
    norm = re.sub(r"[\s_\-]+", "", sid)
    variants = [
        f'"{sid}"',
        norm,
        f"{sid} award",
        f"{sid} tabulation",
        f"{sid} vendor",
        f"{sid} filetype:pdf",
        f"{sid} filetype:xlsx",
        f'"{sid}" bid tab',
    ]
    # dedupe preserve order
    out: list[str] = []
    seen: set[str] = set()
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def document_title_queries(row: dict[str, Any], commercial: dict[str, Any] | None = None) -> list[str]:
    commercial = commercial or {}
    buyer = str(row.get("agency") or row.get("buyer") or "")[:50]
    model = str(commercial.get("model") or commercial.get("mpn") or "")
    sid = str(row.get("solicitation_id") or row.get("notice_id") or "")
    qs: list[str] = []
    for term in DOC_TITLE_TERMS[:8]:
        parts = [p for p in (f'"{buyer}"' if buyer else "", f'"{model}"' if model else "", sid, term) if p]
        if len(parts) >= 2:
            qs.append(" ".join(parts))
    return qs[:12]


def configuration_compatible(
    live: dict[str, Any],
    award: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Exact match requirements for A/B — wrong trim/options ≠ A."""
    commercial = commercial or {}
    live_model = _norm(commercial.get("model") or live.get("model") or "")
    award_model = _norm(award.get("model") or award.get("mpn") or "")
    live_mfr = _norm(commercial.get("manufacturer") or live.get("manufacturer") or "")
    award_mfr = _norm(award.get("manufacturer") or "")
    live_blob = f"{live.get('title') or ''} {commercial.get('description') or ''}".lower()
    award_blob = f"{award.get('item') or ''} {award.get('description') or ''}".lower()

    differences: list[str] = []
    model_ok = bool(live_model and award_model and (live_model in award_model or award_model in live_model))
    mfr_ok = bool(live_mfr and award_mfr and live_mfr.split()[0] in award_mfr)
    nsn_live = _norm(commercial.get("nsn") or live.get("nsn"))
    nsn_award = _norm(award.get("nsn"))
    nsn_ok = bool(nsn_live and nsn_award and nsn_live.replace("-", "") == nsn_award.replace("-", ""))

    # Config risk signals
    for token in ("upfit", "loaded", "package", "with plow", "with bucket", "ambulance", "ssv", "police"):
        in_live = token in live_blob
        in_award = token in award_blob
        if in_live != in_award:
            differences.append(f"config_token_mismatch:{token}")

    exact = (nsn_ok or (model_ok and mfr_ok)) and not differences
    near = (model_ok or nsn_ok) and len(differences) <= 1
    family = mfr_ok or (live_model[:4] and live_model[:4] in award_model)
    return {
        "exact": exact,
        "near": near,
        "family": family,
        "differences": differences,
        "model_ok": model_ok,
        "mfr_ok": mfr_ok,
        "nsn_ok": nsn_ok,
    }


def grade_recovered_award(
    award: dict[str, Any],
    *,
    row: dict[str, Any],
    commercial: dict[str, Any] | None = None,
) -> dict[str, Any]:
    commercial = commercial or {}
    buyer_live = _norm(row.get("agency") or row.get("buyer"))
    buyer_award = _norm(award.get("buyer"))
    same_buyer = bool(buyer_live and buyer_award and (buyer_live[:24] in buyer_award or buyer_award[:24] in buyer_live))
    compat = configuration_compatible(row, award, commercial=commercial)
    unit = _f(award.get("unit_price") or award.get("unit_value"))
    total = _f(award.get("total") or award.get("award_amount"))
    qty = _f(award.get("quantity"))
    is_lot = bool(award.get("lot_price") or award.get("price_basis") == LOT_PRICE)

    if is_lot and not unit:
        return {
            "grade": GOV_VALUE_D,
            "rule_id": None,
            "reason": "lot_price_not_unit",
            "compat": compat,
            "gov": {
                "state": "LOT_VALUE",
                "tier": "D",
                "unit_value": None,
                "total_value": total,
                "price_basis": LOT_PRICE,
                "source": award.get("source"),
                "note": "lot_total_not_decomposed_to_unit",
            },
        }

    # Never invent unit from total without quantity
    if unit is None and total is not None and qty and qty > 0:
        unit = total / qty
    elif unit is None and total is not None and not qty:
        return {
            "grade": GOV_VALUE_D,
            "rule_id": None,
            "reason": "total_without_quantity_not_unit",
            "compat": compat,
            "gov": None,
        }

    if not unit and not total:
        return {"grade": GOV_VALUE_D, "rule_id": None, "reason": "no_price", "compat": compat, "gov": None}

    if same_buyer and compat["exact"]:
        src = str(award.get("source") or "")
        if "bid_tab" in src.lower() or award.get("line_matched"):
            rule = RULE_GOV_A_EXACT_BID_TAB_LINE
        elif "board" in src.lower() or "council" in src.lower():
            rule = RULE_GOV_A_BOARD_APPROVED
        elif compat.get("nsn_ok"):
            rule = RULE_GOV_A_NSN_EXACT
        else:
            rule = RULE_GOV_A_SAME_BUYER_EXACT_AWARD
        return {
            "grade": GOV_VALUE_A,
            "rule_id": rule,
            "reason": "same_buyer_exact",
            "compat": compat,
            "gov": {
                "state": GOV_VALUE_EXACT,
                "tier": "A",
                "unit_value": unit,
                "total_value": total,
                "date": award.get("award_date"),
                "source": award.get("source") or "EXACT_BUYER_AWARD",
                "match_rationale": f"same buyer exact ({rule})",
                "final_award_value": True,
                "vendor": award.get("vendor"),
            },
        }

    if compat["exact"] and not same_buyer:
        return {
            "grade": GOV_VALUE_B,
            "rule_id": RULE_GOV_B_OTHER_GOV_EXACT_MODEL,
            "reason": "other_gov_exact_model",
            "compat": compat,
            "gov": {
                "state": GOV_VALUE_STRONG,
                "tier": "B",
                "unit_value": unit,
                "total_value": total,
                "date": award.get("award_date"),
                "source": award.get("source") or "OTHER_GOV_EXACT_AWARD",
                "match_rationale": "exact model other government buyer",
                "final_award_value": True,
                "vendor": award.get("vendor"),
            },
        }

    if same_buyer and compat["near"]:
        return {
            "grade": GOV_VALUE_B,
            "rule_id": RULE_GOV_B_SAME_BUYER_NEAR_CONFIG,
            "reason": "same_buyer_near_config",
            "compat": compat,
            "gov": {
                "state": GOV_VALUE_STRONG,
                "tier": "B",
                "unit_value": unit,
                "date": award.get("award_date"),
                "source": award.get("source") or "SAME_BUYER_NEAR_CONFIG",
                "match_rationale": f"near config diffs={compat['differences']}",
                "final_award_value": False,
                "vendor": award.get("vendor"),
            },
        }

    if compat["near"] or compat["family"]:
        return {
            "grade": GOV_VALUE_C,
            "rule_id": RULE_GOV_C_NEAR_FAMILY,
            "reason": "near_family",
            "compat": compat,
            "gov": {
                "state": GOV_VALUE_COMPARABLE,
                "tier": "C",
                "unit_value": unit,
                "date": award.get("award_date"),
                "source": award.get("source") or "NEAR_FAMILY_AWARD",
                "match_rationale": f"diffs={compat['differences']}",
                "final_award_value": False,
                "vendor": award.get("vendor"),
            },
        }

    return {"grade": GOV_VALUE_D, "rule_id": None, "reason": "incompatible", "compat": compat, "gov": None}


def _memory_scan(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None,
    buyer_memory: dict[str, Any] | None,
    step: str,
) -> dict[str, Any] | None:
    commercial = commercial or {}
    buyer_memory = buyer_memory or {}
    entries = buyer_memory.get("entries") or buyer_memory.get("by_buyer") or buyer_memory
    if not isinstance(entries, dict):
        return None
    buyer = _norm(row.get("agency") or row.get("buyer"))
    model = _norm(commercial.get("model"))
    mpn = _norm(commercial.get("mpn") or commercial.get("sku"))
    title = _norm(row.get("title"))[:40]
    sid = _norm(row.get("solicitation_id") or row.get("notice_id"))

    for key, val in entries.items():
        if not isinstance(val, dict):
            continue
        b = _norm(val.get("buyer") or val.get("agency") or str(key).split("|")[0])
        if buyer and b and buyer[:18] not in b and b[:18] not in buyer:
            if step.startswith("same_buyer"):
                continue
        hit = False
        if step == "same_buyer_solicitation_family" and sid and sid[:8] in _norm(val.get("solicitation_id") or key):
            hit = True
        elif step == "same_buyer_exact_manufacturer_model" and model and model in _norm(val.get("model") or key):
            hit = True
        elif step == "same_buyer_exact_mpn_sku" and mpn and mpn in _norm(val.get("mpn") or val.get("sku") or key):
            hit = True
        elif step == "same_buyer_exact_product_description" and title and title[:20] in _norm(val.get("item") or val.get("title") or key):
            hit = True
        elif step in {
            "same_buyer_prior_award",
            "same_buyer_bid_tab",
            "same_buyer_contract_award",
            "same_buyer_board_council",
            "same_buyer_purchase_order",
            "same_buyer_check_expenditure",
            "same_buyer_contract_renewal",
            "same_buyer_archived_solicitation",
        }:
            # soft: same buyer + any price
            if buyer and b and (_f(val.get("unit_value") or val.get("unit_price") or val.get("total_value"))):
                if model and model in _norm(val.get("model") or key):
                    hit = True
                elif mpn and mpn in _norm(val.get("mpn") or key):
                    hit = True
        if hit:
            return {**val, "memory_key": key, "search_step": step, "buyer": val.get("buyer") or b}
    return None


def run_exact_history_recovery(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
    buyer_memory: dict[str, Any] | None = None,
    authorize_live: bool = False,
    max_live_fetches: int = 3,
) -> dict[str, Any]:
    """Buyer-first exact history recovery for one Gov D (or weak) row."""
    from phase_l.platform_history import detect_platform, fetch_platform_history, run_buyer_pivot
    from phase_l.supplier_upgrade import classify_prior_awardee_role

    commercial = dict(commercial or {})
    history = history or {}
    attempts: list[dict[str, Any]] = []
    awards_found: list[dict[str, Any]] = []
    auth_class: str | None = None
    outcome = HISTORY_EVIDENCE_EXHAUSTED
    best_grade = GOV_VALUE_D
    best_gov: dict[str, Any] | None = None
    best_rule: str | None = None
    vendor_intel: dict[str, Any] | None = None

    # Fill thin commercial identity from title
    if not commercial.get("model"):
        try:
            from phase_l.commercial_identity import extract_commercial_model, infer_manufacturer

            blob = f"{row.get('title') or ''} {row.get('description') or ''}"
            mh = extract_commercial_model(blob)
            if mh and mh.get("model"):
                commercial["model"] = mh["model"]
            if not commercial.get("manufacturer"):
                mf = infer_manufacturer(blob, model=commercial.get("model"))
                if mf:
                    commercial["manufacturer"] = mf.get("manufacturer")
        except Exception:
            pass

    # 0) Row-embedded historical award (already recovered earlier)
    if row.get("historical_award_unit_price") or history.get("historical_award_unit_price"):
        aw = normalize_award_tabulation(
            {
                "buyer": row.get("agency") or history.get("buyer"),
                "solicitation_id": row.get("solicitation_id"),
                "vendor": history.get("historical_awardee") or row.get("historical_awardee"),
                "manufacturer": commercial.get("manufacturer"),
                "model": commercial.get("model") or commercial.get("mpn") or commercial.get("nsn"),
                "nsn": commercial.get("nsn") or row.get("nsn"),
                "unit_price": row.get("historical_award_unit_price") or history.get("historical_award_unit_price"),
                "total": row.get("historical_award_price") or history.get("historical_award_price"),
                "quantity": row.get("quantity") or history.get("quantity"),
                "award_date": history.get("award_date") or history.get("most_recent_date"),
                "source": "historical_award_unit_price",
                "item": row.get("title"),
            }
        )
        graded = grade_recovered_award(aw, row=row, commercial=commercial)
        attempts.append({"step": "embedded_historical_award", "grade": graded["grade"], "rule": graded.get("rule_id")})
        if graded.get("gov") and graded["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
            awards_found.append(aw)
            best_grade, best_gov, best_rule = graded["grade"], graded["gov"], graded.get("rule_id")

    # 1) Buyer-first memory walk
    for step in BUYER_FIRST_ORDER:
        hit = _memory_scan(row, commercial=commercial, buyer_memory=buyer_memory, step=step)
        attempts.append({"step": step, "hit": bool(hit)})
        if not hit:
            continue
        aw = normalize_award_tabulation({**hit, "source": f"buyer_memory:{step}", "item": hit.get("item") or row.get("title")})
        graded = grade_recovered_award(aw, row=row, commercial=commercial)
        if graded.get("gov") and graded["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
            awards_found.append(aw)
            if graded["grade"] == GOV_VALUE_A or (
                graded["grade"] == GOV_VALUE_B and best_grade not in {GOV_VALUE_A}
            ) or (graded["grade"] == GOV_VALUE_C and best_grade == GOV_VALUE_D):
                best_grade, best_gov, best_rule = graded["grade"], graded["gov"], graded.get("rule_id")

    # 2b) Enrichment / USAspending path for federal identity keys
    if authorize_live or row.get("nsn") or commercial.get("nsn") or commercial.get("mpn"):
        try:
            from phase_l.enrichment import lookup_government_history, load_cache

            cache = load_cache()
            budget = {"usaspending": 0, "usaspending_max": 2 if authorize_live else 0}
            if authorize_live and budget["usaspending_max"] > 0:
                hist2 = lookup_government_history(
                    row,
                    {
                        "nsn": commercial.get("nsn") or row.get("nsn"),
                        "mpn": commercial.get("mpn"),
                        "manufacturer": commercial.get("manufacturer"),
                        "model": commercial.get("model"),
                    },
                    budget=budget,
                    cache=cache,
                    authorize_live=authorize_live,
                )
            else:
                hist2 = {}
                # cache-only
                for _k, val in (cache or {}).items():
                    if not isinstance(val, dict):
                        continue
                    h = val.get("government_history") or val
                    if h.get("historical_award_unit_price") and (
                        (commercial.get("nsn") and str(commercial.get("nsn")) in str(_k))
                        or (commercial.get("mpn") and str(commercial.get("mpn")).upper() in str(_k).upper())
                        or (commercial.get("model") and str(commercial.get("model")).upper() in str(_k).upper())
                    ):
                        hist2 = h
                        break
            attempts.append(
                {
                    "step": "enrichment_usaspending",
                    "hit": bool(hist2.get("historical_award_unit_price")),
                    "live": bool(authorize_live),
                }
            )
            if hist2.get("historical_award_unit_price"):
                aw = normalize_award_tabulation(
                    {
                        "buyer": hist2.get("buyer") or row.get("agency"),
                        "vendor": hist2.get("historical_awardee"),
                        "unit_price": hist2["historical_award_unit_price"],
                        "total": hist2.get("historical_award_price"),
                        "nsn": commercial.get("nsn") or row.get("nsn"),
                        "model": commercial.get("model") or commercial.get("mpn") or commercial.get("nsn"),
                        "manufacturer": commercial.get("manufacturer"),
                        "source": "usaspending_or_cache",
                        "item": row.get("title"),
                        "quantity": hist2.get("quantity") or row.get("quantity") or 1,
                        "award_date": hist2.get("most_recent_date") or hist2.get("award_date"),
                    }
                )
                graded = grade_recovered_award(aw, row=row, commercial=commercial)
                if graded.get("gov") and graded["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
                    awards_found.append(aw)
                    if graded["grade"] == GOV_VALUE_A or (
                        graded["grade"] == GOV_VALUE_B and best_grade != GOV_VALUE_A
                    ) or (graded["grade"] == GOV_VALUE_C and best_grade == GOV_VALUE_D):
                        best_grade, best_gov, best_rule = graded["grade"], graded["gov"], graded.get("rule_id")
        except Exception as exc:
            attempts.append({"step": "enrichment_usaspending", "error": str(exc)[:120]})

    # 2) Product history graph
    gq = query_product_history(
        str(commercial.get("model") or commercial.get("mpn") or "") or None,
        manufacturer=str(commercial.get("manufacturer") or "") or None,
        model=str(commercial.get("model") or "") or None,
    )
    attempts.append({"step": "product_history_graph", "hit": gq.get("hit")})
    if gq.get("hit") and gq.get("node"):
        for ar in (gq["node"].get("awards") or [])[-5:]:
            aw = normalize_award_tabulation(
                {
                    "buyer": ar.get("buyer"),
                    "vendor": ar.get("vendor"),
                    "unit_price": ar.get("price"),
                    "quantity": ar.get("quantity"),
                    "award_date": ar.get("date"),
                    "solicitation_id": ar.get("solicitation"),
                    "manufacturer": commercial.get("manufacturer"),
                    "model": commercial.get("model"),
                    "source": "product_history_graph",
                    "item": row.get("title"),
                    **(ar.get("award") or {}),
                }
            )
            graded = grade_recovered_award(aw, row=row, commercial=commercial)
            if graded.get("gov") and graded["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
                awards_found.append(aw)
                if graded["grade"] == GOV_VALUE_A or (
                    graded["grade"] == GOV_VALUE_B and best_grade != GOV_VALUE_A
                ) or (graded["grade"] == GOV_VALUE_C and best_grade == GOV_VALUE_D):
                    best_grade, best_gov, best_rule = graded["grade"], graded["gov"], graded.get("rule_id")

    # 3) Platform history + buyer pivot
    plat = fetch_platform_history(row, authorize_live=False)
    attempts.append(
        {
            "step": "platform_history",
            "platform": plat.get("platform"),
            "status": plat.get("adapter_status"),
            "awards": len(plat.get("awards_normalized") or []),
        }
    )
    for aw in plat.get("awards_normalized") or []:
        graded = grade_recovered_award(aw, row=row, commercial=commercial)
        if graded.get("gov") and graded["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
            awards_found.append(aw)
            best_grade, best_gov, best_rule = graded["grade"], graded["gov"], graded.get("rule_id")

    pivot = run_buyer_pivot(row, commercial=commercial, authorize_live=authorize_live, max_fetches=max_live_fetches)
    attempts.extend(pivot.get("attempts") or [])
    if pivot.get("auth_class"):
        auth_class = pivot["auth_class"]
    for aw in pivot.get("awards") or []:
        graded = grade_recovered_award(aw, row=row, commercial=commercial)
        if graded.get("gov") and graded["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
            awards_found.append(aw)
            if graded["grade"] == GOV_VALUE_A or (
                graded["grade"] == GOV_VALUE_B and best_grade != GOV_VALUE_A
            ) or (graded["grade"] == GOV_VALUE_C and best_grade == GOV_VALUE_D):
                best_grade, best_gov, best_rule = graded["grade"], graded["gov"], graded.get("rule_id")

    # 4) Solicitation-number / doc-title query ledger (for auth gap / future)
    sid = row.get("solicitation_id") or row.get("notice_id")
    sol_variants = solicitation_search_variants(str(sid) if sid else None)
    doc_qs = document_title_queries(row, commercial)
    attempts.append({"step": "solicitation_number_search", "variants": sol_variants[:5], "executed_live": False})
    attempts.append({"step": "document_title_discovery", "queries": doc_qs[:4], "executed_live": authorize_live})

    # 5) Optional live board/state adapters (bounded)
    if authorize_live and best_grade == GOV_VALUE_D and max_live_fetches > 0:
        try:
            from phase_l.board_records import PublicBoardPurchaseAdapter
            from phase_l.resilient_fetch import DomainCircuitBreaker

            adapter = PublicBoardPurchaseAdapter()
            breaker = DomainCircuitBreaker()
            search_id = {
                "model": commercial.get("model"),
                "manufacturer": commercial.get("manufacturer"),
                "primary_mpn": commercial.get("mpn"),
                "sku": commercial.get("sku"),
            }
            res = adapter.research(search_id=search_id, row=row, breaker=breaker, max_fetches=min(2, max_live_fetches))
            attempts.append({"step": "board_records_live", "hits": len(res.get("records") or []), "tele": res.get("telemetry")})
            for rec in res.get("records") or []:
                aw = normalize_award_tabulation(
                    {
                        "buyer": row.get("agency"),
                        "vendor": rec.get("vendor"),
                        "manufacturer": commercial.get("manufacturer") or rec.get("manufacturer"),
                        "model": commercial.get("model") or rec.get("model"),
                        "unit_price": rec.get("unit_price") or rec.get("price"),
                        "quantity": rec.get("quantity"),
                        "award_date": rec.get("date"),
                        "source": "board_records",
                        "item": rec.get("description") or row.get("title"),
                    }
                )
                graded = grade_recovered_award(aw, row=row, commercial=commercial)
                if graded.get("gov") and graded["grade"] in {GOV_VALUE_A, GOV_VALUE_B, GOV_VALUE_C}:
                    awards_found.append(aw)
                    best_grade, best_gov, best_rule = graded["grade"], graded["gov"], graded.get("rule_id")
        except Exception as exc:
            attempts.append({"step": "board_records_live", "error": str(exc)[:120]})

    # Persist recovered awards into graphs
    for aw in awards_found:
        link_product_history(
            product_key=str(commercial.get("model") or commercial.get("mpn") or row.get("title") or "")[:80],
            manufacturer=commercial.get("manufacturer"),
            mpn_model=str(commercial.get("model") or commercial.get("mpn") or ""),
            buyer=str(aw.get("buyer") or row.get("agency") or ""),
            solicitation=str(aw.get("solicitation_id") or ""),
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
                past_gov_awards=[{"buyer": aw.get("buyer"), "price": aw.get("unit_price"), "date": aw.get("award_date")}],
            )
            vendor_intel = {"vendor": aw["vendor"], "role": role, "note": "channel_intel_not_assumed_wholesale"}

    # Outcome classification
    if best_grade == GOV_VALUE_A:
        outcome = GOV_UPGRADED_A
    elif best_grade == GOV_VALUE_B:
        outcome = GOV_UPGRADED_B
    elif best_grade == GOV_VALUE_C:
        outcome = GOV_UPGRADED_C
    elif auth_class or pivot.get("auth_required"):
        outcome = HISTORY_AUTH_REQUIRED
    elif pivot.get("blocked"):
        outcome = HISTORY_SOURCE_BLOCKED
    elif pivot.get("documents_missing"):
        outcome = HISTORY_DOCUMENT_MISSING
    elif not awards_found:
        outcome = HISTORY_BUYER_RECORDS_NOT_FOUND if not pivot.get("buyer_domain_tried") else HISTORY_EVIDENCE_EXHAUSTED
    else:
        outcome = HISTORY_EVIDENCE_EXHAUSTED

    # Re-grade via quality_audit for consistency when we have gov dict
    if best_gov:
        ginfo = grade_government_value(best_gov, commercial=commercial, history=history)
        best_grade = ginfo["grade"]

    return {
        "kind": "ExactHistoryRecoveryResult",
        "build": BUILD,
        "state": EXACT_HISTORY_RECOVERY,
        "outcome": outcome,
        "grade_after": best_grade,
        "rule_id": best_rule,
        "gov": best_gov,
        "attempts": attempts,
        "awards_found": len(awards_found),
        "awards": awards_found[:5],
        "auth_class": auth_class,
        "vendor_intel": vendor_intel,
        "solicitation_variants": sol_variants,
        "document_queries": doc_qs[:6],
        "platform": detect_platform(row),
        "commercial": {
            "manufacturer": commercial.get("manufacturer"),
            "model": commercial.get("model"),
            "mpn": commercial.get("mpn"),
        },
        "timestamp": _utc(),
    }
