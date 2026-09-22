"""µLab automated research + quote prep — evidence preservation, no fabricated costs.

Public market ≠ estimated supplier cost ≠ actual quote ≠ historical government ≠ HE bid.
Only an actual supplier quote may support BID_CANDIDATE (enforced by economics.derive_status).
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from application_clock import now_utc
from micro_purchase_lab_config import HISTORICAL_MARKET_WINDOW_DAYS
from micro_purchase_lab_economics import (
    D,
    historical_equivalent_bid,
    historical_market_in_window,
    is_quote_expired,
    money,
)

BUILD_TAG = "20260922-m3-micro-lab-automation-1"

STAGE_KEYS = (
    "opportunity",
    "product_identity",
    "government_history",
    "historical_market",
    "current_market",
    "supplier_channels",
    "pricing_model",
    "quote_prep",
)

STATUS_COMPLETE = "COMPLETE"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAILED = "FAILED"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_NOT_RUN = "NOT_RUN"

CONF_VERIFIED = "VERIFIED"
CONF_STRONG = "STRONG"
CONF_MODERATE = "MODERATE"
CONF_WEAK = "WEAK"
CONF_UNKNOWN = "UNKNOWN"

IDENTITY_EXACT = "EXACT"
IDENTITY_STRONG = "STRONG"
IDENTITY_POSSIBLE = "POSSIBLE"
IDENTITY_UNKNOWN = "UNKNOWN"

MATCH_EXACT = "EXACT"
MATCH_STRONG = "STRONG"
MATCH_POSSIBLE = "POSSIBLE"
MATCH_NOT_EXACT = "NOT_EXACT"

# Research budgets (per opportunity) — cost discipline
DEFAULT_BUDGETS = {
    "max_source_queries": 8,
    "max_product_candidate_checks": 6,
    "max_historical_price_checks": 8,
    "max_current_price_checks": 8,
    "max_supplier_channel_checks": 10,
    "max_ai_calls": 1,
}

MIN_DEADLINE_RUNWAY_DAYS = 2

# Evidence type quality rank (higher = better)
EVIDENCE_TYPE_RANK = {
    "MANUFACTURER_PRICE_LIST": 100,
    "AUTHORIZED_DISTRIBUTOR": 90,
    "WHOLESALE": 80,
    "RESELLER": 70,
    "DISTRIBUTOR_CATALOG": 65,
    "RETAIL": 50,
    "ARCHIVED_RETAIL": 45,
    "MARKETPLACE_NEW": 35,
    "IMPORT_EXPORT": 15,
    "OTHER": 10,
}

CONDITION_OK = {"NEW_OEM", "NEW_AUTHORIZED", "NEW"}
CONDITION_BAD = {"USED", "REFURBISHED", "SURPLUS_NEW"}

QUOTE_STATUS_NOT_PREPARED = "NOT_PREPARED"
QUOTE_STATUS_READY = "READY_TO_REQUEST"
QUOTE_STATUS_REQUESTED = "REQUESTED"
QUOTE_STATUS_RECEIVED = "RECEIVED"
QUOTE_STATUS_EXPIRED = "EXPIRED"
QUOTE_STATUS_DECLINED = "DECLINED"
QUOTE_STATUS_NO_RESPONSE = "NO_RESPONSE"
QUOTE_STATUS_NOT_ELIGIBLE = "NOT_ELIGIBLE"

NEXT_ACTIONS = (
    "RESEARCH_OPPORTUNITY",
    "REVIEW_PRODUCT_IDENTITY",
    "RESOLVE_UNITS",
    "REVIEW_HISTORICAL_EVIDENCE",
    "REVIEW_CURRENT_MARKET",
    "PREPARE_SUPPLIER_QUOTES",
    "REQUEST_SUPPLIER_QUOTE",
    "ENTER_SUPPLIER_QUOTE",
    "REVIEW_ECONOMICS",
    "RESOLVE_FUNDING",
    "BID_CANDIDATE",
    "REJECT",
)

# Built-in industrial / OEM channel seeds (research pointers only)
CHANNEL_SEEDS: list[dict[str, Any]] = [
    {"supplier": "MSC Industrial", "website": "https://www.mscdirect.com", "supplier_type": "AUTHORIZED_DISTRIBUTOR", "quote_required": True, "account_required": True},
    {"supplier": "MSC ResaleLink", "website": "https://www.mscdirect.com", "supplier_type": "WHOLESALE_RESELLER", "quote_required": True, "account_required": True},
    {"supplier": "Grainger", "website": "https://www.grainger.com", "supplier_type": "INDUSTRIAL_DISTRIBUTOR", "quote_required": True, "account_required": True},
    {"supplier": "Zoro", "website": "https://www.zoro.com", "supplier_type": "RESELLER", "quote_required": False, "account_required": False},
    {"supplier": "Zoro Reseller", "website": "https://www.zoro.com", "supplier_type": "WHOLESALE_RESELLER", "quote_required": True, "account_required": True},
    {"supplier": "Fastenal", "website": "https://www.fastenal.com", "supplier_type": "INDUSTRIAL_DISTRIBUTOR", "quote_required": True, "account_required": True},
    {"supplier": "Ingram Micro", "website": "https://www.ingrammicro.com", "supplier_type": "IT_DISTRIBUTOR", "quote_required": True, "account_required": True},
    {"supplier": "TD SYNNEX", "website": "https://www.tdsynnex.com", "supplier_type": "IT_DISTRIBUTOR", "quote_required": True, "account_required": True},
    {"supplier": "D&H", "website": "https://www.dandh.com", "supplier_type": "IT_DISTRIBUTOR", "quote_required": True, "account_required": True},
]


def _utc() -> str:
    return now_utc().isoformat()


def _stage(status: str = STATUS_NOT_RUN, *, confidence: str = CONF_UNKNOWN, **extra: Any) -> dict[str, Any]:
    return {"status": status, "confidence": confidence, "updated_at": _utc(), **extra}


def empty_research_state() -> dict[str, Any]:
    return {
        "build": BUILD_TAG,
        "started_at": None,
        "completed_at": None,
        "running": False,
        "budget": dict(DEFAULT_BUDGETS),
        "budget_used": {k: 0 for k in DEFAULT_BUDGETS},
        "stages": {k: _stage() for k in STAGE_KEYS},
        "summary": {},
        "reject_filters": [],
        "next_action": "RESEARCH_OPPORTUNITY",
        "errors": [],
    }


def _budget_ok(state: dict[str, Any], key: str) -> bool:
    used = int((state.get("budget_used") or {}).get(key) or 0)
    limit = int((state.get("budget") or DEFAULT_BUDGETS).get(key) or 0)
    return used < limit


def _budget_inc(state: dict[str, Any], key: str, n: int = 1) -> None:
    bu = state.setdefault("budget_used", {})
    bu[key] = int(bu.get(key) or 0) + n


def _as_dec(v: Any) -> Decimal | None:
    return D(v)


def _days_until(deadline: Any) -> int | None:
    if deadline is None or deadline == "":
        return None
    try:
        from micro_purchase_lab_economics import _parse_date

        d = _parse_date(deadline)
        if d is None:
            return None
        return (d - datetime.now(timezone.utc).date()).days
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Product identity
# ---------------------------------------------------------------------------


def resolve_product_identity(test: dict[str, Any], opp: dict[str, Any] | None = None) -> dict[str, Any]:
    """Reuse M3 identity helpers; never silently pick among ambiguous candidates."""
    row = dict(opp or {})
    # Seed from lab test fields
    for k_src, k_dst in (
        ("manufacturer", "manufacturer"),
        ("part_number", "exact_part_number"),
        ("nsn", "exact_nsn"),
        ("product", "title"),
        ("quantity", "quantity"),
    ):
        if test.get(k_src) and not row.get(k_dst):
            row[k_dst] = test[k_src]
    if test.get("nsn") and not row.get("nsn"):
        row["nsn"] = test["nsn"]
    if test.get("part_number") and not row.get("part_number"):
        row["part_number"] = test["part_number"]

    product: dict[str, Any] = {}
    try:
        from m3_supplier_intelligence import build_product_identity

        product = build_product_identity(row)
    except Exception as exc:
        product = {"identity_status": "FAILED", "error": str(exc)}

    candidates: list[dict[str, Any]] = []
    try:
        from m3_product_identity_resolution import resolve_opportunity_product_identities

        res = resolve_opportunity_product_identities(row)
        if isinstance(res, dict):
            for c in res.get("candidates") or res.get("lines") or []:
                if isinstance(c, dict):
                    candidates.append(c)
            if not candidates and res.get("primary"):
                candidates.append(res["primary"] if isinstance(res["primary"], dict) else {"raw": res["primary"]})
    except Exception:
        pass

    mfr = test.get("manufacturer") or product.get("Manufacturer") or row.get("manufacturer")
    part = test.get("part_number") or product.get("Part_number") or row.get("exact_part_number") or row.get("part_number")
    nsn = test.get("nsn") or product.get("NSN") or row.get("exact_nsn") or row.get("nsn")
    if mfr in {None, "", "UNKNOWN"}:
        mfr = None
    if part in {None, "", "UNKNOWN"}:
        part = None
    if nsn in {None, "", "UNKNOWN"}:
        nsn = None

    # Ambiguity: multiple distinct part candidates without an exact NSN/part already on the test
    distinct_parts = {
        str(c.get("part_number") or c.get("Part_number") or c.get("mpn") or "").strip().upper()
        for c in candidates
        if (c.get("part_number") or c.get("Part_number") or c.get("mpn"))
    }
    distinct_parts.discard("")
    ambiguous = len(distinct_parts) > 1 and not (test.get("part_number") or test.get("nsn"))

    if ambiguous:
        confidence = IDENTITY_POSSIBLE
        status = STATUS_PARTIAL
        needs = "PRODUCT_IDENTITY_NEEDED"
    elif nsn and (mfr or part):
        confidence = IDENTITY_EXACT
        status = STATUS_COMPLETE
        needs = None
    elif nsn or (mfr and part):
        confidence = IDENTITY_STRONG
        status = STATUS_COMPLETE
        needs = None
    elif mfr or part or product.get("sufficient_for_pricing_research"):
        confidence = IDENTITY_POSSIBLE
        status = STATUS_PARTIAL
        needs = "PRODUCT_IDENTITY_NEEDED"
    else:
        confidence = IDENTITY_UNKNOWN
        status = STATUS_NOT_FOUND
        needs = "PRODUCT_IDENTITY_NEEDED"

    identity = {
        "manufacturer": mfr,
        "manufacturer_part_number": part,
        "nsn": nsn,
        "upc_gtin": product.get("UPC") if product.get("UPC") not in {None, "UNKNOWN"} else test.get("upc"),
        "product_description": test.get("product") or product.get("Technical_description") or row.get("title"),
        "government_item_description": row.get("title") or test.get("product"),
        "unit_of_issue": test.get("government_unit") or row.get("unit_of_issue") or "EA",
        "commercial_selling_unit": test.get("commercial_unit") or "EA",
        "pack_case_quantity": test.get("commercial_units_per_gov_unit") or 1,
        "condition_requirement": test.get("condition_requirement") or "NEW_OEM",
        "approved_source_restrictions": test.get("approved_source_restrictions") or row.get("approved_sources"),
        "alternate_approved_manufacturers": test.get("alternate_manufacturers") or [],
        "commercial_cots_status": test.get("cots_status") or "UNKNOWN",
        "confidence": confidence,
        "candidates": candidates[:8],
        "needs_operator_selection": needs == "PRODUCT_IDENTITY_NEEDED",
        "raw_product": product,
    }
    # Only EXACT/STRONG auto-enter pricing fields
    auto_ok = confidence in {IDENTITY_EXACT, IDENTITY_STRONG}
    return {
        "stage_status": status,
        "stage_confidence": CONF_STRONG if auto_ok else (CONF_MODERATE if confidence == IDENTITY_POSSIBLE else CONF_WEAK),
        "identity": identity,
        "auto_apply": auto_ok,
        "needs": needs,
    }


def attempt_unit_normalization(test: dict[str, Any], identity: dict[str, Any] | None = None) -> dict[str, Any]:
    """Reconcile gov unit vs commercial when evidence is clear."""
    gov = str(test.get("government_unit") or (identity or {}).get("unit_of_issue") or "EA").upper().strip()
    factor = _as_dec(test.get("commercial_units_per_gov_unit"))
    commercial = str(test.get("commercial_unit") or (identity or {}).get("commercial_selling_unit") or "EA").upper()

    # Clear EA↔EA
    if gov in {"", "EA", "EACH"} and (factor is None or factor == Decimal("1")):
        return {
            "ok": True,
            "government_unit": gov or "EA",
            "commercial_unit": commercial or "EA",
            "units_per_government_unit": "1",
            "source": "default_ea",
            "confidence": CONF_STRONG,
            "status": STATUS_COMPLETE,
            "needs": None,
        }

    # Explicit CASE with known pack
    if "CASE" in gov or "CS" == gov:
        if factor is not None and factor > 0:
            return {
                "ok": True,
                "government_unit": gov,
                "commercial_unit": commercial,
                "units_per_government_unit": str(factor),
                "source": "operator_or_identity_pack",
                "confidence": CONF_MODERATE,
                "status": STATUS_COMPLETE,
                "needs": None,
            }
        return {
            "ok": False,
            "government_unit": gov,
            "commercial_unit": commercial,
            "units_per_government_unit": None,
            "source": None,
            "confidence": CONF_UNKNOWN,
            "status": STATUS_PARTIAL,
            "needs": "UNIT_NORMALIZATION_REQUIRED",
        }

    if factor is not None and factor > 0:
        return {
            "ok": True,
            "government_unit": gov,
            "commercial_unit": commercial,
            "units_per_government_unit": str(factor),
            "source": "test_factor",
            "confidence": CONF_MODERATE,
            "status": STATUS_COMPLETE,
            "needs": None,
        }

    return {
        "ok": False,
        "government_unit": gov,
        "commercial_unit": commercial,
        "units_per_government_unit": None,
        "source": None,
        "confidence": CONF_UNKNOWN,
        "status": STATUS_PARTIAL,
        "needs": "UNIT_NORMALIZATION_REQUIRED",
    }


# ---------------------------------------------------------------------------
# Government history
# ---------------------------------------------------------------------------


def collect_government_awards(test: dict[str, Any], opp: dict[str, Any] | None = None) -> dict[str, Any]:
    awards: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(a: dict[str, Any], match_tier: str) -> None:
        key = "|".join(
            str(x or "")
            for x in (a.get("award_date"), a.get("unit_price"), a.get("awarded_vendor"), a.get("contract_number"))
        )
        if key in seen:
            return
        seen.add(key)
        row = dict(a)
        row["match_tier"] = match_tier
        awards.append(row)

    # Existing lab awards first
    for a in test.get("historical_awards") or []:
        if isinstance(a, dict):
            _add(a, "lab_existing")

    if opp:
        from micro_purchase_lab_service import _awards_from_row

        for a in _awards_from_row(opp):
            tier = "exact_product"
            if test.get("nsn") and (opp.get("exact_nsn") or opp.get("nsn")) == test.get("nsn"):
                tier = "exact_nsn"
            if test.get("part_number") and (
                opp.get("exact_part_number") or opp.get("part_number")
            ) == test.get("part_number"):
                tier = "exact_part" if tier != "exact_nsn" else "exact_nsn_and_part"
            _add(a, tier)

        for a in opp.get("historical_awards") or []:
            if isinstance(a, dict):
                _add(
                    {
                        "award_date": a.get("date") or a.get("award_date"),
                        "awarded_vendor": a.get("winner") or a.get("vendor") or a.get("awardee"),
                        "unit_price": a.get("unit_price") or a.get("price"),
                        "total_award": a.get("amount") or a.get("total"),
                        "quantity": a.get("quantity"),
                        "unit_of_issue": a.get("unit") or a.get("unit_of_issue"),
                        "contract_number": a.get("contract_number") or a.get("award_id"),
                        "source_url": a.get("url") or a.get("source_url"),
                        "source_type": a.get("source") or "HISTORICAL_AWARD",
                        "confidence": a.get("confidence") or CONF_MODERATE,
                    },
                    "exact_product",
                )

    # Prefer exact tiers first
    tier_rank = {
        "exact_nsn_and_part": 0,
        "exact_nsn": 1,
        "exact_part": 2,
        "exact_product": 3,
        "lab_existing": 4,
        "comparable": 9,
    }
    awards.sort(key=lambda a: (tier_rank.get(str(a.get("match_tier")), 8), str(a.get("award_date") or "")))

    prices = [_as_dec(a.get("unit_price")) for a in awards]
    prices = [p for p in prices if p is not None]
    stats = {}
    if prices:
        sorted_p = sorted(prices)
        stats = {
            "latest_price": str(prices[0]) if awards else None,  # after sort by date — recompute
            "median": str(sorted_p[len(sorted_p) // 2]),
            "min": str(min(prices)),
            "max": str(max(prices)),
            "observations": len(prices),
        }
        # latest by date
        dated = [(a.get("award_date") or "", _as_dec(a.get("unit_price"))) for a in awards]
        dated = [(d, p) for d, p in dated if p is not None]
        dated.sort(key=lambda x: x[0], reverse=True)
        if dated:
            stats["latest_price"] = str(dated[0][1])

    if not awards:
        return {
            "stage_status": STATUS_NOT_FOUND,
            "stage_confidence": CONF_UNKNOWN,
            "awards": [],
            "stats": {},
            "needs": "HISTORICAL_DATA_NEEDED",
        }
    return {
        "stage_status": STATUS_COMPLETE if len(awards) >= 1 else STATUS_PARTIAL,
        "stage_confidence": CONF_STRONG if len(awards) >= 2 else CONF_MODERATE,
        "awards": awards,
        "stats": stats,
        "needs": None,
    }


# ---------------------------------------------------------------------------
# Historical / current market evidence
# ---------------------------------------------------------------------------


def _normalize_market_obs(raw: dict[str, Any], *, kind: str) -> dict[str, Any]:
    price = raw.get("unit_price") or raw.get("price") or raw.get("amount") or raw.get("normalized_unit_price")
    cond = str(raw.get("condition") or raw.get("product_condition") or "UNKNOWN").upper()
    if cond in {"NEW", "OEM", "NEW/OEM"}:
        cond = "NEW_OEM"
    match = str(raw.get("exact_match") or raw.get("product_match") or raw.get("match_confidence") or MATCH_POSSIBLE).upper()
    if match in {"HIGH", "EXACT_MATCH"}:
        match = MATCH_EXACT
    elif match in {"MEDIUM", "STRONG_MATCH"}:
        match = MATCH_STRONG
    elif match in {"LOW"}:
        match = MATCH_POSSIBLE
    et = str(raw.get("evidence_type") or raw.get("source_type") or "OTHER").upper()
    return {
        "evidence_date": raw.get("evidence_date") or raw.get("date") or raw.get("as_of") or raw.get("Date"),
        "seller": raw.get("seller") or raw.get("vendor") or raw.get("source_name") or raw.get("Source"),
        "source": raw.get("source") or raw.get("source_name") or raw.get("Source"),
        "manufacturer": raw.get("manufacturer"),
        "part_number": raw.get("part_number"),
        "price": price,
        "unit_price": price,
        "quantity": raw.get("quantity") or 1,
        "unit": raw.get("unit") or "EA",
        "normalized_unit_price": raw.get("normalized_unit_price") or price,
        "product_condition": cond,
        "exact_match": match,
        "source_url": raw.get("source_url") or raw.get("url") or raw.get("source_url"),
        "url": raw.get("url") or raw.get("source_url"),
        "source_title": raw.get("source_title") or raw.get("title"),
        "evidence_type": et,
        "confidence": str(raw.get("confidence") or raw.get("Product_match_confidence") or CONF_MODERATE).upper(),
        "date_distance_from_award": raw.get("date_distance_from_award"),
        "notes": raw.get("notes"),
        "kind": kind,
        "selected": bool(raw.get("selected")),
        "auto_eligible": match in {MATCH_EXACT, MATCH_STRONG} and cond not in CONDITION_BAD,
    }


def collect_historical_market(
    test: dict[str, Any],
    awards: list[dict[str, Any]],
    opp: dict[str, Any] | None = None,
    *,
    window_days: int = HISTORICAL_MARKET_WINDOW_DAYS,
) -> dict[str, Any]:
    obs: list[dict[str, Any]] = []
    for m in test.get("historical_market_costs") or []:
        if isinstance(m, dict):
            o = _normalize_market_obs(m, kind="historical")
            # attach date distance to nearest award
            best_delta = None
            for a in awards:
                win = historical_market_in_window(a.get("award_date"), o.get("evidence_date"), window_days=window_days)
                if win.get("days_delta") is not None:
                    if best_delta is None or win["days_delta"] < best_delta:
                        best_delta = win["days_delta"]
                        o["date_distance_from_award"] = best_delta
                        o["in_window"] = win.get("in_window")
            obs.append(o)

    if opp:
        for key in ("historical_market_costs", "historical_commercial_prices", "archived_price_evidence"):
            for m in opp.get(key) or []:
                if isinstance(m, dict):
                    obs.append(_normalize_market_obs(m, kind="historical"))

    recommended = select_historical_market_reference(obs, awards, window_days=window_days)
    if not obs:
        return {
            "stage_status": STATUS_NOT_FOUND,
            "stage_confidence": CONF_UNKNOWN,
            "observations": [],
            "recommended": None,
            "needs": "HISTORICAL_COST_NEEDED",
            "note": "No historical commercial price evidence found; operator may add manually or accept research gap",
        }
    return {
        "stage_status": STATUS_COMPLETE if recommended else STATUS_PARTIAL,
        "stage_confidence": (recommended or {}).get("confidence") or CONF_MODERATE,
        "observations": obs,
        "recommended": recommended,
        "needs": None if recommended else "REVIEW_HISTORICAL_EVIDENCE",
    }


def select_historical_market_reference(
    observations: list[dict[str, Any]],
    awards: list[dict[str, Any]],
    *,
    window_days: int = HISTORICAL_MARKET_WINDOW_DAYS,
) -> dict[str, Any] | None:
    """Deterministic selection — exact → new/OEM → closest date → source quality → pack → lowest ambiguity."""
    eligible = [
        o
        for o in observations
        if o.get("auto_eligible")
        and _as_dec(o.get("unit_price") or o.get("price")) is not None
        and str(o.get("product_condition") or "").upper() not in CONDITION_BAD
    ]
    if not eligible:
        # keep visible but do not auto-select weaker
        return None

    award_dates = [a.get("award_date") for a in awards if a.get("award_date")]

    def score(o: dict[str, Any]) -> tuple:
        match_r = {MATCH_EXACT: 0, MATCH_STRONG: 1, MATCH_POSSIBLE: 2, MATCH_NOT_EXACT: 3}.get(
            str(o.get("exact_match")), 4
        )
        cond_r = 0 if str(o.get("product_condition") or "").upper() in CONDITION_OK else 1
        delta = o.get("date_distance_from_award")
        if delta is None and award_dates:
            deltas = []
            for ad in award_dates:
                win = historical_market_in_window(ad, o.get("evidence_date"), window_days=window_days)
                if win.get("days_delta") is not None:
                    deltas.append(win["days_delta"])
            delta = min(deltas) if deltas else 9999
        delta_i = int(delta) if delta is not None else 9999
        et_r = -EVIDENCE_TYPE_RANK.get(str(o.get("evidence_type") or "OTHER").upper(), 0)
        return (match_r, cond_r, delta_i, et_r, str(o.get("seller") or ""))

    best = sorted(eligible, key=score)[0]
    out = dict(best)
    out["selected"] = True
    out["selection_reason"] = (
        f"exact={out.get('exact_match')}; condition={out.get('product_condition')}; "
        f"date_delta={out.get('date_distance_from_award')}; type={out.get('evidence_type')}"
    )
    return out


def collect_current_market(
    test: dict[str, Any],
    opp: dict[str, Any] | None = None,
    *,
    allow_paid_research: bool = False,
    budget_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    obs: list[dict[str, Any]] = []
    for m in test.get("current_market_prices") or []:
        if isinstance(m, dict):
            obs.append(_normalize_market_obs(m, kind="current"))

    if opp:
        try:
            from m3_supplier_intelligence import build_product_identity, collect_existing_price_evidence

            product = build_product_identity(opp)
            for e in collect_existing_price_evidence(opp, product):
                obs.append(
                    _normalize_market_obs(
                        {
                            "unit_price": e.get("amount"),
                            "seller": e.get("Source"),
                            "evidence_date": e.get("Date"),
                            "confidence": e.get("Product_match_confidence"),
                            "evidence_type": "RETAIL",
                            "condition": "NEW_OEM",
                            "exact_match": MATCH_STRONG
                            if e.get("Product_match_confidence") in {"HIGH", "STRONG"}
                            else MATCH_POSSIBLE,
                        },
                        kind="current",
                    )
                )
        except Exception:
            pass

        # Optional gated web research (budgeted)
        if allow_paid_research and budget_state and _budget_ok(budget_state, "max_ai_calls"):
            try:
                from m3_supplier_intelligence import build_product_identity, research_public_pricing_web

                product = build_product_identity(opp)
                web = research_public_pricing_web(opp, product, allow_paid=True)
                _budget_inc(budget_state, "max_ai_calls")
                _budget_inc(budget_state, "max_source_queries")
                for e in web.get("evidence") or []:
                    if isinstance(e, dict):
                        obs.append(
                            _normalize_market_obs(
                                {
                                    "unit_price": e.get("amount") or e.get("unit_price"),
                                    "seller": e.get("Source") or e.get("source_name"),
                                    "url": e.get("source_url") or e.get("url"),
                                    "evidence_date": e.get("Date") or e.get("as_of"),
                                    "confidence": e.get("Product_match_confidence") or e.get("match_confidence"),
                                    "evidence_type": "RETAIL",
                                    "condition": "NEW_OEM",
                                    "exact_match": MATCH_STRONG
                                    if str(e.get("match_confidence") or "").upper() in {"HIGH", "STRONG"}
                                    else MATCH_POSSIBLE,
                                },
                                kind="current",
                            )
                        )
            except Exception as exc:
                if budget_state is not None:
                    budget_state.setdefault("errors", []).append(f"current_market_web:{exc}")

    verified = [
        o
        for o in obs
        if o.get("auto_eligible") and _as_dec(o.get("unit_price") or o.get("price")) is not None
    ]
    recommended = select_current_market_reference(verified or obs)
    summary = current_market_summary(verified)
    if not obs:
        return {
            "stage_status": STATUS_NOT_FOUND,
            "stage_confidence": CONF_UNKNOWN,
            "observations": [],
            "recommended": None,
            "summary": summary,
            "needs": "CURRENT_MARKET_DATA_NEEDED",
            "label": "CURRENT_PUBLIC_MARKET_PRICE",
        }
    return {
        "stage_status": STATUS_COMPLETE if recommended and verified else STATUS_PARTIAL,
        "stage_confidence": CONF_STRONG if len(verified) >= 3 else CONF_MODERATE,
        "observations": obs,
        "recommended": recommended,
        "summary": summary,
        "needs": None if recommended else "REVIEW_CURRENT_MARKET",
        "label": "CURRENT_PUBLIC_MARKET_PRICE",
        "note": "CURRENT PUBLIC MARKET PRICE is not OUR SUPPLIER ACQUISITION PRICE",
    }


def select_current_market_reference(observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [
        o
        for o in observations
        if _as_dec(o.get("unit_price") or o.get("price")) is not None
        and str(o.get("exact_match")) in {MATCH_EXACT, MATCH_STRONG}
        and str(o.get("product_condition") or "").upper() not in CONDITION_BAD
    ]
    pool = eligible or [
        o
        for o in observations
        if _as_dec(o.get("unit_price") or o.get("price")) is not None
        and str(o.get("product_condition") or "").upper() not in CONDITION_BAD
    ]
    if not pool:
        return None

    def score(o: dict[str, Any]) -> tuple:
        match_r = {MATCH_EXACT: 0, MATCH_STRONG: 1}.get(str(o.get("exact_match")), 2)
        et_r = -EVIDENCE_TYPE_RANK.get(str(o.get("evidence_type") or "OTHER").upper(), 0)
        price = _as_dec(o.get("unit_price") or o.get("price")) or Decimal("999999")
        return (match_r, et_r, price)

    best = sorted(pool, key=score)[0]
    out = dict(best)
    out["selected"] = True
    return out


def current_market_summary(observations: list[dict[str, Any]]) -> dict[str, Any]:
    prices = [_as_dec(o.get("unit_price") or o.get("price")) for o in observations]
    prices = [p for p in prices if p is not None]
    if not prices:
        return {
            "lowest_verified": None,
            "median": None,
            "selected": None,
            "verified_observations": 0,
        }
    sorted_p = sorted(prices)
    return {
        "lowest_verified": str(min(prices)),
        "median": str(sorted_p[len(sorted_p) // 2]),
        "selected": None,
        "verified_observations": len(prices),
    }


# ---------------------------------------------------------------------------
# Suppliers + quote packets
# ---------------------------------------------------------------------------


def discover_supplier_channels(
    test: dict[str, Any],
    identity: dict[str, Any] | None = None,
    opp: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    mfr = (identity or {}).get("manufacturer") or test.get("manufacturer")
    title = str(test.get("product") or (opp or {}).get("title") or "").lower()
    nsn = (identity or {}).get("nsn") or test.get("nsn")

    # OEM self
    if mfr:
        candidates.append(
            {
                "supplier": mfr,
                "website": None,
                "supplier_type": "OEM",
                "known_authorization_status": "UNKNOWN",
                "product_availability_evidence": None,
                "reseller_program_evidence": None,
                "government_special_bid_possible": True,
                "direct_ship_capability": "UNKNOWN",
                "quote_required": True,
                "account_required": True,
                "confidence": CONF_MODERATE,
                "reason_codes": ["oem_manufacturer"],
            }
        )

    # Map from existing supplier intelligence
    if opp:
        try:
            from m3_supplier_intelligence import build_product_identity, map_supply_chain

            product = build_product_identity(opp)
            supply = map_supply_chain(opp, product)
            for c in supply.get("all_channels") or []:
                candidates.append(
                    {
                        "supplier": c.get("company"),
                        "website": c.get("website"),
                        "supplier_type": c.get("role"),
                        "known_authorization_status": c.get("confidence"),
                        "product_availability_evidence": c.get("product_match"),
                        "reseller_program_evidence": None,
                        "government_special_bid_possible": c.get("role") in {"AUTHORIZED_DISTRIBUTOR", "OEM"},
                        "direct_ship_capability": "UNKNOWN",
                        "quote_required": True,
                        "account_required": True,
                        "confidence": CONF_MODERATE if c.get("confidence") in {"HIGH", "MEDIUM"} else CONF_WEAK,
                        "reason_codes": ["supply_chain_map", str(c.get("evidence_source") or "")],
                    }
                )
        except Exception:
            pass

    # Seed industrial / IT distributors
    industrial = any(x in title for x in ("valve", "bearing", "bushing", "filter", "hose", "fastener", "kit", "pump", "nsn")) or bool(nsn)
    it_ish = any(x in title for x in ("laptop", "server", "switch", "printer", "monitor", "dell", "lenovo", "cisco"))
    seeds = []
    if industrial or nsn:
        seeds = [s for s in CHANNEL_SEEDS if s["supplier"] in {"MSC Industrial", "MSC ResaleLink", "Grainger", "Zoro", "Fastenal"}]
    if it_ish or (mfr and str(mfr).lower() in {"dell", "lenovo", "hp", "cisco", "brother", "epson"}):
        seeds = seeds + [s for s in CHANNEL_SEEDS if s["supplier"] in {"Ingram Micro", "TD SYNNEX", "D&H"}]
    if not seeds:
        seeds = [s for s in CHANNEL_SEEDS if s["supplier"] in {"MSC Industrial", "Grainger", "Zoro"}]

    for s in seeds:
        candidates.append(
            {
                **s,
                "known_authorization_status": "UNKNOWN",
                "product_availability_evidence": None,
                "reseller_program_evidence": "program_may_exist",
                "government_special_bid_possible": True,
                "direct_ship_capability": "UNKNOWN",
                "confidence": CONF_MODERATE,
                "reason_codes": ["channel_seed"],
            }
        )

    # Dedupe by supplier name
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for c in candidates:
        name = str(c.get("supplier") or "").strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        uniq.append(c)

    ranked = rank_quote_targets(uniq, test=test)
    return {
        "stage_status": STATUS_COMPLETE if ranked else STATUS_NOT_FOUND,
        "stage_confidence": CONF_MODERATE if ranked else CONF_UNKNOWN,
        "candidates": ranked,
        "recommended": ranked[:4],
        "needs": None if ranked else "SUPPLIER_CHANNEL_NEEDED",
    }


def rank_quote_targets(candidates: list[dict[str, Any]], *, test: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Deterministic ranking — not by retail list price alone."""
    hist_quotes = []
    try:
        from micro_purchase_lab_quote_intel import prior_quote_score_for_supplier

        use_hist = True
    except Exception:
        use_hist = False

    def score(c: dict[str, Any]) -> tuple:
        st = str(c.get("supplier_type") or "").upper()
        auth = 0
        if st in {"OEM", "MANUFACTURER"}:
            auth = 0
        elif "AUTHORIZED" in st:
            auth = 1
        elif "WHOLESALE" in st:
            auth = 2
        elif "DISTRIBUTOR" in st:
            auth = 3
        else:
            auth = 5
        exact = 0 if c.get("product_availability_evidence") else 1
        wholesale = 0 if ("WHOLESALE" in st or "RESELLER" in st or st == "OEM") else 1
        special = 0 if c.get("government_special_bid_possible") else 1
        stock = 0 if c.get("in_stock") else 1
        ship = 0 if str(c.get("direct_ship_capability") or "").upper() in {"YES", "TRUE", "AVAILABLE"} else 1
        gov = 0 if "GOVERNMENT" in st else 1
        prior = 5
        if use_hist:
            prior = -prior_quote_score_for_supplier(str(c.get("supplier") or ""))
        reasons = list(c.get("reason_codes") or [])
        if auth <= 1:
            reasons.append("authorized_or_oem")
        if wholesale == 0:
            reasons.append("wholesale_reseller_program")
        if special == 0:
            reasons.append("special_bid_possible")
        c["reason_codes"] = list(dict.fromkeys(reasons))
        return (auth, exact, wholesale, special, stock, ship, gov, prior, str(c.get("supplier") or ""))

    ranked = sorted(candidates, key=score)
    for i, c in enumerate(ranked, 1):
        c["rank"] = i
    return ranked


