"""Phase L.11 — ProcurementBuyerRegistry + coverage reports + AUTH_HISTORY_GAP_QUEUE."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.platform_history import detect_platform

BUILD = "20260928-m3-phase-l11-exact-award-history-recovery"
ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "data" / "phase_l11_buyer_registry.json"
AUTH_GAP_PATH = ROOT / "artifacts" / "phase_l" / "l11_auth_history_gap_queue.json"

BUYER_TYPES = (
    "state",
    "city",
    "county",
    "school_district",
    "university",
    "utility",
    "transit",
    "airport",
    "cooperative",
    "federal",
    "unknown",
)

CATEGORIES = (
    "IT",
    "fleet",
    "equipment",
    "MRO",
    "tools",
    "office_facility",
    "lab_test",
    "safety",
    "electronics",
    "other_commercial",
    "specialty",
)


def _utc() -> str:
    return now_utc().isoformat()


def _load(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"buyers": {}, "build": BUILD}
    return {"buyers": {}, "build": BUILD}


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _utc()
    data["build"] = BUILD
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def infer_buyer_type(row: dict[str, Any]) -> str:
    j = str(row.get("jurisdiction") or "").upper()
    blob = f"{row.get('agency') or ''} {row.get('buyer') or ''}".lower()
    if j == "FEDERAL" or "dept of" in blob or "department of defense" in blob:
        return "federal"
    if "school" in blob or "isd" in blob or "usd" in blob:
        return "school_district"
    if "university" in blob or "college" in blob:
        return "university"
    if "transit" in blob or "metro" in blob:
        return "transit"
    if "airport" in blob or "aviation" in blob:
        return "airport"
    if "utility" in blob or "water" in blob or "electric" in blob:
        return "utility"
    if "county" in blob:
        return "county"
    if "city" in blob or "town" in blob or "village" in blob:
        return "city"
    if "state" in blob or j == "STATE":
        return "state"
    if "coop" in blob or j == "COOPERATIVE":
        return "cooperative"
    return "unknown"


def infer_state(row: dict[str, Any]) -> str | None:
    if row.get("state_code"):
        return str(row["state_code"]).upper()[:2]
    if row.get("state"):
        return str(row["state"]).upper()[:2]
    sid = str((row.get("raw_ref") or {}).get("source_id") or "")
    m = re.search(r"bidnet_([a-z_]+)$", sid)
    if m:
        # crude: texas → TX via map of common
        name = m.group(1).replace("_", " ")
        _MAP = {
            "texas": "TX",
            "ohio": "OH",
            "massachusetts": "MA",
            "pennsylvania": "PA",
            "georgia": "GA",
            "new york": "NY",
            "louisiana": "LA",
            "maryland": "MD",
            "kansas": "KS",
            "illinois": "IL",
            "colorado": "CO",
            "missouri": "MO",
            "arizona": "AZ",
            "utah": "UT",
            "kentucky": "KY",
            "tennessee": "TN",
            "hawaii": "HI",
            "connecticut": "CT",
            "indiana": "IN",
            "oregon": "OR",
            "arkansas": "AR",
            "nevada": "NV",
            "idaho": "ID",
            "florida": "FL",
            "california": "CA",
            "michigan": "MI",
            "wisconsin": "WI",
            "minnesota": "MN",
            "washington": "WA",
            "virginia": "VA",
            "north carolina": "NC",
            "south carolina": "SC",
            "alabama": "AL",
            "alaska": "AK",
            "oklahoma": "OK",
            "new jersey": "NJ",
            "new mexico": "NM",
            "iowa": "IA",
            "nebraska": "NE",
            "montana": "MT",
            "wyoming": "WY",
            "vermont": "VT",
            "maine": "ME",
            "rhode island": "RI",
            "delaware": "DE",
            "west virginia": "WV",
            "north dakota": "ND",
            "south dakota": "SD",
            "mississippi": "MS",
            "new hampshire": "NH",
        }
        return _MAP.get(name)
    return None


def infer_category(row: dict[str, Any]) -> str:
    t = str(row.get("title") or "").lower()
    if any(x in t for x in ("server", "laptop", "cisco", "dell", "network", "computer", "chromebook")):
        return "IT"
    if any(x in t for x in ("vehicle", "truck", "suv", "fleet", "pickup", "bus", "ambulance", "f-150", "f150", "durango", "explorer")):
        return "fleet"
    if any(x in t for x in ("excavator", "loader", "bobcat", "forklift", "tractor", "mower")):
        return "equipment"
    if any(x in t for x in ("parts", "lubricant", "filter", "bearing", "nsn", "valve")):
        return "MRO"
    if any(x in t for x in ("tool", "wrench", "drill")):
        return "tools"
    if any(x in t for x in ("furniture", "office", "facility", "janitor")):
        return "office_facility"
    if any(x in t for x in ("lab", "spectrometer", "microscope", "test equipment")):
        return "lab_test"
    if any(x in t for x in ("safety", "ppe", "helmet", "respirator")):
        return "safety"
    if any(x in t for x in ("radio", "camera", "electronics", "monitor")):
        return "electronics"
    if "nsn" in t or "circuit card" in t:
        return "specialty"
    return "other_commercial"


def upsert_buyer(row: dict[str, Any], *, auth_requirements: str | None = None) -> dict[str, Any]:
    data = _load(REGISTRY_PATH)
    buyers = data.setdefault("buyers", {})
    name = str(row.get("agency") or row.get("buyer") or "UNKNOWN")[:120]
    key = name.upper()
    rec = buyers.setdefault(
        key,
        {
            "buyer": name,
            "state": None,
            "buyer_type": "unknown",
            "platform": None,
            "procurement_url": None,
            "history_url": None,
            "award_url": None,
            "auth_requirements": None,
            "source_health": "unknown",
            "commercial_yield": 0,
            "opportunity_count": 0,
            "last_checked": None,
        },
    )
    rec["state"] = infer_state(row) or rec.get("state")
    rec["buyer_type"] = infer_buyer_type(row) or rec.get("buyer_type")
    rec["platform"] = detect_platform(row) or rec.get("platform")
    if auth_requirements:
        rec["auth_requirements"] = auth_requirements
    rec["opportunity_count"] = int(rec.get("opportunity_count") or 0) + 1
    if infer_category(row) in {"IT", "fleet", "equipment", "MRO", "electronics"}:
        rec["commercial_yield"] = int(rec.get("commercial_yield") or 0) + 1
    rec["last_checked"] = _utc()
    _save(REGISTRY_PATH, data)
    return rec


def build_registry_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    for r in rows:
        upsert_buyer(r)
    return _load(REGISTRY_PATH)


def state_coverage_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_state: dict[str, dict[str, Any]] = {}
    for r in rows:
        st = infer_state(r) or "UNK"
        b = by_state.setdefault(
            st,
            {"buyers": set(), "live": 0, "commercial": 0, "award_history": 0, "platforms": Counter()},
        )
        b["buyers"].add(str(r.get("agency") or "")[:80])
        b["live"] += 1
        cat = infer_category(r)
        if cat not in {"specialty"}:
            b["commercial"] += 1
        if r.get("historical_award_unit_price") or r.get("historical_award_price"):
            b["award_history"] += 1
        b["platforms"][detect_platform(r)] += 1
    out = {}
    for st, b in sorted(by_state.items()):
        out[st] = {
            "buyers_known": len(b["buyers"]),
            "live_opportunities": b["live"],
            "commercial_opportunities": b["commercial"],
            "award_history_capability": b["award_history"],
            "platforms": dict(b["platforms"]),
            "source_health": "mixed",
        }
    return {
        "kind": "StateCoverageReport",
        "build": BUILD,
        "states": out,
        "state_count": len(out),
        "claims_50_state_coverage": False,
    }


def buyer_type_coverage_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_t: dict[str, dict[str, Any]] = {}
    for r in rows:
        t = infer_buyer_type(r)
        b = by_t.setdefault(t, {"buyers": set(), "opps": 0, "history": 0})
        b["buyers"].add(str(r.get("agency") or "")[:80])
        b["opps"] += 1
        if r.get("historical_award_unit_price"):
            b["history"] += 1
    return {
        "kind": "BuyerTypeCoverageReport",
        "build": BUILD,
        "types": {
            t: {
                "buyers": len(v["buyers"]),
                "current_opportunities": v["opps"],
                "historical_evidence_access": v["history"],
            }
            for t, v in sorted(by_t.items())
        },
    }


def category_coverage_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    c = Counter(infer_category(r) for r in rows)
    weak = [k for k in CATEGORIES if c.get(k, 0) < 5]
    return {
        "kind": "CategoryCoverageReport",
        "build": BUILD,
        "counts": dict(c),
        "weak_categories": weak,
    }


def build_auth_history_gap_queue(
    rows: list[dict[str, Any]],
    *,
    audits: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Rank rows that could become quote-worthy if award evidence unlocked."""
    audits = audits or []
    by_id = {a.get("opportunity_id"): a for a in audits}
    queue: list[dict[str, Any]] = []
    for r in rows:
        oid = str(r.get("notice_id") or r.get("solicitation_id") or r.get("id") or "")
        a = by_id.get(oid) or {}
        if a.get("outcome") not in {
            "HISTORY_AUTH_REQUIRED",
            None,
        } and a.get("auth_class") is None:
            # still include recon D with commercial identity
            if a.get("grade_after") not in {None, "GOV_VALUE_D"}:
                continue
        profit = float(a.get("apparent_profit") or r.get("expected_net_profit") or 0) or 0
        identity = 1 if (a.get("commercial") or {}).get("model") or r.get("nsn") else 0
        supplier = {"SUPPLIER_A": 3, "SUPPLIER_B": 2, "SUPPLIER_C": 1}.get(str(a.get("supplier_grade") or ""), 0)
        deadline = float(a.get("deadline_days") or r.get("runway_days") or 0) or 0
        score = profit / 1000.0 + identity * 10 + supplier * 5 + min(deadline, 30) * 0.2
        queue.append(
            {
                "opportunity_id": oid,
                "buyer": r.get("agency"),
                "platform": detect_platform(r),
                "product": (r.get("title") or "")[:120],
                "auth_class": a.get("auth_class") or "UNKNOWN_AUTH",
                "score": round(score, 2),
                "apparent_profit": profit,
                "exact_product_identity": bool(identity),
                "supplier_quality": a.get("supplier_grade"),
                "deadline_days": deadline,
                "registration_intel": a.get("registration_intel"),
                "create_account": False,
            }
        )
    queue.sort(key=lambda x: -x["score"])
    AUTH_GAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUTH_GAP_PATH.write_text(json.dumps({"kind": "AUTH_HISTORY_GAP_QUEUE", "build": BUILD, "rows": queue}, indent=2), encoding="utf-8")
    return queue
