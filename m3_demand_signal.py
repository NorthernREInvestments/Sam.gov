"""BUILD 6 — Demand Signal intelligence layer.

Unifies historical demand, early notices, watchlist interest, and lifecycle
signals into reusable DemandSignal objects. Does not run discovery, change
scoring, or invent products.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from application_clock import now_utc
from federal_dla_constants import NOTICE_MARKET_RESEARCH, NOTICE_UPCOMING_PROCUREMENT

BUILD_TAG = "20260918-m3-demand-signal-1"
DEMAND_SIGNAL_INDEX_KEY = "m3_demand_signal_index_v1"

# Signal types
RECOMPETE = "RECOMPETE"
EXPIRATION = "EXPIRATION"
SOURCES_SOUGHT = "SOURCES_SOUGHT"
FORECAST = "FORECAST"
HISTORICAL_PATTERN = "HISTORICAL_PATTERN"
WATCHLIST = "WATCHLIST"

SIGNAL_TYPES = (RECOMPETE, EXPIRATION, SOURCES_SOUGHT, FORECAST, HISTORICAL_PATTERN, WATCHLIST)

# States
UNKNOWN = "UNKNOWN"
DETECTED = "DETECTED"
VALIDATED = "VALIDATED"
ACTIVE = "ACTIVE"
CONVERTED_TO_OPPORTUNITY = "CONVERTED_TO_OPPORTUNITY"
EXPIRED = "EXPIRED"

_RECOMPETE_RE = re.compile(r"\b(recompete|re-compete|follow[- ]on|option\s+year|renewal)\b", re.I)
_FORECAST_RE = re.compile(r"\b(procurement\s+forecast|acquisition\s+forecast|forecast)\b", re.I)
_SOURCES_RE = re.compile(r"\b(sources\s+sought|request\s+for\s+information|\brfi\b|market\s+research)\b", re.I)


def _utc() -> str:
    return now_utc().isoformat()


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _num(v: Any) -> float | None:
    if v is None or v == "" or str(v).upper() == "UNKNOWN":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_date(v: Any) -> datetime | None:
    if v is None or v == "" or str(v).upper() == "UNKNOWN":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            if s.endswith("Z"):
                s2 = s[:-1] + "+00:00"
                return datetime.fromisoformat(s2)
            if "T" in s:
                return datetime.fromisoformat(s)
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def empty_signal(**overrides: Any) -> dict[str, Any]:
    base = {
        "kind": "M3DemandSignal",
        "build": BUILD_TAG,
        "signal_id": None,
        "signal_type": UNKNOWN,
        "related_product": None,
        "product_id": None,
        "product_dedupe_key": None,
        "related_agency": None,
        "related_buyer": None,
        "source": None,
        "evidence": [],
        "confidence": UNKNOWN,
        "expected_timing": UNKNOWN,
        "status": UNKNOWN,
        "monitoring_recipe": None,
        "opportunity_id": None,
        "created_at": _utc(),
        "updated_at": _utc(),
    }
    base.update(overrides)
    return base


def signal_id_for(*, signal_type: str, key_parts: list[Any]) -> str:
    bits = [signal_type] + [str(p).strip() for p in key_parts if p not in (None, "", "UNKNOWN")]
    return "|".join(bits)[:240]


def _evidence(source: Any, snippet: Any = None, confidence: Any = None, field: str | None = None) -> dict[str, Any]:
    return {
        "field": field,
        "source": source or "UNKNOWN",
        "snippet": snippet,
        "confidence": confidence or UNKNOWN,
    }


def _monitoring_recipe(signal_type: str, *, agency: str | None, product: Any) -> dict[str, Any]:
    """Planner preparation only — does not execute search."""
    agency_l = (agency or "UNKNOWN").upper()
    product_hint = None
    if isinstance(product, dict):
        product_hint = product.get("nsn") or product.get("part_number") or product.get("title")
    elif product:
        product_hint = str(product)
    sources = ["fed_sam_contract_opportunities"]
    if "DLA" in agency_l:
        sources.append("dla_via_sam_spe_spr")
    if "DHS" in agency_l or "HOMELAND" in agency_l:
        sources.append("fed_sam_contract_opportunities")
    return {
        "kind": "M3DiscoveryRecipeDraft",
        "automate_search": False,
        "signal_type": signal_type,
        "monitor_sources": sources,
        "agency_filter": agency or "UNKNOWN",
        "product_hint": product_hint or "UNKNOWN",
        "operator_action": f"Monitor {', '.join(sources)} for {signal_type} related to {product_hint or agency or 'product'}",
        "note": "BUILD 6 prepares recipes only — discovery engine unchanged",
    }


def _product_ref_from_row(row: dict[str, Any]) -> dict[str, Any]:
    proj = row.get("award_product_projection") if isinstance(row.get("award_product_projection"), dict) else {}
    fact = row.get("product_fact_projection") if isinstance(row.get("product_fact_projection"), dict) else {}
    struct = row.get("dla_product_structure") if isinstance(row.get("dla_product_structure"), dict) else {}
    nsn = struct.get("nsn")
    if isinstance((struct.get("fields") or {}).get("nsn"), dict):
        nsn = nsn or (struct["fields"]["nsn"].get("value"))
    pid = (
        row.get("knowledge_product_id")
        or proj.get("knowledge_product_id")
        or (proj.get("knowledge_product_ids") or [None])[0]
        or fact.get("knowledge_product_id")
    )
    dedupe = None
    if nsn:
        dedupe = f"nsn:{str(nsn).upper()}"
    return {
        "product_id": pid,
        "product_dedupe_key": dedupe,
        "nsn": nsn,
        "part_number": struct.get("part_number"),
        "title": row.get("title"),
    }


def _confidence_rank(c: str) -> int:
    return {"VALIDATED": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}.get(str(c).upper(), 0)


def _status_from_confidence(conf: str, *, weak: bool = False) -> str:
    c = str(conf or "").upper()
    if weak or c in {"", "UNKNOWN", "LOW"}:
        return DETECTED if c == "LOW" else UNKNOWN
    if c in {"HIGH", "VALIDATED"}:
        return VALIDATED
    if c == "MEDIUM":
        return DETECTED
    return DETECTED


# ---------------------------------------------------------------------------
# Signal builders (read existing data only)
# ---------------------------------------------------------------------------

def signal_from_historical_pattern(row: dict[str, Any]) -> dict[str, Any] | None:
    """Repeated / projected award demand → HISTORICAL_PATTERN."""
    proj = row.get("award_product_projection") if isinstance(row.get("award_product_projection"), dict) else {}
    demands = list(proj.get("demand_evidence") or [])
    awards = list(row.get("historical_awards") or row.get("award_history") or [])
    buying = list(proj.get("buying_agencies") or [])

    count = len(demands) if demands else len(awards)
    # Need evidence of recurrence or at least one validated demand projection
    if count >= 2:
        conf = "HIGH"
        status = VALIDATED
        reason = f"{count} historical purchase evidences"
    elif demands and str((demands[0] or {}).get("confidence") or "").upper() in {"HIGH", "VALIDATED"}:
        conf = "HIGH"
        status = VALIDATED
        reason = "Validated award/product demand projection"
        count = 1
    elif _num(row.get("historical_award_amount")) is not None and (
        (row.get("dla_product_structure") or {}).get("has_exact_nsn")
        or (row.get("product_identity") or {}).get("identity_state") == "EXACT_NSN"
    ):
        conf = "MEDIUM"
        status = DETECTED
        reason = "Single historical award amount with exact product identity"
        count = 1
    else:
        return None

    product = _product_ref_from_row(row)
    agency = buying[0] if buying else (row.get("agency") or row.get("buyer"))
    # Timing: months since latest award date if known
    dates = []
    for d in demands:
        if isinstance(d, dict):
            dt = _parse_date(d.get("date"))
            if dt:
                dates.append(dt)
    for a in awards:
        if isinstance(a, dict):
            dt = _parse_date(a.get("award_date") or a.get("Start Date") or a.get("date"))
            if dt:
                dates.append(dt)
    timing = UNKNOWN
    if dates:
        latest = max(dates)
        months = max(0, int((now_utc() - latest).days / 30))
        timing = f"last_award_approx_{months}_months_ago"
        if months >= 8:
            conf = "HIGH" if conf != "MEDIUM" else conf
            reason = f"{reason}; recompete window may be approaching ({months} mo)"

    sid = signal_id_for(
        signal_type=HISTORICAL_PATTERN,
        key_parts=[product.get("product_dedupe_key") or product.get("nsn") or row.get("canonical_id"), agency],
    )
    return empty_signal(
        signal_id=sid,
        signal_type=HISTORICAL_PATTERN,
        related_product=product,
        product_id=product.get("product_id"),
        product_dedupe_key=product.get("product_dedupe_key"),
        related_agency=agency,
        related_buyer=row.get("buyer") or agency,
        source="award_product_projection|historical_awards",
        evidence=[
            _evidence("historical_pattern", reason, conf, field="demand_count"),
            _evidence("purchase_count", str(count), conf, field="count"),
        ],
        confidence=conf,
        expected_timing=timing,
        status=status,
        monitoring_recipe=_monitoring_recipe(HISTORICAL_PATTERN, agency=agency, product=product),
        opportunity_id=row.get("canonical_id"),
    )


def signal_from_sources_sought(row: dict[str, Any]) -> dict[str, Any] | None:
    sem = str(
        row.get("notice_semantic_class")
        or (row.get("notice_type_intel") or {}).get("notice_semantic_class")
        or (row.get("raw_metadata") or {}).get("notice_semantic_class")
        or ""
    ).upper()
    title = str(row.get("title") or "")
    notice_type = str(row.get("notice_type") or row.get("type") or "")
    blob = f"{title} {notice_type} {sem}".lower()
    is_ss = (
        sem == NOTICE_MARKET_RESEARCH
        or bool(_SOURCES_RE.search(blob))
        or str(row.get("notice_type_code") or "").lower() in {"r", "s"}
        and "source" in blob
    )
    if not is_ss and "sources sought" not in blob and "rfi" not in blob:
        return None

    # Weak title-only without semantic class → DETECTED not VALIDATED
    if sem == NOTICE_MARKET_RESEARCH:
        conf, status = "HIGH", VALIDATED
    elif _SOURCES_RE.search(title):
        conf, status = "HIGH", VALIDATED
    else:
        conf, status = "MEDIUM", DETECTED

    product = _product_ref_from_row(row)
    agency = row.get("agency") or row.get("buyer")
    sid = signal_id_for(
        signal_type=SOURCES_SOUGHT,
        key_parts=[row.get("notice_id") or row.get("canonical_id"), agency],
    )
    return empty_signal(
        signal_id=sid,
        signal_type=SOURCES_SOUGHT,
        related_product=product,
        product_id=product.get("product_id"),
        product_dedupe_key=product.get("product_dedupe_key"),
        related_agency=agency,
        related_buyer=row.get("buyer") or agency,
        source="notice_semantic_class|title",
        evidence=[
            _evidence("notice_semantic_class", sem or notice_type or title[:80], conf, field="notice_type"),
        ],
        confidence=conf,
        expected_timing="pre_solicitation_window",
        status=status,
        monitoring_recipe=_monitoring_recipe(SOURCES_SOUGHT, agency=agency, product=product),
        opportunity_id=row.get("canonical_id"),
    )


def signal_from_forecast(row: dict[str, Any]) -> dict[str, Any] | None:
    notice_type = str(row.get("notice_type") or "").upper()
    title = str(row.get("title") or "")
    source_id = str(row.get("source_id") or "")
    lead_only = bool(row.get("lead_only") or (row.get("raw_metadata") or {}).get("lead_only"))
    if not (
        notice_type == "FORECAST"
        or lead_only
        or "forecast" in source_id.lower()
        or _FORECAST_RE.search(title)
    ):
        return None
    # Forecast without agency/product → weak
    agency = row.get("agency") or row.get("buyer")
    product = _product_ref_from_row(row)
    if not agency and not product.get("nsn"):
        conf, status = "LOW", UNKNOWN
    else:
        conf, status = ("HIGH", VALIDATED) if notice_type == "FORECAST" or lead_only else ("MEDIUM", DETECTED)
    if status == UNKNOWN:
        return empty_signal(
            signal_id=signal_id_for(signal_type=FORECAST, key_parts=[row.get("canonical_id")]),
            signal_type=FORECAST,
            related_product=product,
            related_agency=agency,
            source=source_id or "forecast_notice",
            evidence=[_evidence("forecast", title[:120] or "weak_forecast", "LOW")],
            confidence="LOW",
            expected_timing=UNKNOWN,
            status=UNKNOWN,
            opportunity_id=row.get("canonical_id"),
            monitoring_recipe=_monitoring_recipe(FORECAST, agency=agency, product=product),
        )
    sid = signal_id_for(signal_type=FORECAST, key_parts=[row.get("canonical_id"), agency])
    return empty_signal(
        signal_id=sid,
        signal_type=FORECAST,
        related_product=product,
        product_id=product.get("product_id"),
        product_dedupe_key=product.get("product_dedupe_key"),
        related_agency=agency,
        related_buyer=row.get("buyer") or agency,
        source=source_id or "forecast_notice",
        evidence=[_evidence("forecast", title[:120] or notice_type, conf)],
        confidence=conf,
        expected_timing="agency_forecast_horizon",
        status=status,
        monitoring_recipe=_monitoring_recipe(FORECAST, agency=agency, product=product),
        opportunity_id=row.get("canonical_id"),
    )


def signal_from_expiration_or_recompete(row: dict[str, Any]) -> list[dict[str, Any]]:
    """PoP end / option year / recompete language → EXPIRATION and/or RECOMPETE."""
    out: list[dict[str, Any]] = []
    product = _product_ref_from_row(row)
    agency = row.get("agency") or row.get("buyer")
    title = str(row.get("title") or "")
    pop_end = (
        row.get("period_of_performance_end")
        or row.get("pop_end")
        or row.get("ordering_period_end_date")
        or (row.get("performance") or {}).get("period_of_performance_end")
    )
    options = row.get("option_years_remaining")
    if options is None:
        options = (row.get("performance") or {}).get("option_years_remaining")

    end_dt = _parse_date(pop_end)
    now = now_utc()
    if end_dt:
        days = (end_dt - now).days
        if days < -90:
            # Long expired — mark EXPIRED expiration signal only if product known
            if product.get("nsn") or product.get("product_id"):
                out.append(
                    empty_signal(
                        signal_id=signal_id_for(signal_type=EXPIRATION, key_parts=[row.get("canonical_id"), pop_end]),
                        signal_type=EXPIRATION,
                        related_product=product,
                        product_id=product.get("product_id"),
                        product_dedupe_key=product.get("product_dedupe_key"),
                        related_agency=agency,
                        source="period_of_performance_end",
                        evidence=[_evidence("pop_end", str(pop_end), "HIGH", field="expiration")],
                        confidence="HIGH",
                        expected_timing="expired",
                        status=EXPIRED,
                        monitoring_recipe=_monitoring_recipe(EXPIRATION, agency=agency, product=product),
                        opportunity_id=row.get("canonical_id"),
                    )
                )
        elif days <= 365:
            conf = "HIGH" if days <= 180 else "MEDIUM"
            status = VALIDATED if conf == "HIGH" else DETECTED
            timing = f"ends_in_{max(0, days)}_days"
            out.append(
                empty_signal(
                    signal_id=signal_id_for(signal_type=EXPIRATION, key_parts=[row.get("canonical_id"), pop_end]),
                    signal_type=EXPIRATION,
                    related_product=product,
                    product_id=product.get("product_id"),
                    product_dedupe_key=product.get("product_dedupe_key"),
                    related_agency=agency,
                    source="period_of_performance_end",
                    evidence=[_evidence("pop_end", str(pop_end), conf, field="expiration")],
                    confidence=conf,
                    expected_timing=timing,
                    status=status,
                    monitoring_recipe=_monitoring_recipe(EXPIRATION, agency=agency, product=product),
                    opportunity_id=row.get("canonical_id"),
                )
            )
            # Near end → also RECOMPETE
            if days <= 180:
                out.append(
                    empty_signal(
                        signal_id=signal_id_for(signal_type=RECOMPETE, key_parts=[row.get("canonical_id"), "pop"]),
                        signal_type=RECOMPETE,
                        related_product=product,
                        product_id=product.get("product_id"),
                        product_dedupe_key=product.get("product_dedupe_key"),
                        related_agency=agency,
                        source="period_of_performance_end",
                        evidence=[
                            _evidence("recompete_window", f"PoP ends {pop_end}", conf, field="recompete"),
                        ],
                        confidence=conf,
                        expected_timing=timing,
                        status=status,
                        monitoring_recipe=_monitoring_recipe(RECOMPETE, agency=agency, product=product),
                        opportunity_id=row.get("canonical_id"),
                    )
                )

    if options is not None:
        try:
            oy = int(options)
        except (TypeError, ValueError):
            oy = None
        if oy is not None and oy == 0:
            out.append(
                empty_signal(
                    signal_id=signal_id_for(signal_type=RECOMPETE, key_parts=[row.get("canonical_id"), "options_exhausted"]),
                    signal_type=RECOMPETE,
                    related_product=product,
                    product_id=product.get("product_id"),
                    product_dedupe_key=product.get("product_dedupe_key"),
                    related_agency=agency,
                    source="option_years_remaining",
                    evidence=[_evidence("option_years_remaining", "0", "HIGH", field="recompete")],
                    confidence="HIGH",
                    expected_timing="options_exhausted_recompete_likely",
                    status=VALIDATED,
                    monitoring_recipe=_monitoring_recipe(RECOMPETE, agency=agency, product=product),
                    opportunity_id=row.get("canonical_id"),
                )
            )

    if _RECOMPETE_RE.search(title) or str(row.get("notice_semantic_class") or "") == NOTICE_UPCOMING_PROCUREMENT:
        # Title/semantic recompete without dates → DETECTED unless strong semantic
        conf = "HIGH" if str(row.get("notice_semantic_class") or "") == NOTICE_UPCOMING_PROCUREMENT else "MEDIUM"
        status = VALIDATED if conf == "HIGH" else DETECTED
        out.append(
            empty_signal(
                signal_id=signal_id_for(signal_type=RECOMPETE, key_parts=[row.get("canonical_id"), "title"]),
                signal_type=RECOMPETE,
                related_product=product,
                product_id=product.get("product_id"),
                product_dedupe_key=product.get("product_dedupe_key"),
                related_agency=agency,
                source="title|notice_semantic_class",
                evidence=[_evidence("recompete_language", title[:120], conf)],
                confidence=conf,
                expected_timing="upcoming_procurement" if conf == "HIGH" else UNKNOWN,
                status=status,
                monitoring_recipe=_monitoring_recipe(RECOMPETE, agency=agency, product=product),
                opportunity_id=row.get("canonical_id"),
            )
        )
    return out


def signal_from_watchlist(row: dict[str, Any], *, target: Any | None = None) -> dict[str, Any] | None:
    """Operator / GovSpend watchlist interest → WATCHLIST signal."""
    meta = row.get("govspend_watchlist") or row.get("watchlist_match")
    if isinstance(meta, dict) and (meta.get("award_id") or meta.get("matched_watchlist_id") or meta.get("watchlist_id")):
        agency = meta.get("agency") or meta.get("contracting_office") or row.get("agency")
        conf = "HIGH" if meta.get("award_id") or meta.get("matched_watchlist_id") else "MEDIUM"
        product = _product_ref_from_row(row)
        if not product.get("title"):
            product["title"] = meta.get("contract_name") or row.get("title")
        sid = signal_id_for(
            signal_type=WATCHLIST,
            key_parts=[meta.get("matched_watchlist_id") or meta.get("watchlist_id") or meta.get("award_id"), agency],
        )
        return empty_signal(
            signal_id=sid,
            signal_type=WATCHLIST,
            related_product=product,
            product_id=product.get("product_id"),
            product_dedupe_key=product.get("product_dedupe_key"),
            related_agency=agency,
            related_buyer=meta.get("contracting_office") or agency,
            source="govspend_watchlist",
            evidence=[
                _evidence(
                    "watchlist",
                    meta.get("contract_name") or meta.get("award_id") or "watchlist_hit",
                    conf,
                    field="watchlist",
                )
            ],
            confidence=conf,
            expected_timing="recompete_or_similar_posting",
            status=VALIDATED if conf == "HIGH" else DETECTED,
            monitoring_recipe=_monitoring_recipe(WATCHLIST, agency=agency, product=product),
            opportunity_id=row.get("canonical_id"),
        )

    if target is not None:
        # dataclass or dict target without row match — still a signal
        t = target
        agency = getattr(t, "agency", None) or (t.get("agency") if isinstance(t, dict) else None)
        award_id = getattr(t, "award_id", None) or (t.get("award_id") if isinstance(t, dict) else None)
        name = getattr(t, "contract_name", None) or (t.get("contract_name") if isinstance(t, dict) else None)
        tid = getattr(t, "id", None) or (t.get("id") if isinstance(t, dict) else None)
        if not (award_id or tid or name):
            return None
        conf = "HIGH" if award_id else "MEDIUM"
        product = {"title": name, "product_id": None, "product_dedupe_key": None, "nsn": None}
        return empty_signal(
            signal_id=signal_id_for(signal_type=WATCHLIST, key_parts=[tid or award_id, agency]),
            signal_type=WATCHLIST,
            related_product=product,
            related_agency=agency,
            related_buyer=getattr(t, "contracting_office", None) or agency,
            source="gs_watchlist_target",
            evidence=[_evidence("watchlist_target", name or award_id, conf)],
            confidence=conf,
            expected_timing="operator_defined_interest",
            status=VALIDATED if conf == "HIGH" else DETECTED,
            monitoring_recipe=_monitoring_recipe(WATCHLIST, agency=agency, product=product),
            opportunity_id=None,
        )

    if row.get("watchlist_hit") or row.get("is_watchlist_match"):
        product = _product_ref_from_row(row)
        agency = row.get("agency")
        return empty_signal(
            signal_id=signal_id_for(signal_type=WATCHLIST, key_parts=[row.get("canonical_id")]),
            signal_type=WATCHLIST,
            related_product=product,
            related_agency=agency,
            source="watchlist_flag",
            evidence=[_evidence("watchlist_flag", "true", "MEDIUM")],
            confidence="MEDIUM",
            expected_timing="operator_defined_interest",
            status=DETECTED,
            monitoring_recipe=_monitoring_recipe(WATCHLIST, agency=agency, product=product),
            opportunity_id=row.get("canonical_id"),
        )
    return None


def collect_demand_signals_for_row(
    row: dict[str, Any],
    *,
    watchlist_target: Any | None = None,
) -> list[dict[str, Any]]:
    """Collect all demand signals detectable from one pipeline/contract row."""
    signals: list[dict[str, Any]] = []
    for builder in (
        signal_from_historical_pattern,
        signal_from_sources_sought,
        signal_from_forecast,
    ):
        s = builder(row)
        if s:
            signals.append(s)
    signals.extend(signal_from_expiration_or_recompete(row))
    wl = signal_from_watchlist(row, target=watchlist_target)
    if wl:
        signals.append(wl)

    # Dedupe by signal_id keep highest confidence
    by_id: dict[str, dict[str, Any]] = {}
    for s in signals:
        sid = s.get("signal_id") or signal_id_for(signal_type=str(s.get("signal_type")), key_parts=[id(s)])
        s["signal_id"] = sid
        prev = by_id.get(sid)
        if prev is None or _confidence_rank(s.get("confidence")) > _confidence_rank(prev.get("confidence")):
            by_id[sid] = s
    return list(by_id.values())


def load_demand_signal_index() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == DEMAND_SIGNAL_INDEX_KEY).one_or_none()
            if row and row.value:
                data = json.loads(row.value)
                if isinstance(data, dict):
                    data.setdefault("by_signal_id", {})
                    data.setdefault("by_product", {})
                    data.setdefault("by_opportunity", {})
                    return data
        finally:
            db.close()
    except Exception:
        pass
    return {
        "kind": "M3DemandSignalIndex",
        "by_signal_id": {},
        "by_product": {},
        "by_opportunity": {},
        "build": BUILD_TAG,
    }


def save_demand_signal_index(index: dict[str, Any]) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        index = dict(index)
        index["updated_at"] = _utc()
        index["build"] = BUILD_TAG
        raw = json.dumps(index, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == DEMAND_SIGNAL_INDEX_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=DEMAND_SIGNAL_INDEX_KEY, value=raw))
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        return False


def upsert_signals_into_index(
    signals: list[dict[str, Any]],
    *,
    index: dict[str, Any] | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    index = index if index is not None else {"by_signal_id": {}, "by_product": {}, "by_opportunity": {}}
    by = index.setdefault("by_signal_id", {})
    by_prod = index.setdefault("by_product", {})
    by_opp = index.setdefault("by_opportunity", {})
    for s in signals:
        sid = s.get("signal_id")
        if not sid:
            continue
        # Do not upgrade VALIDATED → UNKNOWN; preserve stronger stored status unless new is stronger
        prev = by.get(sid)
        if prev and _confidence_rank(prev.get("confidence")) > _confidence_rank(s.get("confidence")):
            continue
        stored = deepcopy(s)
        stored["updated_at"] = _utc()
        if prev and prev.get("created_at"):
            stored["created_at"] = prev["created_at"]
        by[sid] = stored
        pk = s.get("product_dedupe_key") or (f"pid:{s['product_id']}" if s.get("product_id") else None)
        if pk:
            by_prod.setdefault(pk, [])
            if sid not in by_prod[pk]:
                by_prod[pk].append(sid)
        oid = s.get("opportunity_id")
        if oid:
            by_opp.setdefault(str(oid), [])
            if sid not in by_opp[str(oid)]:
                by_opp[str(oid)].append(sid)
    if persist:
        save_demand_signal_index(index)
    return index


def build_demand_signals(
    rows: list[dict[str, Any]] | None = None,
    *,
    store: Any | None = None,
    watchlist_targets: list[Any] | None = None,
    persist: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    """
    Build unified demand signal view.

    Does not execute discovery. Prepares monitoring recipes for later planner.
    """
    if rows is None:
        if store is None:
            from m3_pipeline_store import M3PipelineStore

            store = M3PipelineStore()
        rows = list(store.all()) if hasattr(store, "all") else []

    index = load_demand_signal_index() if persist else {"by_signal_id": {}, "by_product": {}, "by_opportunity": {}}
    all_signals: list[dict[str, Any]] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        sigs = collect_demand_signals_for_row(row)
        all_signals.extend(sigs)

    if watchlist_targets:
        for t in watchlist_targets:
            s = signal_from_watchlist({}, target=t)
            if s:
                all_signals.append(s)

    upsert_signals_into_index(all_signals, index=index, persist=persist)

    # Prefer index values (deduped)
    signals = list((index.get("by_signal_id") or {}).values())
    # Sort: VALIDATED/ACTIVE first, then by type priority
    type_rank = {
        RECOMPETE: 6,
        EXPIRATION: 5,
        SOURCES_SOUGHT: 4,
        HISTORICAL_PATTERN: 3,
        WATCHLIST: 2,
        FORECAST: 1,
    }
    status_rank = {
        ACTIVE: 5,
        VALIDATED: 4,
        DETECTED: 3,
        CONVERTED_TO_OPPORTUNITY: 2,
        UNKNOWN: 1,
        EXPIRED: 0,
    }
    signals.sort(
        key=lambda s: (
            -status_rank.get(str(s.get("status")), 0),
            -type_rank.get(str(s.get("signal_type")), 0),
            -_confidence_rank(str(s.get("confidence"))),
            str(s.get("signal_id") or ""),
        )
    )
    limited = signals[: max(1, min(500, int(limit or 200)))]

    # Products/categories that deserve monitoring
    monitor: list[dict[str, Any]] = []
    seen_prod: set[str] = set()
    for s in limited:
        if str(s.get("status")) in {UNKNOWN, EXPIRED}:
            continue
        if str(s.get("confidence")).upper() == "LOW":
            continue
        prod = s.get("related_product") if isinstance(s.get("related_product"), dict) else {}
        key = s.get("product_dedupe_key") or prod.get("nsn") or prod.get("title") or s.get("related_agency")
        if not key or key in seen_prod:
            continue
        seen_prod.add(str(key))
        monitor.append(
            {
                "product_key": key,
                "product": prod,
                "agency": s.get("related_agency"),
                "signal_types": [s.get("signal_type")],
                "why_monitor": (s.get("evidence") or [{}])[0].get("snippet") if s.get("evidence") else s.get("signal_type"),
                "monitoring_recipe": s.get("monitoring_recipe"),
                "confidence": s.get("confidence"),
                "status": s.get("status"),
            }
        )

    by_type: dict[str, int] = {}
    for s in limited:
        t = str(s.get("signal_type") or "UNKNOWN")
        by_type[t] = by_type.get(t, 0) + 1

    return {
        "kind": "M3DemandSignalBundle",
        "build": BUILD_TAG,
        "question": "Where should we be looking before the solicitation appears?",
        "count": len(limited),
        "by_type": by_type,
        "signals": limited,
        "products_to_monitor": monitor,
        "discovery_automation": False,
        "note": "Intelligence layer only — SAM/DLA/BidNet discovery engines unchanged",
        "generated_at": _utc(),
    }


def annotate_row_demand_signals(row: dict[str, Any]) -> dict[str, Any]:
    """Attach demand_signals annotation without removing other pipeline fields."""
    signals = collect_demand_signals_for_row(row)
    row = dict(row)
    row["demand_signals"] = {
        "build": BUILD_TAG,
        "updated_at": _utc(),
        "count": len(signals),
        "signals": signals,
        "types": sorted({str(s.get("signal_type")) for s in signals}),
    }
    return row