def build_quote_packet(test: dict[str, Any], supplier: dict[str, Any]) -> dict[str, Any]:
    factor = _as_dec(test.get("commercial_units_per_gov_unit")) or Decimal("1")
    qty = _as_dec(test.get("quantity")) or Decimal("1")
    norm_qty = qty * factor
    text = (
        "We are evaluating a government procurement requirement for:\n\n"
        f"Manufacturer: {test.get('manufacturer') or '[manufacturer]'}\n"
        f"Part Number: {test.get('part_number') or '[part]'}\n"
        f"NSN: {test.get('nsn') or 'N/A'}\n"
        f"Quantity: {test.get('quantity') or '[quantity]'} {test.get('government_unit') or 'EA'}\n"
        f"Normalized commercial quantity: {norm_qty} {test.get('commercial_unit') or 'EA'}\n"
        f"Delivery Destination: {test.get('delivery_location') or '[location]'}\n"
        f"Required Delivery Date: {test.get('deadline') or '[date]'}\n"
        f"Solicitation: {test.get('solicitation') or 'N/A'}\n"
        f"Government Agency: {test.get('agency') or 'N/A'}\n"
        f"Bid Deadline: {test.get('deadline') or 'N/A'}\n"
        f"Condition: {test.get('condition_requirement') or 'NEW_OEM'}\n"
        f"Approved-source / packaging notes: {test.get('approved_source_restrictions') or 'N/A'}\n\n"
        "Please provide your best reseller/government-project pricing including:\n"
        "- unit price;\n"
        "- extended price;\n"
        "- freight;\n"
        "- stock status;\n"
        "- lead time;\n"
        "- quote expiration;\n"
        "- payment terms;\n"
        "- MOQ;\n"
        "- direct-ship availability;\n"
        "- blind-drop-ship availability;\n"
        "- whether special government/project pricing is available;\n"
        "- whether deal registration or special-bid pricing applies.\n\n"
        "Note: This inquiry does not claim authorized reseller status, government contract status, "
        "payment terms, existing supplier account, or past performance."
    )
    return {
        "id": f"QPKT-{uuid4().hex[:10]}",
        "supplier": supplier.get("supplier"),
        "website": supplier.get("website"),
        "manufacturer": test.get("manufacturer"),
        "part_number": test.get("part_number"),
        "nsn": test.get("nsn"),
        "quantity": test.get("quantity"),
        "government_unit": test.get("government_unit"),
        "normalized_commercial_quantity": str(norm_qty),
        "delivery_location": test.get("delivery_location"),
        "requested_delivery_date": test.get("deadline"),
        "solicitation": test.get("solicitation"),
        "agency": test.get("agency"),
        "bid_deadline": test.get("deadline"),
        "product_requirement": test.get("product"),
        "condition_requirement": test.get("condition_requirement") or "NEW_OEM",
        "approved_source_requirement": test.get("approved_source_restrictions"),
        "packaging_requirements": test.get("packaging_requirements"),
        "documentation_requirements": test.get("documentation_requirements"),
        "quote_request_text": text,
        "reason_codes": supplier.get("reason_codes") or [],
        "created_at": _utc(),
    }


