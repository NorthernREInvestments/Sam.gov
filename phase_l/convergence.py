"""Phase L.2.4 — price/history convergence: join, access gate, freight, queues."""

from __future__ import annotations

import re
from typing import Any

from application_clock import now_utc
from phase_l.commercial_identity import (
    PROMISING_UNIT_ECONOMICS,
    QUANTITY_REQUIRED,
    TOTAL_PROFIT_UNKNOWN_QUANTITY,
    compute_unit_spread,
)
from phase_l.economics import DEFAULT_FINANCING_RATE, build_phase_l_economics

# Convergence states
BOTH_FOUND_POSITIVE_SPREAD = "BOTH_FOUND_POSITIVE_SPREAD"
BOTH_FOUND_NEGATIVE_SPREAD = "BOTH_FOUND_NEGATIVE_SPREAD"
HISTORY_ONLY = "HISTORY_ONLY"
CURRENT_PRICE_ONLY = "CURRENT_PRICE_ONLY"
NEITHER_FOUND = "NEITHER_FOUND"
ACCESS_BLOCKED = "ACCESS_BLOCKED"
IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
CONFIGURATION_CONFLICT = "CONFIGURATION_CONFLICT"

# History confidence (L.2.4)
EXACT_PRODUCT_EXACT_UNIT = "EXACT_PRODUCT_EXACT_UNIT"
EXACT_PRODUCT_DERIVED_UNIT = "EXACT_PRODUCT_DERIVED_UNIT"
EXACT_MODEL = "EXACT_MODEL"
STRONG_COMPARABLE = "STRONG_COMPARABLE"
WEAK_COMPARABLE = "WEAK_COMPARABLE"
HISTORY_REJECTED = "REJECTED"

# Acquisition price types
PUBLIC_RETAIL = "PUBLIC_RETAIL"
OEM_MSRP = "OEM_MSRP"
DEALER_ADVERTISED = "DEALER_ADVERTISED"
AUTHORIZED_DISTRIBUTOR = "AUTHORIZED_DISTRIBUTOR"
COOPERATIVE_CONTRACT = "COOPERATIVE_CONTRACT"
STATE_TERM_CONTRACT = "STATE_TERM_CONTRACT"
PUBLIC_GOV_CHANNEL = "PUBLIC_GOV_CHANNEL"
PUBLIC_CATALOG = "PUBLIC_CATALOG"
MARKETPLACE = "MARKETPLACE"
QUOTE_REQUIRED = "QUOTE_REQUIRED"
NO_PUBLIC_PRICE = "NO_PUBLIC_PRICE"

# Price access
PRICE_ACCESS_YES = "YES"
PRICE_ACCESS_NO = "NO"
PRICE_ACCESS_CONDITIONAL = "CONDITIONAL"
PRICE_ACCESS_UNKNOWN = "UNKNOWN"

# Queues
QUEUE_PROFITABLE_ANY = "PROFITABLE_ANY_AMOUNT"
QUEUE_PROFITABLE_10K = "PROFITABLE_GE_10K"
QUEUE_PROMISING_UNIT = "PROMISING_UNIT_ECONOMICS"
QUEUE_NEGATIVE = "NEGATIVE_ECONOMICS"
QUEUE_INCOMPLETE = "RESEARCH_INCOMPLETE"

# Failure / signal codes
HISTORY_NOT_FOUND = "HISTORY_NOT_FOUND"
CURRENT_PRICE_NOT_FOUND = "CURRENT_PRICE_NOT_FOUND"
PRICE_INACCESSIBLE = "PRICE_INACCESSIBLE"
PRODUCT_PAGE_BLOCKED = "PRODUCT_PAGE_BLOCKED"
BOT_BLOCKED = "BOT_BLOCKED"
CONFIGURATION_UNRESOLVED = "CONFIGURATION_UNRESOLVED"
FREIGHT_UNRESOLVED = "FREIGHT_UNRESOLVED"
QUANTITY_UNKNOWN = "QUANTITY_UNKNOWN"
NEGATIVE_SPREAD = "NEGATIVE_SPREAD"
POSITIVE_SPREAD = "POSITIVE_SPREAD"
POSITIVE_NET = "POSITIVE_NET"

