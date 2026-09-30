"""Phase L.2.6 — progressive funnel: cheap broad → costly deep; strict only at the end.

Stages answer different questions:
  0–1: worth keeping?
  2: enough identity to search?
  3: economically promising enough to spend more?
  4: deep research (expensive)
  5: verified economics
  6: pre-bid compliance (READY_TO_BID still strict)
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.commercial_identity import (
    apply_commercial_overlay_to_screen,
    compute_unit_spread,
    recover_commercial_identity,
)
from phase_l.acquisition_lanes import (
    COMMERCIAL_DISTRIBUTOR_CHANNEL,
    COMMERCIAL_OPEN_CHANNEL,
    MILSPEC_OPEN_CHANNEL,
    MILSPEC_SPECIALTY,
    QUOTE_REQUIRED_COMMERCIAL,
    SOLE_SOURCE_RESTRICTED,
    SOURCE_APPROVAL_REQUIRED,
    classify_acquisition_lane,
    LANE_PRIORITY,
)
from phase_l.convergence import (
    classify_freight_screen,
    join_history_and_price,
)
from phase_l.deadline_freshness import classify_deadline_freshness
from phase_l.economics import DEFAULT_FINANCING_RATE
from phase_l.enrichment import enrichment_priority, stage_a_identity
from phase_l.prebid_compliance import evaluate_prebid_compliance
from phase_l.product_fitness import (
    ENGINEERING_SUPPORT,
    MIXED_PRODUCT_SERVICE,
    PRODUCT_RESALE,
    PRODUCT_WITH_INCIDENTAL_SERVICE,
    REPAIR_OVERHAUL,
    SERVICE,
    classify_product_fitness,
)

# ---------------------------------------------------------------------------
# Triage / research priority states
# ---------------------------------------------------------------------------
KEEP = "KEEP"
PROMISING = "PROMISING"
LOW_PRIORITY = "LOW_PRIORITY"
HARD_REJECT = "HARD_REJECT"

DEEP_RESEARCH_HIGH = "DEEP_RESEARCH_HIGH"
DEEP_RESEARCH_MEDIUM = "DEEP_RESEARCH_MEDIUM"
DEEP_RESEARCH_LOW = "DEEP_RESEARCH_LOW"
DO_NOT_SPEND_MORE = "DO_NOT_SPEND_MORE"

PROMISING_ECONOMICS = "PROMISING_ECONOMICS"
QUANTITY_REQUIRED_FOR_TOTAL = "QUANTITY_REQUIRED_FOR_TOTAL"
FREIGHT_REQUIRED = "FREIGHT_REQUIRED"
CONFIGURATION_RESEARCH_REQUIRED = "CONFIGURATION_RESEARCH_REQUIRED"
NEGATIVE_STOP_LOSS = "NEGATIVE_STOP_LOSS"

# Queues
QUEUE_BROAD = "BROAD_CANDIDATES"
QUEUE_ECON_RECON = "ECONOMIC_RECON_QUEUE"
QUEUE_PROMISING_DEEP = "PROMISING_DEEP_RESEARCH"
QUEUE_DEEP_IN_PROGRESS = "DEEP_RESEARCH_IN_PROGRESS"
QUEUE_PROFITABLE_COMPLIANCE = "PROFITABLE_NEEDS_COMPLIANCE"
QUEUE_BID_CANDIDATE = "BID_CANDIDATE"

# Confidence
CONF_EXACT = "EXACT"
CONF_STRONG = "STRONG"
CONF_APPROXIMATE = "APPROXIMATE"
CONF_PARTIAL = "PARTIAL"
CONF_KNOWN = "KNOWN"
CONF_ESTIMATED = "ESTIMATED"
CONF_UNKNOWN = "UNKNOWN"

# Relative cost ceilings (units, not USD)
STAGE_BUDGET = {
    0: 0,
    1: 1,
    2: 3,
    3: 12,
    4: 80,
    5: 20,
    6: 15,
}

ROOT = Path(__file__).resolve().parents[1]
MEMORY_PATH = ROOT / "data" / "phase_l26_known_product_memory.json"

_EXPIRED = re.compile(r"\b(expired|cancelled|canceled|awarded|closed)\b", re.I)
_CONSTRUCTION = re.compile(
    r"\b(construction|renovation|demolition|paving|asphalt|concrete\s+pour|"
    r"labor[\-\s]?only|janitorial|custodial|food\s+service|catering|"
    r"perishable|meal\s+service)\b",
    re.I,
)
_SOLE_INACCESSIBLE = re.compile(
    r"\b(sole[\-\s]?source|brand[\-\s]?name\s+only|approved\s+source\s+list|"
    r"source\s+approval\s+required|must\s+be\s+oem\s+authorized)\b",
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


def _blob(row: dict[str, Any]) -> str:
    return "\n".join(
        str(x or "")
        for x in (
            row.get("title"),
            row.get("description"),
            row.get("solicitation_text"),
            row.get("agency"),
            row.get("department"),
        )
    )


# ---------------------------------------------------------------------------
# Cost ledger (per opportunity)
# ---------------------------------------------------------------------------
@dataclass
class ResearchCostLedger:
    stage: int = 0
    requests: int = 0
    searches: int = 0
    llm_calls: int = 0
    estimated_cost_units: float = 0.0
    time_ms: float = 0.0
    notes: list[str] = field(default_factory=list)

    def charge(self, *, units: float, kind: str = "request", note: str = "") -> bool:
        """Return False if stage budget would be exceeded (stop-loss)."""
        ceiling = STAGE_BUDGET.get(self.stage, 80)
        if self.estimated_cost_units + units > ceiling and ceiling > 0:
            self.notes.append(f"budget_exceeded:{kind}:{note}")
            return False
        self.estimated_cost_units += units
        if kind == "search":
            self.searches += 1
        elif kind == "llm":
            self.llm_calls += 1
        else:
            self.requests += 1
        if note:
            self.notes.append(note)
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Known product memory
# ---------------------------------------------------------------------------
def load_product_memory() -> dict[str, Any]:
    if MEMORY_PATH.exists():
        try:
            return json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {"products": {}}
    return {"products": {}}


def save_product_memory(mem: dict[str, Any]) -> None:
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_PATH.write_text(json.dumps(mem, indent=2, default=str), encoding="utf-8")


def product_memory_key(identity: dict[str, Any], commercial: dict[str, Any]) -> str | None:
    parts = [
        commercial.get("manufacturer") or identity.get("manufacturer"),
        commercial.get("model") or identity.get("model"),
        commercial.get("mpn") or identity.get("mpn") or identity.get("nsn"),
    ]
    key = "|".join(str(p).strip().upper() for p in parts if p)
    return key if len(key) >= 4 else None


def recall_product(mem: dict[str, Any], key: str | None) -> dict[str, Any] | None:
    if not key:
        return None
    return (mem.get("products") or {}).get(key)


def remember_product(mem: dict[str, Any], key: str | None, payload: dict[str, Any]) -> None:
    if not key:
        return
    products = mem.setdefault("products", {})
    prev = dict(products.get(key) or {})
    prev.update({k: v for k, v in payload.items() if v is not None})
    prev["updated_at"] = _utc()
    products[key] = prev


# ---------------------------------------------------------------------------
# Stage 0 — broad discovery (cheap)
# ---------------------------------------------------------------------------
def stage0_broad_discovery(row: dict[str, Any]) -> dict[str, Any]:
    """Reject only obvious hard failures. No MPN/history/qty/price required."""
    blob = _blob(row)
    fresh = classify_deadline_freshness(row)
    if not fresh.get("deep_enrichment_allowed") and fresh.get("deadline_state") in {
        "EXPIRED",
        "CANCELLED",
        "AWARDED",
        "ARCHIVAL",
    }:
        return {
            "stage": 0,
            "pass": False,
            "state": HARD_REJECT,
            "reason": f"deadline:{fresh.get('deadline_state')}",
            "deadline": fresh,
        }
    if _CONSTRUCTION.search(blob) and not re.search(r"\b(NSN|P/?N|equipment|vehicle|supply)\b", blob, re.I):
        return {"stage": 0, "pass": False, "state": HARD_REJECT, "reason": "construction_labor_food", "deadline": fresh}
    access = str(row.get("our_bid_access") or "").upper()
    if access == "NO":
        return {"stage": 0, "pass": False, "state": HARD_REJECT, "reason": "access_no", "deadline": fresh}
    # Sole-source / source-approval we clearly cannot satisfy
    if _SOLE_INACCESSIBLE.search(blob) and access not in {"YES", "CONDITIONAL"}:
        return {"stage": 0, "pass": False, "state": HARD_REJECT, "reason": "inaccessible_source_requirement", "deadline": fresh}
    return {
        "stage": 0,
        "pass": True,
        "state": KEEP,
        "reason": "broad_keep",
        "deadline": fresh,
        "source_level": row.get("source_level") or row.get("level"),
    }


# ---------------------------------------------------------------------------
# Stage 1 — cheap triage
# ---------------------------------------------------------------------------
def stage1_cheap_triage(row: dict[str, Any], *, stage0: dict[str, Any] | None = None) -> dict[str, Any]:
    if stage0 and not stage0.get("pass"):
        return {**stage0, "stage": 1, "triage_state": HARD_REJECT}

    fit = classify_product_fitness(row)
    fitness = fit.get("product_fitness")
    access = str(row.get("our_bid_access") or "").upper()
    blob = _blob(row)

    if fitness in {SERVICE, ENGINEERING_SUPPORT, REPAIR_OVERHAUL}:
        return {
            "stage": 1,
            "pass": False,
            "triage_state": HARD_REJECT,
            "reason": f"fitness:{fitness}",
            "product_fitness": fit,
        }
    if fitness == MIXED_PRODUCT_SERVICE:
        triage = LOW_PRIORITY
        keep = True
    elif fitness in {PRODUCT_RESALE, PRODUCT_WITH_INCIDENTAL_SERVICE}:
        triage = PROMISING if access == "YES" else KEEP
        keep = True
    else:
        triage = LOW_PRIORITY
        keep = True

    # Boost obvious commerciality
    if re.search(r"\b(Bobcat|Ford|Dell|Caterpillar|\bCAT\b|ToolCat|Police\s+Responder)\b", blob, re.I):
        if triage != HARD_REJECT:
            triage = PROMISING
    if access == "YES" and fitness in {PRODUCT_RESALE, PRODUCT_WITH_INCIDENTAL_SERVICE}:
        triage = PROMISING if triage != HARD_REJECT else triage

    # L.3 — cheap acquisition-lane classification (not a rejection)
    lane = classify_acquisition_lane(row)
    from phase_l.commercial_feed_expansion import stage1_commercial_triage_status

    commercial_triage = stage1_commercial_triage_status(row, lane=lane)
    if lane.get("primary_research_priority") and triage != HARD_REJECT:
        triage = PROMISING
    if lane.get("specialty_pipeline") and triage == PROMISING:
        # Keep, but mark lower urgency — still passes Stage 1
        triage = KEEP

    return {
        "stage": 1,
        "pass": keep and triage != HARD_REJECT,
        "triage_state": triage,
        "commercial_triage_status": commercial_triage,
        "reason": f"fitness={fitness};access={access};lane={lane.get('acquisition_lane')}",
        "product_fitness": fit,
        "acquisition_lane": lane.get("acquisition_lane"),
        "lane_priority": lane.get("lane_priority"),
        "lane_classification": lane,
        "queue": QUEUE_BROAD if keep else None,
    }


# ---------------------------------------------------------------------------
# Stage 2 — identity enrichment (PARTIAL can proceed)
# ---------------------------------------------------------------------------
def stage2_identity_anchor(row: dict[str, Any]) -> dict[str, Any]:
    """Recover identity; Stage 2 admits researchable uncertainty (L.5).

    Missing MPN / quantity / history / public price is NOT a Stage 2 failure.
    Exact identity remains required only for final readiness (Stage 5/6).
    """
    from phase_l.stage2_admission import (
        CONFIGURATION_RESEARCH_REQUIRED,
        DOCUMENT_ENRICHMENT_REQUIRED,
        QUANTITY_UNRESOLVED,
        SPEC_DRIVEN_COMMERCIAL_PRODUCT,
        collect_stage2_anchors_l5,
        stage1_l5_outcome,
    )

    screen0 = stage_a_identity(row)
    screen = apply_commercial_overlay_to_screen(screen0, row)
    commercial = screen.get("commercial_identity") or {}
    identity = screen.get("identity") or {}

    l5 = collect_stage2_anchors_l5(row, commercial=commercial, identity=identity)
    anchors = l5["anchors"]
    recon_eligible = bool(l5["pass"])

    identity_conf = CONF_EXACT if "mpn" in anchors or "nsn" in anchors else (
        CONF_STRONG if "manufacturer_model" in anchors or "commercial_state" in anchors else (
            CONF_PARTIAL if anchors else CONF_UNKNOWN
        )
    )

    pri = enrichment_priority(row, screen)
    lane = classify_acquisition_lane(row, commercial=commercial)
    qty = screen.get("quantity")
    qty_status = QUANTITY_UNRESOLVED if qty is None else "QUANTITY_RESOLVED"

    return {
        "stage": 2,
        "pass": recon_eligible,
        "recon_identity_eligible": recon_eligible,
        "market_research_eligible": bool(screen.get("market_research_eligible")),
        "identity_anchors": anchors,
        "identity_confidence": identity_conf,
        "identity_state": commercial.get("commercial_identity_state") or screen.get("identity_research_state"),
        "configuration_completeness": commercial.get("configuration_completeness")
        or (CONFIGURATION_RESEARCH_REQUIRED if SPEC_DRIVEN_COMMERCIAL_PRODUCT in (l5.get("statuses") or []) else None),
        "quantity": qty,
        "quantity_status": qty_status,
        "uom": screen.get("uom"),
        "screen": screen,
        "commercial": commercial,
        "identity": identity,
        "priority": pri,
        "acquisition_lane": lane.get("acquisition_lane"),
        "lane_priority": lane.get("lane_priority"),
        "lane_classification": lane,
        "commercial_acquisition_score": (lane.get("commercial_acquisition_score") or {}).get("score"),
        "researchability_score": (l5.get("researchability") or {}).get("score"),
        "l5_statuses": l5.get("statuses") or [],
        "brand_or_equal": l5.get("brand_or_equal"),
        "document_enrichment_required": DOCUMENT_ENRICHMENT_REQUIRED in (l5.get("statuses") or []),
        "legacy_strict_would_pass": l5.get("legacy_strict_would_pass"),
        "stage1_l5_outcome": stage1_l5_outcome(row, lane=lane),
        "admission_policy": "L5_PERMISSIVE",
        "reason": "anchors=" + ",".join(anchors) if anchors else "no_identity_anchor",
        "queue": QUEUE_ECON_RECON if recon_eligible else None,
    }


# ---------------------------------------------------------------------------
# Stage 3 — economic reconnaissance (ranges OK; missing qty/freight don't kill)
# ---------------------------------------------------------------------------
def _approx_hist_from_row(row: dict[str, Any], memory: dict[str, Any] | None) -> tuple[float | None, float | None, str]:
    """Return (low, high, confidence) for historical side."""
    for k in ("historical_award_unit_price", "historical_unit_price", "award_unit_price"):
        v = _f(row.get(k))
        if v and v > 0:
            return v, v, CONF_EXACT
    # Budget / ceiling hints
    for k in ("estimated_value", "budget", "ceiling", "total_value", "award_amount"):
        v = _f(row.get(k))
        if v and v > 100:
            # treat as total-ish; leave as approximate unit unknown
            return v * 0.4, v * 0.9, CONF_APPROXIMATE
    if memory:
        hv = _f(memory.get("median_government_price") or memory.get("historical_unit_price"))
        if hv and hv > 0:
            return hv * 0.9, hv * 1.1, CONF_STRONG
    return None, None, CONF_UNKNOWN


def _approx_acq_from_row(row: dict[str, Any], memory: dict[str, Any] | None, commercial: dict[str, Any]) -> tuple[float | None, float | None, str]:
    for k in ("public_retail_unit_price", "acquisition_unit_price", "msrp"):
        v = _f(row.get(k))
        if v and v > 0:
            return v, v, CONF_EXACT if k != "msrp" else CONF_STRONG
    if memory:
        av = _f(memory.get("known_acquisition_benchmark") or memory.get("public_retail_unit_price"))
        if av and av > 0:
            return av * 0.95, av * 1.15, CONF_STRONG
    # Very rough category MSRP placeholders — only for ranking, never finalize
    st = str(commercial.get("commercial_identity_state") or "")
    title = str(row.get("title") or "").lower()
    if "VEHICLE" in st or "police" in title or "f-150" in title or "suv" in title:
        return 35000.0, 65000.0, CONF_APPROXIMATE
    if "EQUIPMENT" in st or "bobcat" in title or "toolcat" in title or "excavator" in title:
        return 45000.0, 90000.0, CONF_APPROXIMATE
    if commercial.get("mpn") or commercial.get("model"):
        return 50.0, 5000.0, CONF_APPROXIMATE
    return None, None, CONF_UNKNOWN


def stage3_economic_recon(
    row: dict[str, Any],
    *,
    stage2: dict[str, Any],
    recurring_boost: float = 0.0,
    memory: dict[str, Any] | None = None,
    ledger: ResearchCostLedger | None = None,
) -> dict[str, Any]:
    """Estimate promising economics BEFORE perfect evidence. Ranges + labels."""
    ledger = ledger or ResearchCostLedger(stage=3)
    ledger.charge(units=2, kind="request", note="econ_recon")

    commercial = stage2.get("commercial") or {}
    qty = _f(stage2.get("quantity") or row.get("quantity"))
    freight = classify_freight_screen(row)
    config = commercial.get("configuration_completeness") or CONF_UNKNOWN

    hist_lo, hist_hi, hist_conf = _approx_hist_from_row(row, memory)
    acq_lo, acq_hi, acq_conf = _approx_acq_from_row(row, memory, commercial)

    spread_lo = spread_hi = None
    if hist_lo is not None and acq_hi is not None:
        spread_lo = round(hist_lo - acq_hi, 2)
    if hist_hi is not None and acq_lo is not None:
        spread_hi = round(hist_hi - acq_lo, 2)

    unit = compute_unit_spread(
        historical_unit_price=hist_lo if hist_conf in {CONF_EXACT, CONF_STRONG} else None,
        public_retail_unit_price=acq_lo if acq_conf in {CONF_EXACT, CONF_STRONG} else None,
        quantity=qty,
        financing_rate=DEFAULT_FINANCING_RATE,
    )

    signals: list[str] = []
    apparent_positive = False
    if spread_hi is not None and spread_hi > 0:
        apparent_positive = True
        signals.append(PROMISING_ECONOMICS)
    if spread_lo is not None and spread_lo > 0 and hist_conf != CONF_APPROXIMATE:
        apparent_positive = True
        signals.append(PROMISING_ECONOMICS)
    # Approximate ranges can still mark promising for ranking
    if (
        hist_conf == CONF_APPROXIMATE
        and acq_conf == CONF_APPROXIMATE
        and spread_hi is not None
        and spread_hi > 1000
    ):
        apparent_positive = True
        signals.append(PROMISING_ECONOMICS)

    if qty is None and (spread_hi or 0) > 0:
        signals.append(QUANTITY_REQUIRED_FOR_TOTAL)
    if freight.get("freight_unresolved") and apparent_positive:
        signals.append(FREIGHT_REQUIRED)
    if config in {"PARTIAL", "INCOMPLETE", "CONFIGURATION_PARTIAL", None, CONF_UNKNOWN} and commercial.get("model"):
        signals.append(CONFIGURATION_RESEARCH_REQUIRED)

    # Stop-loss: clearly negative with strong evidence
    stop_loss = False
    stop_reason = None
    if (
        hist_conf in {CONF_EXACT, CONF_STRONG}
        and acq_conf in {CONF_EXACT, CONF_STRONG}
        and spread_hi is not None
        and spread_hi < 0
    ):
        stop_loss = True
        stop_reason = "acquisition_exceeds_government_price"
        signals.append(NEGATIVE_STOP_LOSS)

    # Research priority score
    score = 0
    if stage2.get("recon_identity_eligible"):
        score += 20
    if stage2.get("market_research_eligible"):
        score += 15
    if hist_conf in {CONF_EXACT, CONF_STRONG}:
        score += 25
    elif hist_conf == CONF_APPROXIMATE:
        score += 8
    if acq_conf in {CONF_EXACT, CONF_STRONG}:
        score += 25
    elif acq_conf == CONF_APPROXIMATE:
        score += 8
    if apparent_positive:
        score += 30
    if spread_hi and spread_hi >= 10000:
        score += 20
    elif spread_hi and spread_hi >= 1000:
        score += 10
    if str(row.get("our_bid_access") or "").upper() == "YES":
        score += 15
    score += int(recurring_boost)
    if memory:
        score += 10  # known product reuse
    fresh = classify_deadline_freshness(row)
    runway = fresh.get("days_remaining")
    if isinstance(runway, (int, float)) and runway >= 7:
        score += 5

    # L.3 — acquisition-lane ranking boost (not exclusion)
    lane = stage2.get("acquisition_lane") or (classify_acquisition_lane(row, commercial=commercial).get("acquisition_lane"))
    lane_pri = int(stage2.get("lane_priority") or LANE_PRIORITY.get(lane, 99))
    cas = stage2.get("commercial_acquisition_score")
    if isinstance(cas, dict):
        cas = cas.get("score")
    cas_n = int(cas or 0)
    if lane in {COMMERCIAL_OPEN_CHANNEL, COMMERCIAL_DISTRIBUTOR_CHANNEL}:
        score += 35
    elif lane == QUOTE_REQUIRED_COMMERCIAL:
        score += 25
    elif lane == MILSPEC_OPEN_CHANNEL:
        score += 15
    elif lane == MILSPEC_SPECIALTY:
        score -= 20  # deprioritize specialty crowding, do not reject
    elif lane in {SOURCE_APPROVAL_REQUIRED, SOLE_SOURCE_RESTRICTED}:
        score -= 30
    score += max(0, min(20, cas_n // 5))

    if stop_loss:
        deep = DO_NOT_SPEND_MORE
    elif lane in {SOURCE_APPROVAL_REQUIRED, SOLE_SOURCE_RESTRICTED}:
        deep = DO_NOT_SPEND_MORE  # specialty pipeline — no deep spend
    elif lane == MILSPEC_SPECIALTY and score < 50:
        deep = DEEP_RESEARCH_LOW  # limited specialty research
    elif score >= 70:
        deep = DEEP_RESEARCH_HIGH
    elif score >= 40:
        deep = DEEP_RESEARCH_MEDIUM
    elif score >= 20:
        deep = DEEP_RESEARCH_LOW
    else:
        deep = DO_NOT_SPEND_MORE

    # Survive Stage 3 unless stop-loss / no anchors
    survive = bool(stage2.get("recon_identity_eligible")) and not stop_loss

    return {
        "stage": 3,
        "pass": survive,
        "promising_economics": apparent_positive,
        "signals": list(dict.fromkeys(signals)),
        "historical_range": {"low": hist_lo, "high": hist_hi, "confidence": hist_conf},
        "acquisition_range": {"low": acq_lo, "high": acq_hi, "confidence": acq_conf},
        "spread_range": {"low": spread_lo, "high": spread_hi},
        "unit_economics_verified": unit if hist_conf in {CONF_EXACT, CONF_STRONG} and acq_conf in {CONF_EXACT, CONF_STRONG} else None,
        "quantity": qty,
        "freight": freight,
        "freight_confidence": CONF_UNKNOWN if freight.get("freight_unresolved") else CONF_ESTIMATED,
        "configuration_status": config,
        "research_priority_score": score,
        "deep_research_priority": deep,
        "acquisition_lane": lane,
        "lane_priority": lane_pri,
        "commercial_acquisition_score": cas_n,
        "stop_loss": stop_loss,
        "stop_reason": stop_reason,
        "can_finalize_economics": (
            hist_conf in {CONF_EXACT, CONF_STRONG}
            and acq_conf in {CONF_EXACT, CONF_STRONG}
            and qty is not None
            and not freight.get("freight_unresolved")
        ),
        "approx_may_rank_not_finalize": hist_conf == CONF_APPROXIMATE or acq_conf == CONF_APPROXIMATE,
        "cost": ledger.to_dict(),
        "queue": QUEUE_PROMISING_DEEP if deep in {DEEP_RESEARCH_HIGH, DEEP_RESEARCH_MEDIUM} else (
            QUEUE_ECON_RECON if survive else None
        ),
        "memory_hit": bool(memory),
    }


# ---------------------------------------------------------------------------
# Stage 4–6 wrappers
# ---------------------------------------------------------------------------
def should_deep_research(stage3: dict[str, Any]) -> bool:
    return stage3.get("deep_research_priority") in {DEEP_RESEARCH_HIGH, DEEP_RESEARCH_MEDIUM}


def stage5_final_economic_gate(
    *,
    row: dict[str, Any],
    history: dict[str, Any] | None,
    market: dict[str, Any] | None,
    commercial: dict[str, Any] | None,
    quantity: float | None,
) -> dict[str, Any]:
    """Strict join — approximate evidence must not finalize."""
    # Strip approximate-only row fields: join uses history/market evidence only
    hist = dict(history or {})
    mkt = dict(market or {})
    # If history confidence is approximate-only and no verified unit, clear
    if hist.get("history_confidence") in {"WEAK_COMPARABLE", "APPROXIMATE"} and not hist.get(
        "historical_identity_match_type"
    ):
        # keep price for ranking elsewhere; join_history_and_price uses map_history_confidence
        pass

    joined = join_history_and_price(
        row=row,
        history=hist,
        market=mkt,
        commercial=commercial,
        quantity=quantity,
    )
    net = joined.get("expected_net_profit")
    band = None
    if isinstance(net, (int, float)):
        if net <= 0:
            band = "negative"
        elif net < 10000:
            band = "positive_lt_10k"
        elif net < 25000:
            band = "ge_10k"
        elif net < 50000:
            band = "ge_25k"
        elif net < 75000:
            band = "ge_50k"
        elif net < 100000:
            band = "ge_75k"
        else:
            band = "ge_100k"

    return {
        "stage": 5,
        "joined": joined,
        "economics_completed": bool(joined.get("economics_completed")),
        "expected_net_profit": net,
        "profit_band": band,
        "verified_positive": isinstance(net, (int, float)) and net > 0 and bool(joined.get("economics_completed")),
        "queue": QUEUE_PROFITABLE_COMPLIANCE if (isinstance(net, (int, float)) and net > 0 and joined.get("economics_completed")) else None,
    }


def stage6_prebid_gate(row: dict[str, Any], *, stage5: dict[str, Any]) -> dict[str, Any]:
    """Full compliance only for supported positive economics. READY_TO_BID stays strict."""
    if not stage5.get("verified_positive"):
        return {
            "stage": 6,
            "pass": False,
            "ready_to_bid": False,
            "reason": "no_verified_positive_economics",
            "prebid_compliance": None,
        }
    enriched = dict(row)
    if stage5.get("joined", {}).get("history_unit") is not None:
        enriched["historical_award_unit_price"] = stage5["joined"]["history_unit"]
    if stage5.get("joined", {}).get("current_unit") is not None:
        enriched["public_retail_unit_price"] = stage5["joined"]["current_unit"]
    prebid = evaluate_prebid_compliance(enriched)
    ready = bool(prebid.get("ready_to_bid")) and bool(prebid.get("prebid_package_fully_reviewed"))
    # Hard rule: unresolved mandatory → not READY_TO_BID
    if prebid.get("unresolved_mandatory_requirements"):
        ready = False
    return {
        "stage": 6,
        "pass": ready,
        "ready_to_bid": ready,
        "prebid_compliance": prebid,
        "queue": QUEUE_BID_CANDIDATE if ready else QUEUE_PROFITABLE_COMPLIANCE,
        "reason": "strict_final_gate",
    }


# ---------------------------------------------------------------------------
# Full cheap pipeline (0→3) for one row
# ---------------------------------------------------------------------------
def run_progressive_stages_cheap(
    row: dict[str, Any],
    *,
    memory: dict[str, Any] | None = None,
    recurring_boost: float = 0.0,
) -> dict[str, Any]:
    ledger = ResearchCostLedger(stage=0)
    s0 = stage0_broad_discovery(row)
    if not s0.get("pass"):
        return {"reached_stage": 0, "drop_stage": 0, "stage0": s0, "cost": ledger.to_dict()}

    ledger.stage = 1
    ledger.charge(units=1, kind="request", note="triage")
    s1 = stage1_cheap_triage(row, stage0=s0)
    if not s1.get("pass"):
        return {"reached_stage": 1, "drop_stage": 1, "stage0": s0, "stage1": s1, "cost": ledger.to_dict()}

    ledger.stage = 2
    ledger.charge(units=2, kind="request", note="identity")
    s2 = stage2_identity_anchor(row)
    if not s2.get("pass"):
        return {
            "reached_stage": 2,
            "drop_stage": 2,
            "stage0": s0,
            "stage1": s1,
            "stage2": s2,
            "cost": ledger.to_dict(),
        }

    key = product_memory_key(s2.get("identity") or {}, s2.get("commercial") or {})
    mem_hit = recall_product(memory or {}, key) if memory is not None else None

    ledger.stage = 3
    s3 = stage3_economic_recon(
        row,
        stage2=s2,
        recurring_boost=recurring_boost,
        memory=mem_hit,
        ledger=ledger,
    )
    return {
        "reached_stage": 3 if s3.get("pass") else 3,
        "drop_stage": None if s3.get("pass") else 3,
        "stage0": s0,
        "stage1": s1,
        "stage2": {k: v for k, v in s2.items() if k not in {"screen"}},  # keep screen separate
        "stage2_screen": s2.get("screen"),
        "stage3": s3,
        "product_key": key,
        "memory_hit": mem_hit,
        "cost": ledger.to_dict(),
        "survives_to_stage3": bool(s3.get("pass")),
        "deep_research_priority": s3.get("deep_research_priority"),
    }