# ---------------------------------------------------------------------------
# Pricing model + reject filters + next action
# ---------------------------------------------------------------------------


def build_pricing_model(
    test: dict[str, Any],
    awards: list[dict[str, Any]],
    hist_rec: dict[str, Any] | None,
    current_rec: dict[str, Any] | None,
) -> dict[str, Any]:
    last_gov = None
    if awards:
        dated = sorted(awards, key=lambda a: str(a.get("award_date") or ""), reverse=True)
        last_gov = _as_dec(dated[0].get("unit_price"))
    hist_m = _as_dec((hist_rec or {}).get("unit_price") or (hist_rec or {}).get("price"))
    curr_m = _as_dec((current_rec or {}).get("unit_price") or (current_rec or {}).get("price"))
    equiv = historical_equivalent_bid(last_gov, curr_m, hist_m) if last_gov and curr_m and hist_m else None

    return {
        "stage_status": STATUS_COMPLETE if equiv is not None else STATUS_PARTIAL,
        "stage_confidence": CONF_MODERATE if equiv is not None else CONF_WEAK,
        "last_government_price": str(last_gov) if last_gov is not None else None,
        "historical_market_reference": hist_rec,
        "current_market_reference": current_rec,
        "historical_equivalent_bid": str(equiv) if equiv is not None else None,
        "label": "HISTORICAL-EQUIVALENT BID ESTIMATE",
        "note": "Not expected award, winning bid, or guaranteed bid",
        "data_types": {
            "historical_government_price": str(last_gov) if last_gov is not None else None,
            "historical_market_price": str(hist_m) if hist_m is not None else None,
            "current_public_market_price": str(curr_m) if curr_m is not None else None,
            "historical_equivalent_bid": str(equiv) if equiv is not None else None,
            "actual_supplier_quote": None,
        },
    }