_HEAVY = re.compile(
    r"\b(vehicle|truck|excavator|bobcat|toolcat|tractor|forklift|crane|bus|suv|"
    r"generator|pump\s+station|equipment|machinery|police\s+responder)\b",
    re.I,
)
_AK = re.compile(r"\b(alaska|ak\b|juneau|anchorage|fairbanks)\b", re.I)
_HI = re.compile(r"\b(hawaii|hi\b|honolulu|maui|oahu)\b", re.I)
_PR = re.compile(r"\b(puerto\s+rico|pr\b|virgin\s+islands|guam|american\s+samoa)\b", re.I)
_OCONUS = re.compile(r"\b(oconus|overseas|germany|korea|japan|kuwait|qatar|bahrain)\b", re.I)

_GOV_ONLY = re.compile(
    r"\b(government\s+only|gov(?:ernment)?[\-\s]?only|federal\s+agencies?\s+only|"
    r"authorized\s+government\s+purchasers?|gsa\s+schedule\s+holders?\s+only|"
    r"contract\s+holders?\s+only)\b",
    re.I,
)
_COOP_ONLY = re.compile(
    r"\b(members?\s+only|cooperative\s+members?|participating\s+agencies?\s+only|"
    r"sourcewell\s+members?|omnia\s+partners?\s+only)\b",
    re.I,
)
_DEALER_ONLY = re.compile(
    r"\b(dealer[\-\s]?only|dealers?\s+only|authorized\s+dealers?\s+only|"
    r"reseller[\-\s]?only|var[\-\s]?only)\b",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def expanded_history_keywords(
    identity: dict[str, Any],
    row: dict[str, Any] | None = None,
    commercial: dict[str, Any] | None = None,
) -> list[str]:
    """≥3 identity approaches: NSN/MPN plus manufacturer+model commercial keys."""
    from phase_j.history_reconciliation import history_search_keywords

    row = row or {}
    commercial = commercial or {}
    keys: list[str] = []

    def add(q: str | None) -> None:
        q = re.sub(r"\s+", " ", str(q or "")).strip()
        if q and q not in keys and len(q) >= 3:
            keys.append(q)

    for k in history_search_keywords(identity):
        add(k)

    nsn = identity.get("nsn") or commercial.get("nsn")
    mpn = identity.get("mpn") or commercial.get("mpn") or identity.get("normalized_mpn")
    mfr = identity.get("manufacturer") or commercial.get("manufacturer")
    model = identity.get("model") or commercial.get("model")
    sku = identity.get("sku") or commercial.get("sku")
    title = (row.get("title") or commercial.get("raw_title") or "")[:80]

    if mpn:
        add(mpn)
        add(re.sub(r"[^A-Za-z0-9]", "", str(mpn)))
        if mfr:
            add(f"{mfr} {mpn}")
    if nsn:
        add(nsn)
        add(str(nsn).replace("-", ""))
    if mfr and model:
        add(f"{mfr} {model}")
        add(f'"{model}"')
        add(model)
    elif model:
        add(model)
        add(f'"{model}"')
    if sku:
        add(str(sku))
    if title and (mpn or model):
        add(title[:60])
    agency = row.get("agency") or row.get("department") or row.get("buyer")
    if agency and model:
        add(f"{agency} {model}")

    return keys[:12]


def history_query_approaches(keywords: list[str]) -> list[list[str]]:
    """Split into ≥3 distinct query approaches where possible."""
    if not keywords:
        return []
    approaches: list[list[str]] = []
    # 1: primary identity key
    approaches.append(keywords[:1])
    # 2: alternate / compact variant
    if len(keywords) >= 2:
        approaches.append(keywords[1:3])
    # 3: commercial model / title
    if len(keywords) >= 3:
        approaches.append(keywords[2:5])
    elif len(keywords) == 1:
        approaches.append(keywords)
        approaches.append(keywords)
    while len(approaches) < 3 and keywords:
        approaches.append(keywords[:1])
    return approaches[:5]


def map_history_confidence(history: dict[str, Any]) -> str:
    conf = str(history.get("history_confidence") or "").upper()
    if history.get("historical_award_unit_price") is None:
        return HISTORY_REJECTED
    if conf in {"EXACT_RECENT", "EXACT"}:
        return EXACT_PRODUCT_EXACT_UNIT
    if conf in {"EXACT_OLDER"}:
        return EXACT_PRODUCT_DERIVED_UNIT
    if conf in {"EXACT_MODEL", "EXACT_PRODUCT_EXACT_UNIT"}:
        return EXACT_MODEL if conf == "EXACT_MODEL" else EXACT_PRODUCT_EXACT_UNIT
    if conf in {"STRONG_COMPARABLE"}:
        return STRONG_COMPARABLE
    if conf in {"WEAK_COMPARABLE"}:
        return WEAK_COMPARABLE
    # L.2.5 procurement adapters (bid tab / council / PDF award)
    if history.get("source_type") in {
        "BID_TAB_HTML",
        "BOARD_COUNCIL",
        "PDF_PRICE",
        "STATE_LOCAL_AWARD",
        "COOPERATIVE_HISTORICAL",
    }:
        return EXACT_MODEL
    if history.get("history_research_state") == "HISTORY_FOUND":
        return STRONG_COMPARABLE
    return HISTORY_REJECTED


def history_economics_ok(confidence: str) -> bool:
    return confidence in {
        EXACT_PRODUCT_EXACT_UNIT,
        EXACT_PRODUCT_DERIVED_UNIT,
        EXACT_MODEL,
        STRONG_COMPARABLE,
    }


def classify_acquisition_price_type(
    *,
    url: str | None = None,
    page_type: str | None = None,
    seller_type: str | None = None,
    evidence_text: str | None = None,
) -> str:
    low = f"{url or ''} {evidence_text or ''}".lower()
    st = str(seller_type or "").upper()
    if any(x in low for x in ("sourcewell", "omnia", "naspo", "buyboard", "h-gac")):
        return COOPERATIVE_CONTRACT
    if any(x in low for x in ("state.", ".gov", "term contract", "statewide")):
        if "price" in low or "contract" in low:
            return STATE_TERM_CONTRACT
    if page_type in {"PDF_PRICE_LIST", "CONTRACT_CATALOG"} or low.endswith(".pdf"):
        return PUBLIC_CATALOG if "contract" not in low else PUBLIC_GOV_CHANNEL
    if st == "MANUFACTURER" or any(x in low for x in ("msrp", "list price")):
        return OEM_MSRP if "msrp" in low or "list price" in low else PUBLIC_RETAIL
    if st in {"AUTHORIZED_DISTRIBUTOR", "ESTABLISHED_DISTRIBUTOR"}:
        return AUTHORIZED_DISTRIBUTOR
    if st == "EQUIPMENT_DEALER" or "dealer" in low:
        return DEALER_ADVERTISED
    if st == "MARKETPLACE" or any(x in low for x in ("amazon.", "ebay.", "walmart.")):
        return MARKETPLACE
    if st == "RETAILER":
        return PUBLIC_RETAIL
    if "request a quote" in low or "call for price" in low:
        return QUOTE_REQUIRED
    return PUBLIC_RETAIL


def classify_price_access(
    *,
    price_type: str,
    url: str | None = None,
    evidence_text: str | None = None,
) -> dict[str, Any]:
    """Can *we* buy at this price? Gov-only / coop-member prices are intelligence only."""
    blob = f"{url or ''} {evidence_text or ''}"
    if _GOV_ONLY.search(blob):
        return {
            "price_access": PRICE_ACCESS_NO,
            "economics_eligible": False,
            "access_reason": "government_only",
        }
    if price_type in {COOPERATIVE_CONTRACT, STATE_TERM_CONTRACT, PUBLIC_GOV_CHANNEL}:
        if _COOP_ONLY.search(blob):
            return {
                "price_access": PRICE_ACCESS_CONDITIONAL,
                "economics_eligible": False,
                "access_reason": "cooperative_members_only",
            }
        # Public schedule pages without clear open retail → conditional
        return {
            "price_access": PRICE_ACCESS_CONDITIONAL,
            "economics_eligible": False,
            "access_reason": "channel_eligibility_unconfirmed",
        }
    if _DEALER_ONLY.search(blob):
        return {
            "price_access": PRICE_ACCESS_CONDITIONAL,
            "economics_eligible": False,
            "access_reason": "dealer_only",
        }
    if price_type == MARKETPLACE:
        return {
            "price_access": PRICE_ACCESS_YES,
            "economics_eligible": True,
            "access_reason": "public_marketplace",
            "confidence_penalty": "lower",
        }
    if price_type in {PUBLIC_RETAIL, OEM_MSRP, DEALER_ADVERTISED, AUTHORIZED_DISTRIBUTOR, PUBLIC_CATALOG}:
        return {
            "price_access": PRICE_ACCESS_YES,
            "economics_eligible": True,
            "access_reason": "publicly_advertised",
        }
    if price_type == QUOTE_REQUIRED:
        return {
            "price_access": PRICE_ACCESS_UNKNOWN,
            "economics_eligible": False,
            "access_reason": "quote_required",
        }
    return {
        "price_access": PRICE_ACCESS_UNKNOWN,
        "economics_eligible": False,
        "access_reason": "unknown",
    }


def classify_freight_screen(row: dict[str, Any]) -> dict[str, Any]:
    blob = " ".join(
        str(x)
        for x in (
            row.get("title"),
            row.get("description"),
            row.get("place_of_performance"),
            row.get("pop_state"),
            row.get("delivery_location"),
            row.get("ship_to"),
            row.get("state"),
        )
        if x
    )
    if _AK.search(blob):
        dest = "ALASKA"
    elif _HI.search(blob):
        dest = "HAWAII"
    elif _PR.search(blob):
        dest = "TERRITORY"
    elif _OCONUS.search(blob):
        dest = "OCONUS"
    else:
        dest = "CONUS"

    heavy = bool(_HEAVY.search(blob))
    freight_unresolved = dest != "CONUS" or heavy
    return {
        "destination_class": dest,
        "heavy_freight": heavy,
        "freight_unresolved": freight_unresolved,
        "freight_status": FREIGHT_UNRESOLVED if freight_unresolved else "FREIGHT_SCREEN_OK",
        "note": (
            "FREIGHT_REQUIRED_BEFORE_FINAL_PASS"
            if freight_unresolved
            else "standard_conus_parcel_or_ltl_ok_to_estimate"
        ),
    }


def select_usable_acquisition(
    evidence_or_market: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Pick best accessible acquisition price; keep inaccessible as intelligence."""
    items: list[dict[str, Any]] = []
    market_selected_ok = False
    if isinstance(evidence_or_market, dict):
        if evidence_or_market.get("verified_evidence"):
            items.extend(list(evidence_or_market["verified_evidence"]))
        elif evidence_or_market.get("evidence"):
            items.extend(
                e for e in evidence_or_market["evidence"] if e.get("economics_eligible")
            )
        if evidence_or_market.get("public_retail_unit_price") is not None and evidence_or_market.get(
            "economics_eligible", True
        ):
            market_selected_ok = True
            items.append(
                {
                    "price": evidence_or_market["public_retail_unit_price"],
                    "source_url": evidence_or_market.get("public_retail_source"),
                    "seller_type": (evidence_or_market.get("selected_observation") or {}).get("seller_type"),
                    "page_type": (evidence_or_market.get("selected_observation") or {}).get("page_type"),
                    "economics_eligible": True,
                    "confidence": evidence_or_market.get("l22_confidence") or "STRONG_VERIFIED",
                    "evidence_text": evidence_or_market.get("selection_reason"),
                }
            )
    else:
        items = list(evidence_or_market or [])

    usable: list[dict[str, Any]] = []
    intelligence: list[dict[str, Any]] = []
    for raw in items:
        price = _f(raw.get("price") or raw.get("unit_price") or raw.get("public_retail_unit_price"))
        if price is None or price <= 0:
            continue
        url = raw.get("source_url") or raw.get("url") or raw.get("public_retail_source")
        ptype = raw.get("acquisition_price_type") or classify_acquisition_price_type(
            url=url,
            page_type=raw.get("page_type"),
            seller_type=raw.get("seller_type"),
            evidence_text=raw.get("evidence_text") or raw.get("context_snippet"),
        )
        access = classify_price_access(
            price_type=ptype,
            url=url,
            evidence_text=raw.get("evidence_text") or raw.get("context_snippet"),
        )
        rec = {
            **{k: v for k, v in raw.items() if k not in access},
            "price": price,
            "unit_price": price,
            "acquisition_price_type": ptype,
            **access,
            "source_url": url,
        }
        verified = (
            raw.get("economics_eligible")
            or raw.get("confidence") in {"EXACT_VERIFIED", "STRONG_VERIFIED"}
            or market_selected_ok
        )
        if verified and access["economics_eligible"]:
            usable.append(rec)
        else:
            intelligence.append(rec)

    def score(r: dict[str, Any]) -> tuple:
        t = r.get("acquisition_price_type")
        pref = {
            AUTHORIZED_DISTRIBUTOR: 40,
            DEALER_ADVERTISED: 35,
            PUBLIC_RETAIL: 30,
            OEM_MSRP: 20,
            PUBLIC_CATALOG: 25,
            MARKETPLACE: 5,
        }.get(t, 10)
        return (pref, -abs(float(r["price"])))

    usable.sort(key=score, reverse=True)
    if usable:
        best = usable[0]
        return {
            "selected": best,
            "usable_count": len(usable),
            "intelligence": intelligence[:10],
            "public_retail_unit_price": best["price"],
            "public_retail_source": best.get("source_url"),
            "acquisition_price_type": best.get("acquisition_price_type"),
            "price_access": best.get("price_access"),
            "economics_eligible": True,
        }
    if intelligence:
        intel = intelligence[0]
        return {
            "selected": None,
            "usable_count": 0,
            "intelligence": intelligence[:10],
            "public_retail_unit_price": None,
            "intel_price": intel["price"],
            "intel_source": intel.get("source_url"),
            "acquisition_price_type": intel.get("acquisition_price_type"),
            "price_access": intel.get("price_access"),
            "economics_eligible": False,
            "drop_reason": PRICE_INACCESSIBLE,
        }
    return None


def join_history_and_price(
    *,
    row: dict[str, Any],
    history: dict[str, Any] | None,
    market: dict[str, Any] | None,
    commercial: dict[str, Any] | None = None,
    quantity: float | None = None,
) -> dict[str, Any]:
    """Join branches into an explicit convergence state + queues signals."""
    history = history or {}
    market = market or {}
    commercial = commercial or {}
    freight = classify_freight_screen(row)

    hist_conf = map_history_confidence(history)
    hist_ok = history_economics_ok(hist_conf) and _f(history.get("historical_award_unit_price")) is not None
    hist_u = _f(history.get("historical_award_unit_price")) if hist_ok else None

    # Current usable acquisition
    if market.get("identity_conflict") or market.get("drop_reason") == "PRODUCT_IDENTITY_MISMATCH":
        # Only if no usable price
        pass
    acquisition = select_usable_acquisition(market)
    retail_u = None
    price_access = PRICE_ACCESS_UNKNOWN
    price_type = NO_PUBLIC_PRICE
    if acquisition and acquisition.get("economics_eligible") and acquisition.get("public_retail_unit_price"):
        retail_u = float(acquisition["public_retail_unit_price"])
        price_access = acquisition.get("price_access") or PRICE_ACCESS_YES
        price_type = acquisition.get("acquisition_price_type") or PUBLIC_RETAIL
    elif acquisition and acquisition.get("drop_reason") == PRICE_INACCESSIBLE:
        price_access = acquisition.get("price_access") or PRICE_ACCESS_NO
        price_type = acquisition.get("acquisition_price_type") or PUBLIC_GOV_CHANNEL

    cfg = commercial.get("configuration_completeness") or market.get("configuration_match")
    if cfg in {"CONFIGURATION_CONFLICT", "CONFIGURATION_MISMATCH"}:
        state = CONFIGURATION_CONFLICT
    elif market.get("drop_reason") == "PRODUCT_IDENTITY_MISMATCH" and retail_u is None and hist_u is None:
        state = IDENTITY_CONFLICT
    elif hist_u is not None and retail_u is not None:
        state = (
            BOTH_FOUND_POSITIVE_SPREAD
            if hist_u > retail_u
            else BOTH_FOUND_NEGATIVE_SPREAD
        )
    elif hist_u is not None:
        state = HISTORY_ONLY
    elif retail_u is not None:
        state = CURRENT_PRICE_ONLY
    elif price_access in {PRICE_ACCESS_NO, PRICE_ACCESS_CONDITIONAL} and acquisition and acquisition.get("intel_price"):
        state = ACCESS_BLOCKED
    else:
        state = NEITHER_FOUND

    qty = _f(quantity)
    unit = compute_unit_spread(
        historical_unit_price=hist_u,
        public_retail_unit_price=retail_u,
        quantity=qty,
        financing_rate=DEFAULT_FINANCING_RATE,
    )

    codes: list[str] = []
    if hist_u is None:
        codes.append(HISTORY_NOT_FOUND)
    if retail_u is None:
        if state == ACCESS_BLOCKED:
            codes.append(PRICE_INACCESSIBLE)
        else:
            codes.append(CURRENT_PRICE_NOT_FOUND)
    if freight.get("freight_unresolved"):
        codes.append(FREIGHT_UNRESOLVED)
    if qty is None:
        codes.append(QUANTITY_UNKNOWN)
    if unit.get("unit_raw_spread") is not None:
        codes.append(POSITIVE_SPREAD if unit["unit_raw_spread"] > 0 else NEGATIVE_SPREAD)

    # Total economics only when qty known AND freight not blocking final pass
    expected_net = None
    economics_completed = False
    meets_any_profit = False
    meets_10k = False
    if (
        hist_u is not None
        and retail_u is not None
        and qty is not None
        and state.startswith("BOTH_FOUND")
        and not freight.get("freight_unresolved")
        and cfg not in {"CONFIGURATION_CONFLICT", "CONFIGURATION_MISMATCH"}
    ):
        econ = build_phase_l_economics(
            quantity=qty,
            historical_unit_price=hist_u,
            public_retail_unit_price=retail_u,
            public_retail_source=(acquisition or {}).get("public_retail_source"),
        )
        expected_net = econ.get("expected_net_profit")
        economics_completed = expected_net is not None
        if isinstance(expected_net, (int, float)):
            if expected_net > 0:
                meets_any_profit = True
                codes.append(POSITIVE_NET)
            if expected_net >= 10000:
                meets_10k = True
    elif (
        hist_u is not None
        and retail_u is not None
        and qty is not None
        and freight.get("freight_unresolved")
    ):
        # Raw total spread visible but not final net
        codes.append(FREIGHT_UNRESOLVED)

    # Queue assignment
    queue = QUEUE_INCOMPLETE
    if state == ACCESS_BLOCKED:
        queue = QUEUE_INCOMPLETE
        codes.append(ACCESS_BLOCKED if ACCESS_BLOCKED not in codes else PRICE_INACCESSIBLE)
    elif economics_completed and meets_10k:
        queue = QUEUE_PROFITABLE_10K
    elif economics_completed and meets_any_profit:
        queue = QUEUE_PROFITABLE_ANY
    elif economics_completed and expected_net is not None and expected_net <= 0:
        queue = QUEUE_NEGATIVE
    elif (
        hist_u is not None
        and retail_u is not None
        and unit.get("unit_raw_spread") is not None
        and unit["unit_raw_spread"] > 0
        and (qty is None or freight.get("freight_unresolved"))
    ):
        queue = QUEUE_PROMISING_UNIT
    elif state in {HISTORY_ONLY, CURRENT_PRICE_ONLY, NEITHER_FOUND, IDENTITY_CONFLICT, CONFIGURATION_CONFLICT}:
        queue = QUEUE_INCOMPLETE
    elif state == BOTH_FOUND_NEGATIVE_SPREAD and qty is not None:
        queue = QUEUE_NEGATIVE

    return {
        "kind": "PhaseL24Convergence",
        "build": "20260927-m3-phase-l24-price-history-convergence",
        "convergence_state": state,
        "history_confidence_l24": hist_conf,
        "history_unit": hist_u,
        "current_unit": retail_u,
        "acquisition_price_type": price_type,
        "price_access": price_access,
        "acquisition": acquisition,
        "unit_economics": unit,
        "freight": freight,
        "expected_net_profit": expected_net,
        "economics_completed": economics_completed,
        "meets_any_positive_profit": meets_any_profit,
        "meets_floor_10k": meets_10k,
        "queue": queue,
        "codes": codes,
        "ready_to_bid": False,
        "joined_at": _utc(),
        "research_signal": unit.get("research_signal")
        or (
            f"{PROMISING_UNIT_ECONOMICS} / {QUANTITY_REQUIRED}"
            if queue == QUEUE_PROMISING_UNIT
            else None
        ),
        "total_status": unit.get("total_status")
        or (TOTAL_PROFIT_UNKNOWN_QUANTITY if qty is None else None),
    }
