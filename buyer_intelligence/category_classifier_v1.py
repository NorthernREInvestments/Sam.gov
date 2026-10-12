"""Hardened durable-product category classifier V1.

Evidence hierarchy (strongest first):
1. title/description exclusion (service/construction/food) — vetoes product noise
2. explicit commodity/product code (when present)
3. clear line-item description
4. solicitation/item title
5. buyer/category historical pattern (support only; never overrides contradiction)

Never force OTHER_DURABLE into a named category.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

BUILD = "20261008-m3-three-lane-integration-live-match-v1"

CANONICAL = (
    "AUTO_PARTS",
    "MRO",
    "TOOLS",
    "ELECTRICAL",
    "LIGHTING",
    "PPE",
    "JANITORIAL",
    "OFFICE_FURNITURE",
    "OFFICE_SUPPLIES",
    "COMMON_EQUIPMENT",
    "PLUMBING",
    "HVAC",
    "FASTENERS",
    "HOSES_FITTINGS",
    "BEARINGS",
    "ADHESIVES",
    "SAFETY",
    "OTHER_DURABLE",
)

# Exclude / flag — checked first on title/description
_EXCLUDE: list[tuple[str, re.Pattern[str]]] = [
    ("FOOD", re.compile(r"\b(food|grocery|dairy|milk|meat|produce|beverage|catering|perishable)\b", re.I)),
    ("BEVERAGE", re.compile(r"\b(beverage|soft\s*drink|soda\s+fountain)\b", re.I)),
    ("PERISHABLE", re.compile(r"\b(perishable|fresh\s+produce)\b", re.I)),
    ("SERVICE", re.compile(
        r"\b("
        r"professional\s+services?|consulting|staffing|engineering\s+services?|"
        r"maintenance\s+services?|repair\s+services?|monitoring\s+and\s+repair|"
        r"inspection\s+services?|on-?call\s+\w+\s+services?|electrical\s+services?|"
        r"construction\s+management\s+services?|plan\s+review\s+services?|"
        r"sanitary\s+service|grounds?\s+maintenance(?:\s+service)?|landscape\s+maintenance|"
        r"janitorial\s+services?|custodial\s+services?|"
        r"disposal\s+services?|recycling\s+services?|waste\s+(?:disposal|services?)|"
        r"transport\s+and\s+disposal|leachate\s+transport|"
        r"event\s+coordinator|advisory\s+and\s+consulting|workforce\s+solutions|"
        r"powder\s+coating\s+services?|coating\s+services?|"
        r"generator\s+rental|rental\s+of\s+(?:emergency\s+)?generators?|"
        r"snow\s+removal|deicing\s+services?|recycling\s+services?|"
        r"reforestation|tire\s+recycling"
        r")\b",
        re.I,
    )),
    ("INSTALLATION", re.compile(
        r"\b(installation\s+(?:only|project|services?)|install\s+and\s+commission|"
        r"turnkey\s+install|gate\s+install|upfitting\s+fabrication\s+services|"
        r"wet\s+tap\s+installation|installations?\b)",
        re.I,
    )),
    ("CONSTRUCTION", re.compile(
        r"\b("
        r"construction|asphalt|concrete|paving|excavation|demolition|hfst|"
        r"roadway|pavement|sidewalk|overpass|roundabout|bridge\s+improvements?|"
        r"rehabilitation|renovation|tenant\s+improvement|resurfacing|overlay|"
        r"erosion\s+mitigation|safe\s+routes\s+to\s+school|curb\s+ramp|"
        r"road\s+surface|substation|line\s+extension|cable\s+relocation|"
        r"taxiway|drainage\s+improvements?|tank\s+removal|"
        r"pedestrian\s+improvements?|facility\s+improvements?|"
        r"waterlines?|water\s+mains?|sewer\s+(?:interceptor|rehab|conversion)|"
        r"interceptor\s+lining|cul-?de-?sac|septic\s+to\s+sewer|"
        r"trunk\s+sanitary\s+sewer|traffic\s+management\s+program|"
        r"job\s+order\s+contracts?|gap\s+closure\s+project|parking\s+lot|"
        r"security\s+enhancements?|replacement\s+project"
        r")\b",
        re.I,
    )),
    ("SPECIALTY_EXECUTION", re.compile(
        r"\b(sole\s*source|manufacturer\s+authorization|custom\s+fabricat|"
        r"rifles?|firearms?|weapons?|taser)\b",
        re.I,
    )),
]

# Context-aware product patterns — order matters (more specific first)
_PRODUCT: list[tuple[str, re.Pattern[str], str]] = [
    ("AUTO_PARTS", re.compile(
        r"\b(fleet\s+parts?|transit\s+parts?|inventory\s+parts?|auto(?:motive)?\s+parts?|"
        r"vehicle\s+parts?|ford\s+parts?|brakes?|alternator|starter|spark\s*plugs?|exhaust|manifold|"
        r"oem\s+parts?|bus\s+parts?|ac-?delco|motorcraft)\b",
        re.I,
    ), "HIGH"),
    ("ELECTRICAL", re.compile(
        r"\b(electrical\s+suppl(?:y|ies)|electrical\s+materials?|circuit\s+breakers?|"
        r"conduit|electrical\s+wire|copper\s+wire|switchgear|electrical\s+outlet)\b",
        re.I,
    ), "HIGH"),
    ("JANITORIAL", re.compile(
        r"\b(janitorial\s+suppl(?:y|ies)|cleaning\s+suppl(?:y|ies)|disinfectant|"
        r"trash\s+bags?|mops?|brooms?|toilet\s+paper|paper\s+towels?|custodial\s+suppl)\b",
        re.I,
    ), "HIGH"),
    ("OFFICE_FURNITURE", re.compile(
        r"\b(office\s+furniture|desks?|chairs?|cubicles?|filing\s+cabinets?|workstations?)\b",
        re.I,
    ), "HIGH"),
    ("OFFICE_SUPPLIES", re.compile(
        r"\b(office\s+suppl(?:y|ies)|toners?|printer\s+paper|staplers?|binders?|copy\s+paper|"
        r"ink\s+cartridges?)\b",
        re.I,
    ), "HIGH"),
    ("LIGHTING", re.compile(
        r"\b(lighting\s+suppl|led\s+(?:fixture|bulb|lamp|driver)s?|luminaires?|light\s+fixtures?|"
        r"streetlights?|street\s+light\s+equipment)\b",
        re.I,
    ), "HIGH"),
    ("PPE", re.compile(
        r"\b(ppe|respirators?|hard\s*hats?|safety\s+vests?|goggles?|face\s+shields?|safety\s+gloves?|"
        r"steel[-\s]?toe|composite\s+toe|safety\s+(?:shoes|boots|kits?)|"
        r"lead\s+safety\s+kits?|containment\s+units?)\b",
        re.I,
    ), "HIGH"),
    ("SAFETY", re.compile(
        r"\b(safety\s+suppl|fall\s+protection|lockout|tagout|first\s+aid\s+kits?)\b", re.I
    ), "MEDIUM"),
    ("TOOLS", re.compile(
        r"\b(hand\s+tools?|power\s+tools?|wrenches?|drills?|socket\s+sets?|screwdrivers?|hammers?|pliers)\b",
        re.I,
    ), "HIGH"),
    ("PLUMBING", re.compile(
        r"\b(plumbing\s+suppl|pipe\s+fittings?|faucets?|valves?|urinals?|plumbing\s+materials?|"
        r"insertion\s+valves?|fire\s+hydrants?|hydrant\s+(?:repair\s+)?parts?|"
        r"ductile\s+iron\s+pipe|corrugated\s+(?:high\s+density\s+)?polyethylene\s+pipe|"
        r"hdpe\s+pipe|pvc\s+pipe|water\s+meters?|flow\s+meters?|"
        r"dechlorinat(?:ing|or)|valve\s+exerciser)\b",
        re.I,
    ), "HIGH"),
    ("HVAC", re.compile(
        r"\b(hvac\s+suppl|hvac\s+parts?|air\s+handlers?|condensers?|refrigerants?)\b", re.I
    ), "HIGH"),
    ("FASTENERS", re.compile(
        r"\b(fasteners?|hex\s+bolts?|machine\s+screws?|anchor\s+bolts?|nuts?\s+and\s+bolts?)\b",
        re.I,
    ), "HIGH"),
    ("HOSES_FITTINGS", re.compile(
        r"\b(hydraulic\s+hoses?|hose\s+fittings?|hose\s+assembl(?:y|ies)|couplers?)\b", re.I
    ), "HIGH"),
    ("BEARINGS", re.compile(
        r"\b(bearings?|ball\s+bearings?|roller\s+bearings?|seal\s+kits?)\b", re.I
    ), "HIGH"),
    ("ADHESIVES", re.compile(
        r"\b(adhesives?|epoxy|sealants?|threadlockers?|caulk)\b", re.I
    ), "MEDIUM"),
    ("MRO", re.compile(
        r"\b(mro\b|maintenance\s+suppl(?:y|ies)|industrial\s+suppl|"
        r"hardware\s+suppl|gaskets?|bearings?|seal\s+kits?|fasteners?)\b",
        re.I,
    ), "MEDIUM"),
    ("COMMON_EQUIPMENT", re.compile(
        r"\b(equipment\s+purchase|pumps?|compressors?|"
        r"generator\s+purchase|purchase\s+of\s+(?:emergency\s+)?generators?|"
        r"backhoes?|loaders?|excavators?|tractors?)\b",
        re.I,
    ), "MEDIUM"),
    # Weaker cues — LOW/MEDIUM only; avoid bare civil-construction tokens
    # bare "electrical" removed — too often matches "electrical services"
    ("LIGHTING", re.compile(r"\b(lighting\s+suppl|led\s+bulbs?)\b", re.I), "MEDIUM"),
    ("AUTO_PARTS", re.compile(r"\b(fleet\s+parts?|automotive)\b", re.I), "LOW"),
    ("TOOLS", re.compile(r"\b(hand\s+tools?|power\s+tools?)\b", re.I), "LOW"),
    ("MRO", re.compile(r"\b(hardware\s+suppl|industrial\s+suppl)\b", re.I), "LOW"),
    ("PLUMBING", re.compile(r"\b(plumbing\s+suppl|valves?)\b", re.I), "LOW"),
    ("PPE", re.compile(r"\b(safety\s+gloves?|goggles?)\b", re.I), "LOW"),
    ("JANITORIAL", re.compile(r"\b(janitorial\s+suppl|cleaning\s+suppl)\b", re.I), "LOW"),
    ("OFFICE_FURNITURE", re.compile(r"\b(office\s+furniture|furniture)\b", re.I), "LOW"),
    ("OFFICE_SUPPLIES", re.compile(r"\b(toners?|office\s+suppl)\b", re.I), "LOW"),
]

_CONF_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}


def classify_text(
    text: str,
    *,
    source: str = "title",
    commodity_code: str | None = None,
) -> dict[str, Any]:
    """Classify a single text blob. Does not invent categories."""
    blob = (text or "").strip()
    if commodity_code:
        # No trusted code map in-repo yet — record presence only
        pass
    if not blob:
        return {
            "CATEGORY": "OTHER_DURABLE",
            "CATEGORY_CONFIDENCE": "UNKNOWN",
            "CATEGORY_SOURCE": source,
            "EXCLUDE_FLAG": None,
            "MATCHED_PATTERN": None,
        }
    for flag, pat in _EXCLUDE:
        if pat.search(blob):
            return {
                "CATEGORY": "OTHER_DURABLE",
                "CATEGORY_CONFIDENCE": "HIGH",
                "CATEGORY_SOURCE": source,
                "EXCLUDE_FLAG": flag,
                "MATCHED_PATTERN": pat.pattern[:80],
            }
    # Block "maintenance services" from becoming MRO even if later patterns hit
    if re.search(r"\bmaintenance\s+services?\b", blob, re.I) and not re.search(
        r"\bmaintenance\s+suppl", blob, re.I
    ):
        return {
            "CATEGORY": "OTHER_DURABLE",
            "CATEGORY_CONFIDENCE": "HIGH",
            "CATEGORY_SOURCE": source,
            "EXCLUDE_FLAG": "SERVICE",
            "MATCHED_PATTERN": "maintenance services",
        }
    best: dict[str, Any] | None = None
    for cat, pat, conf in _PRODUCT:
        if pat.search(blob):
            cand = {
                "CATEGORY": cat,
                "CATEGORY_CONFIDENCE": conf,
                "CATEGORY_SOURCE": source,
                "EXCLUDE_FLAG": None,
                "MATCHED_PATTERN": pat.pattern[:80],
            }
            if best is None or _CONF_RANK[conf] > _CONF_RANK[best["CATEGORY_CONFIDENCE"]]:
                best = cand
                if conf == "HIGH":
                    break
    if best:
        return best
    return {
        "CATEGORY": "OTHER_DURABLE",
        "CATEGORY_CONFIDENCE": "LOW",
        "CATEGORY_SOURCE": source,
        "EXCLUDE_FLAG": None,
        "MATCHED_PATTERN": None,
    }


def classify_lines(lines: list[str]) -> dict[str, Any]:
    """Aggregate line classifications → project category."""
    if not lines:
        return {
            "CATEGORY": "OTHER_DURABLE",
            "CATEGORY_CONFIDENCE": "UNKNOWN",
            "CATEGORY_SOURCE": "lines",
            "AGGREGATION": "UNKNOWN",
            "EXCLUDE_FLAG": None,
            "LINE_COUNTS": {},
        }
    counts: Counter[str] = Counter()
    excludes: Counter[str] = Counter()
    for ln in lines:
        r = classify_text(ln, source="line")
        if r.get("EXCLUDE_FLAG"):
            excludes[str(r["EXCLUDE_FLAG"])] += 1
            continue
        counts[str(r["CATEGORY"])] += 1
    if excludes and sum(excludes.values()) >= max(1, len(lines) // 2 + 1):
        top_ex = excludes.most_common(1)[0][0]
        return {
            "CATEGORY": "OTHER_DURABLE",
            "CATEGORY_CONFIDENCE": "HIGH",
            "CATEGORY_SOURCE": "lines",
            "AGGREGATION": "MIXED_PRODUCT_SERVICE" if counts else "EXCLUDED",
            "EXCLUDE_FLAG": top_ex,
            "LINE_COUNTS": dict(counts),
        }
    if not counts:
        return {
            "CATEGORY": "OTHER_DURABLE",
            "CATEGORY_CONFIDENCE": "UNKNOWN",
            "CATEGORY_SOURCE": "lines",
            "AGGREGATION": "UNKNOWN",
            "EXCLUDE_FLAG": None,
            "LINE_COUNTS": {},
        }
    named = Counter({k: v for k, v in counts.items() if k != "OTHER_DURABLE"})
    use = named if named else counts
    top_cat, top_n = use.most_common(1)[0]
    total = sum(use.values())
    share = top_n / total if total else 0
    if len(use) == 1:
        agg = "SINGLE_CATEGORY"
        conf = "HIGH" if share >= 0.8 else "MEDIUM"
    elif share >= 0.6:
        agg = "MULTI_CATEGORY"
        conf = "MEDIUM"
    else:
        agg = "MULTI_CATEGORY"
        conf = "LOW"
        top_cat = "OTHER_DURABLE"
    if total >= 8 and share < 0.35:
        agg = "MULTI_CATEGORY"
        conf = "LOW"
        top_cat = "OTHER_DURABLE"
    return {
        "CATEGORY": top_cat,
        "CATEGORY_CONFIDENCE": conf,
        "CATEGORY_SOURCE": "lines",
        "AGGREGATION": agg,
        "EXCLUDE_FLAG": None,
        "LINE_COUNTS": dict(counts),
    }


def classify_opportunity(
    *,
    title: str = "",
    description: str = "",
    line_texts: list[str] | None = None,
    commodity_code: str | None = None,
    history_category: str | None = None,
) -> dict[str, Any]:
    """Full hierarchy classification for one opportunity/project."""
    title_blob = f"{title} {description}".strip()
    # 1) Title/description exclusion always wins — do not let line product keywords
    #    reclassify a service/construction solicitation into a durable category.
    title_r = classify_text(title_blob, source="title", commodity_code=commodity_code)
    if title_r.get("EXCLUDE_FLAG"):
        return {
            **title_r,
            "AGGREGATION": "EXCLUDED",
            "HIERARCHY_USED": "title_exclude",
        }
    # Clear HIGH title product (e.g. "AUTOMOTIVE PARTS") beats noisy BOM lines
    if (
        title_r["CATEGORY"] != "OTHER_DURABLE"
        and title_r.get("CATEGORY_CONFIDENCE") == "HIGH"
    ):
        return {**title_r, "AGGREGATION": "SINGLE_CATEGORY", "HIERARCHY_USED": "title"}

    lines = [t for t in (line_texts or []) if t and str(t).strip()]
    if lines:
        line_result = classify_lines(lines)
        if line_result.get("EXCLUDE_FLAG"):
            return {**line_result, "HIERARCHY_USED": "lines"}
        if line_result["CATEGORY"] != "OTHER_DURABLE" and line_result.get("CATEGORY_CONFIDENCE") in {
            "HIGH",
            "MEDIUM",
        }:
            return {**line_result, "HIERARCHY_USED": "lines"}
        line_fallback = line_result
    else:
        line_fallback = None

    if title_r["CATEGORY"] != "OTHER_DURABLE":
        return {**title_r, "AGGREGATION": "SINGLE_CATEGORY", "HIERARCHY_USED": "title"}

    if (
        history_category
        and history_category in CANONICAL
        and history_category != "OTHER_DURABLE"
    ):
        return {
            "CATEGORY": history_category,
            "CATEGORY_CONFIDENCE": "LOW",
            "CATEGORY_SOURCE": "buyer_history_pattern",
            "AGGREGATION": "UNKNOWN",
            "EXCLUDE_FLAG": None,
            "HIERARCHY_USED": "history_support",
            "TITLE_RESULT": title_r,
        }

    out = {
        **title_r,
        "AGGREGATION": (line_fallback or {}).get("AGGREGATION") or "UNKNOWN",
        "HIERARCHY_USED": "title_other",
    }
    if line_fallback:
        out["LINE_FALLBACK"] = line_fallback
    return out