def evaluate_reject_filters(
    test: dict[str, Any],
    *,
    identity: dict[str, Any] | None,
    units: dict[str, Any] | None,
    awards: list[dict[str, Any]],
    current_summary: dict[str, Any] | None,
    suppliers: list[dict[str, Any]],
    equiv: Any,
) -> list[dict[str, Any]]:
    """Return reject decisions. Caution: high public price + distributor channel → quote needed, not reject."""
    filters: list[dict[str, Any]] = []
    status = str(test.get("opportunity_status") or "OPEN").upper()
    if status in {"EXPIRED", "CANCELLED", "CLOSED", "AWARDED"}:
        filters.append({"code": "EXPIRED_OPPORTUNITY", "action": "REJECT", "detail": status})

    runway = _days_until(test.get("deadline"))
    if runway is not None and runway < MIN_DEADLINE_RUNWAY_DAYS:
        filters.append(
            {
                "code": "INSUFFICIENT_DEADLINE_RUNWAY",
                "action": "REJECT",
                "detail": f"runway_days={runway}",
            }
        )

    conf = str((identity or {}).get("confidence") or IDENTITY_UNKNOWN)
    if conf in {IDENTITY_UNKNOWN} and not (test.get("part_number") or test.get("nsn")):
        filters.append({"code": "NO_VALID_PRODUCT_IDENTITY", "action": "REJECT", "detail": conf})

    if units and units.get("needs") == "UNIT_NORMALIZATION_REQUIRED":
        filters.append({"code": "UNRESOLVED_UNIT_ISSUE", "action": "HOLD", "detail": "UNIT_NORMALIZATION_REQUIRED"})

    if not awards:
        filters.append({"code": "NO_GOVERNMENT_HISTORY", "action": "HOLD", "detail": "HISTORICAL_DATA_NEEDED"})

    curr = _as_dec((current_summary or {}).get("lowest_verified") or (current_summary or {}).get("median"))
    he = _as_dec(equiv)
    has_dist = any(
        "DISTRIBUTOR" in str(s.get("supplier_type") or "").upper()
        or "WHOLESALE" in str(s.get("supplier_type") or "").upper()
        or str(s.get("supplier_type") or "").upper() == "OEM"
        for s in suppliers
    )
    if curr is not None and he is not None and curr > he * Decimal("1.5"):
        if has_dist:
            filters.append(
                {
                    "code": "PUBLIC_PRICE_HIGH_DISTRIBUTOR_PRESENT",
                    "action": "SUPPLIER_QUOTE_NEEDED",
                    "detail": "public_new_above_HE_but_wholesale_channels_exist",
                }
            )
        else:
            filters.append(
                {
                    "code": "OBVIOUS_ECONOMIC_IMPOSSIBILITY",
                    "action": "REJECT",
                    "detail": f"public={curr} he={he}",
                }
            )

    return filters


def derive_next_action(test: dict[str, Any], research: dict[str, Any]) -> str:
    status = str(test.get("status") or "")
    if status == "BID_CANDIDATE":
        return "BID_CANDIDATE"
    rejects = [f for f in (research.get("reject_filters") or []) if f.get("action") == "REJECT"]
    if rejects:
        return "REJECT"
    stages = research.get("stages") or {}
    ident = stages.get("product_identity") or {}
    if ident.get("status") in {STATUS_NOT_FOUND, STATUS_FAILED} or (ident.get("needs") == "PRODUCT_IDENTITY_NEEDED"):
        return "REVIEW_PRODUCT_IDENTITY"
    if (stages.get("opportunity") or {}).get("unit_needs") == "UNIT_NORMALIZATION_REQUIRED":
        return "RESOLVE_UNITS"
    if (stages.get("government_history") or {}).get("status") == STATUS_NOT_FOUND:
        return "REVIEW_HISTORICAL_EVIDENCE"
    if (stages.get("historical_market") or {}).get("status") in {STATUS_NOT_FOUND, STATUS_PARTIAL}:
        if not (test.get("historical_market_costs") or []):
            return "REVIEW_HISTORICAL_EVIDENCE"
    if (stages.get("current_market") or {}).get("status") == STATUS_NOT_FOUND:
        return "REVIEW_CURRENT_MARKET"
    quotes = [q for q in (test.get("supplier_quotes") or []) if not q.get("archived")]
    if not quotes:
        packets = test.get("quote_packets") or []
        qitems = test.get("quote_queue_items") or []
        if not packets and not qitems:
            return "PREPARE_SUPPLIER_QUOTES"
        pending = [q for q in qitems if str(q.get("quote_status")) in {QUOTE_STATUS_READY, QUOTE_STATUS_NOT_PREPARED}]
        if pending or packets:
            return "REQUEST_SUPPLIER_QUOTE"
        return "ENTER_SUPPLIER_QUOTE"
    if status in {"ECONOMIC_PASS", "ECONOMIC_FAIL", "EXECUTION_FAIL"}:
        return "REVIEW_ECONOMICS"
    if status == "SUPPLIER_QUOTE_NEEDED":
        return "ENTER_SUPPLIER_QUOTE"
    return "REVIEW_ECONOMICS"


# ---------------------------------------------------------------------------
# Main research orchestration
# ---------------------------------------------------------------------------


def load_opportunity_row(canonical_id: str | None) -> dict[str, Any] | None:
    if not canonical_id:
        return None
    try:
        from m3_discovery_service import restore_pipeline_store_from_db
        from m3_pipeline_store import M3PipelineStore

        store = M3PipelineStore()
        restore_pipeline_store_from_db(store)
        return store.get(canonical_id)
    except Exception:
        return None


def research_opportunity(
    test: dict[str, Any],
    *,
    allow_paid_research: bool = False,
    accept_auto_evidence: bool = True,
) -> dict[str, Any]:
    """Run all research stages for one lab test. Mutates a copy; caller persists."""
    row = deepcopy(test)
    state = empty_research_state()
    state["started_at"] = _utc()
    state["running"] = True
    opp = load_opportunity_row(row.get("linked_opportunity_id"))

    # 1 Opportunity
    try:
        runway = _days_until(row.get("deadline"))
        state["stages"]["opportunity"] = _stage(
            STATUS_COMPLETE if row.get("solicitation") or row.get("product") else STATUS_PARTIAL,
            confidence=CONF_STRONG if row.get("solicitation") else CONF_MODERATE,
            solicitation=row.get("solicitation"),
            agency=row.get("agency"),
            deadline=row.get("deadline"),
            deadline_runway_days=runway,
            linked_opportunity_id=row.get("linked_opportunity_id"),
        )
    except Exception as exc:
        state["stages"]["opportunity"] = _stage(STATUS_FAILED, confidence=CONF_UNKNOWN, error=str(exc))
        state["errors"].append(f"opportunity:{exc}")

    # 2 Product identity
    ident_out = resolve_product_identity(row, opp)
    state["stages"]["product_identity"] = _stage(
        ident_out["stage_status"],
        confidence=ident_out["stage_confidence"],
        identity=ident_out["identity"],
        needs=ident_out.get("needs"),
        candidates=(ident_out.get("identity") or {}).get("candidates") or [],
    )
    if ident_out.get("auto_apply"):
        ident = ident_out["identity"]
        if ident.get("manufacturer"):
            row["manufacturer"] = ident["manufacturer"]
        if ident.get("manufacturer_part_number"):
            row["part_number"] = ident["manufacturer_part_number"]
        if ident.get("nsn"):
            row["nsn"] = ident["nsn"]
        if ident.get("product_description") and not row.get("product"):
            row["product"] = ident["product_description"]
        row["identity_confidence"] = ident.get("confidence")
    else:
        row["identity_confidence"] = (ident_out.get("identity") or {}).get("confidence") or IDENTITY_UNKNOWN
        row["identity_candidates"] = (ident_out.get("identity") or {}).get("candidates") or []

    # Units (attached to opportunity stage extras + dedicated field)
    units = attempt_unit_normalization(row, ident_out.get("identity"))
    row["unit_normalization"] = units
    if units.get("ok") and units.get("units_per_government_unit") is not None:
        row["commercial_units_per_gov_unit"] = units["units_per_government_unit"]
        row["government_unit"] = units.get("government_unit") or row.get("government_unit")
    state["stages"]["opportunity"]["unit_normalization"] = units
    state["stages"]["opportunity"]["unit_needs"] = units.get("needs")

    # 3 Government history
    gov = collect_government_awards(row, opp)
    state["stages"]["government_history"] = _stage(
        gov["stage_status"],
        confidence=gov["stage_confidence"],
        stats=gov.get("stats"),
        award_count=len(gov.get("awards") or []),
        needs=gov.get("needs"),
    )
    if accept_auto_evidence and gov.get("awards"):
        # merge, prefer existing operator selections
        existing = row.get("historical_awards") or []
        if not existing:
            row["historical_awards"] = gov["awards"]
        else:
            # append new only
            have = {
                "|".join(str(x or "") for x in (a.get("award_date"), a.get("unit_price"), a.get("awarded_vendor")))
                for a in existing
            }
            merged = list(existing)
            for a in gov["awards"]:
                key = "|".join(str(x or "") for x in (a.get("award_date"), a.get("unit_price"), a.get("awarded_vendor")))
                if key not in have:
                    merged.append(a)
            row["historical_awards"] = merged

    # 4 Historical market
    hist = collect_historical_market(row, row.get("historical_awards") or gov.get("awards") or [], opp)
    state["stages"]["historical_market"] = _stage(
        hist["stage_status"],
        confidence=hist["stage_confidence"],
        observation_count=len(hist.get("observations") or []),
        recommended=hist.get("recommended"),
        needs=hist.get("needs"),
        note=hist.get("note"),
    )
    if accept_auto_evidence and hist.get("recommended") and not (row.get("historical_market_costs") or []):
        rec = dict(hist["recommended"])
        rec["operator_accepted"] = False
        rec["auto_suggested"] = True
        row["historical_market_costs"] = [rec]
        row["recommended_historical_market"] = rec
    elif hist.get("observations"):
        row["historical_market_candidates"] = hist["observations"]
        row["recommended_historical_market"] = hist.get("recommended")

    # 5 Current market
    curr = collect_current_market(
        row, opp, allow_paid_research=allow_paid_research, budget_state=state
    )
    if curr.get("recommended") and curr.get("summary") is not None:
        curr["summary"]["selected"] = (curr["recommended"] or {}).get("unit_price") or (curr["recommended"] or {}).get(
            "price"
        )
    state["stages"]["current_market"] = _stage(
        curr["stage_status"],
        confidence=curr["stage_confidence"],
        observation_count=len(curr.get("observations") or []),
        recommended=curr.get("recommended"),
        summary=curr.get("summary"),
        needs=curr.get("needs"),
        label=curr.get("label"),
        note=curr.get("note"),
    )
    if accept_auto_evidence and curr.get("recommended") and not (row.get("current_market_prices") or []):
        rec = dict(curr["recommended"])
        rec["auto_suggested"] = True
        row["current_market_prices"] = [rec]
        row["recommended_current_market"] = rec
    elif curr.get("observations"):
        row["current_market_candidates"] = curr["observations"]
        row["recommended_current_market"] = curr.get("recommended")

    # 6 Suppliers
    sup = discover_supplier_channels(row, ident_out.get("identity"), opp)
    state["stages"]["supplier_channels"] = _stage(
        sup["stage_status"],
        confidence=sup["stage_confidence"],
        candidate_count=len(sup.get("candidates") or []),
        recommended=sup.get("recommended"),
        needs=sup.get("needs"),
    )
    row["supplier_candidates"] = sup.get("candidates") or []
    row["recommended_quote_targets"] = sup.get("recommended") or []

    # 7 Pricing model
    pricing = build_pricing_model(
        row,
        row.get("historical_awards") or [],
        row.get("recommended_historical_market") or hist.get("recommended"),
        row.get("recommended_current_market") or curr.get("recommended"),
    )
    state["stages"]["pricing_model"] = _stage(
        pricing["stage_status"],
        confidence=pricing["stage_confidence"],
        historical_equivalent_bid=pricing.get("historical_equivalent_bid"),
        data_types=pricing.get("data_types"),
        note=pricing.get("note"),
        label=pricing.get("label"),
    )
    row["research_pricing_model"] = pricing

    # Estimated supplier cost from OUR history (never verified / never BID gate)
    try:
        from micro_purchase_lab_quote_intel import estimated_supplier_cost_band_for_test

        est = estimated_supplier_cost_band_for_test(row)
        row["estimated_supplier_cost"] = est
        if est and est.get("available"):
            pricing.setdefault("data_types", {})["estimated_supplier_cost"] = est
            pricing["data_types"]["estimated_label"] = "ESTIMATED_FROM_OUR_QUOTE_HISTORY"
    except Exception:
        row["estimated_supplier_cost"] = {"available": False}

    # Reject filters
    filters = evaluate_reject_filters(
        row,
        identity=ident_out.get("identity"),
        units=units,
        awards=row.get("historical_awards") or [],
        current_summary=curr.get("summary"),
        suppliers=row.get("supplier_candidates") or [],
        equiv=pricing.get("historical_equivalent_bid"),
    )
    state["reject_filters"] = filters
    hard_reject = any(f.get("action") == "REJECT" for f in filters)

    # 8 Quote prep stage (packets generated on Prepare Quotes; mark readiness here)
    can_prep = bool(row.get("recommended_quote_targets")) and not hard_reject
    if (ident_out.get("identity") or {}).get("needs") == "PRODUCT_IDENTITY_NEEDED":
        can_prep = False
    state["stages"]["quote_prep"] = _stage(
        STATUS_COMPLETE if can_prep else STATUS_PARTIAL,
        confidence=CONF_MODERATE if can_prep else CONF_WEAK,
        ready_to_prepare=can_prep,
        note="Use Prepare Quotes to generate packets and Quote Queue items",
    )

    # Summary
    snap_he = pricing.get("historical_equivalent_bid")
    curr_pub = (curr.get("summary") or {}).get("selected") or (curr.get("summary") or {}).get("lowest_verified")
    state["summary"] = {
        "product_identity": (ident_out.get("identity") or {}).get("confidence"),
        "historical_awards": len(row.get("historical_awards") or []),
        "historical_market_observations": len(hist.get("observations") or []),
        "current_market_observations": len(curr.get("observations") or []),
        "supplier_candidates": len(row.get("supplier_candidates") or []),
        "best_historical_equivalent_bid": snap_he,
        "current_public_market": curr_pub,
        "actual_reseller_quote": "NEEDED",
        "recommended_next": None,
    }
    targets = [t.get("supplier") for t in (row.get("recommended_quote_targets") or [])[:2]]
    if targets:
        state["summary"]["recommended_next"] = "REQUEST QUOTES FROM " + " + ".join(str(t) for t in targets if t)

    state["completed_at"] = _utc()
    state["running"] = False
    state["next_action"] = "REJECT" if hard_reject else None  # filled after enrich

    row["automated_research"] = state
    row["research_completed_at"] = state["completed_at"]
    # Operator review flag — evidence not silently trusted for economics if POSSIBLE-only
    row["research_needs_review"] = bool(
        ident_out.get("needs")
        or hist.get("needs")
        or curr.get("needs")
        or units.get("needs")
    )

    # Persist research without forcing quote; economics will set SUPPLIER_QUOTE_NEEDED
    from micro_purchase_lab_economics import enrich_test

    enriched = enrich_test(row)
    # Estimated cost must never flip to BID_CANDIDATE — derive_status already requires actual quotes
    if not (enriched.get("supplier_quotes") or []) and enriched.get("status") == "BID_CANDIDATE":
        enriched["status"] = "SUPPLIER_QUOTE_NEEDED"
    state["next_action"] = derive_next_action(enriched, state)
    enriched["lab_next_action"] = state["next_action"]
    enriched["automated_research"] = state
    state["summary"]["status_after_research"] = enriched.get("status")
    return enriched


def prepare_quotes_for_test(test: dict[str, Any], *, top_n: int = 4) -> dict[str, Any]:
    row = deepcopy(test)
    targets = row.get("recommended_quote_targets") or row.get("supplier_candidates") or []
    if not targets:
        # run lightweight discovery
        sup = discover_supplier_channels(row, None, load_opportunity_row(row.get("linked_opportunity_id")))
        targets = sup.get("recommended") or sup.get("candidates") or []
        row["supplier_candidates"] = sup.get("candidates") or []
        row["recommended_quote_targets"] = targets

    packets = []
    queue_items = []
    for s in targets[:top_n]:
        pkt = build_quote_packet(row, s)
        packets.append(pkt)
        snap = (row.get("economics_snapshot") or {})
        queue_items.append(
            {
                "id": f"QQ-{uuid4().hex[:10]}",
                "test_id": row.get("id"),
                "opportunity": row.get("product") or row.get("solicitation"),
                "solicitation": row.get("solicitation"),
                "supplier": pkt["supplier"],
                "product": row.get("product"),
                "part_number": row.get("part_number"),
                "nsn": row.get("nsn"),
                "quantity": row.get("quantity"),
                "deadline": row.get("deadline"),
                "historical_equivalent_price": snap.get("historical_equivalent_bid")
                or (row.get("research_pricing_model") or {}).get("historical_equivalent_bid"),
                "current_public_price": snap.get("current_market_price")
                or (row.get("recommended_current_market") or {}).get("unit_price"),
                "estimated_quote_value_band": (row.get("estimated_supplier_cost") or {}).get("band_label"),
                "quote_status": QUOTE_STATUS_READY,
                "supplier_account_status": s.get("account_required") and "ACCOUNT_MAY_BE_REQUIRED" or "UNKNOWN",
                "next_action": "REQUEST_SUPPLIER_QUOTE",
                "packet_id": pkt["id"],
                "quote_request_text": pkt["quote_request_text"],
                "reason_codes": pkt.get("reason_codes"),
                "created_at": _utc(),
                "updated_at": _utc(),
            }
        )

    row["quote_packets"] = packets
    row["quote_queue_items"] = queue_items
    row["quote_status"] = QUOTE_STATUS_READY
    from micro_purchase_lab_economics import enrich_test

    enriched = enrich_test(row)
    if not (enriched.get("supplier_quotes") or []):
        # Explicit stop condition from the build brief
        if enriched.get("status") not in {"PRODUCT_IDENTITY_NEEDED", "UNIT_NORMALIZATION_REQUIRED", "REJECT"}:
            # derive_status will already be SUPPLIER_QUOTE_NEEDED when quotes missing after market data
            pass
    enriched["lab_next_action"] = "REQUEST_SUPPLIER_QUOTE"
    research = enriched.get("automated_research") or empty_research_state()
    research["stages"]["quote_prep"] = _stage(
        STATUS_COMPLETE,
        confidence=CONF_STRONG,
        packets=len(packets),
        queue_items=len(queue_items),
    )
    research["next_action"] = "REQUEST_SUPPLIER_QUOTE"
    enriched["automated_research"] = research
    return enriched


def research_batch(test_ids: list[str], *, limit: int = 5, allow_paid_research: bool = False) -> dict[str, Any]:
    from micro_purchase_lab_service import get_test, save_test

    ids = list(test_ids or [])[: max(1, min(limit, 5))]
    results = []
    for tid in ids:
        try:
            t = get_test(tid)
            if not t:
                results.append({"test_id": tid, "ok": False, "error": "not_found"})
                continue
            out = research_opportunity(t, allow_paid_research=allow_paid_research)
            saved = save_test(out)
            results.append({"test_id": tid, "ok": True, "status": saved.get("status"), "next_action": saved.get("lab_next_action")})
        except Exception as exc:
            results.append({"test_id": tid, "ok": False, "error": str(exc)})
    return {"count": len(results), "results": results, "limit": limit}
