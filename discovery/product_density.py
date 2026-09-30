"""Product-density classification for discovery funnel prioritization."""

from __future__ import annotations

import re
from typing import Any

PRODUCT_STRONG = "PRODUCT_STRONG"
PRODUCT_LIKELY = "PRODUCT_LIKELY"
MIXED = "MIXED"
SERVICE_LIKELY = "SERVICE_LIKELY"
SERVICE_STRONG = "SERVICE_STRONG"

# High-priority tangible goods tokens
_PRODUCT_STRONG_RE = re.compile(
    r"\b("
    r"laptop|chromebook|chromebox|ipad|server|workstation|desktop|monitor|printer|"
    r"router|switch|firewall|nas|storage array|generator|pump|valve|motor|"
    r"battery|batteries|radio|camera|trailer|utility vehicle|skid steer|"
    r"forklift|excavator|backhoe|mower|appliance|hvac unit|nsn\b|"
    r"dell\b|hp\b|lenovo|cisco|apple|asus|caterpillar|ford\b|yamaha"
    r")\b",
    re.I,
)

_PRODUCT_LIKELY_RE = re.compile(
    r"\b("
    r"equipment|hardware|parts|components?|supplies|materials|devices?|"
    r"accessories|tools?|furniture|fleet|vehicle|vans?|trucks?|vessel|"
    r"instrument|meter|sensor|cable|wire|filter|hose|fitting|kit|"
    r"assembly|chemical|acid|ppe|safety gear|locker|shelving|"
    r"networking|electronics|industrial|mro|lab equipment|test equipment|"
    r"communications|security hardware|facility equipment"
    r")\b",
    re.I,
)

_SERVICE_STRONG_RE = re.compile(
    r"\b("
    r"consulting|staffing|janitorial|catering|food service|professional services|"
    r"architectural|engineering services|legal services|audit services|"
    r"software as a service|saas|managed services|process mining|"
    r"training services|social services"
    r")\b",
    re.I,
)

_SERVICE_LIKELY_RE = re.compile(
    r"\b("
    r"services|reconstruction|renovation|construction|facade|paving|"
    r"asphalt|concrete pour|demolition|design[- ]build|inspection services|"
    r"maintenance services|cleaning and inspection|tree planting"
    r")\b",
    re.I,
)

# Commodity / NIGP rough buckets → product bias
NIGP_PRODUCT_PREFIXES = {
    "20": "computer",  # computer equipment families vary by agency
    "204": "computer",
    "206": "computer",
    "207": "computer",
    "285": "electrical",
    "445": "hand tools",
    "450": "hardware",
    "560": "material handling",
    "071": "vehicles",
    "070": "automotive",
    "257": "fire",
    "340": "fire protection",
    "680": "police",
}


def classify_product_confidence(row: dict[str, Any]) -> dict[str, Any]:
    """Return product confidence label + reasons from cheap local signals."""
    title = str(row.get("title") or "")
    desc = str(row.get("description") or "")[:2000]
    blob = f"{title}\n{desc}"
    dept = str(row.get("department") or row.get("buyer_department") or row.get("agency") or "")
    commodity = str(
        row.get("commodity_code")
        or row.get("nigp")
        or row.get("unspsc")
        or row.get("naics")
        or ""
    )
    reasons: list[str] = []

    # Commodity hint
    commodity_product = False
    for pref, label in NIGP_PRODUCT_PREFIXES.items():
        if commodity.startswith(pref):
            commodity_product = True
            reasons.append(f"commodity:{label}")
            break

    strong = bool(_PRODUCT_STRONG_RE.search(blob))
    likely = bool(_PRODUCT_LIKELY_RE.search(blob))
    svc_strong = bool(_SERVICE_STRONG_RE.search(blob))
    svc_likely = bool(_SERVICE_LIKELY_RE.search(blob))

    # Department boosts
    dept_l = dept.lower()
    if any(x in dept_l for x in ("it ", "information tech", "fleet", "public works", "warehouse", "purchasing")):
        reasons.append("product_dense_buyer_dept")
        likely = True

    if strong and not svc_strong:
        label = PRODUCT_STRONG
        reasons.append("strong_product_token")
    elif (likely or commodity_product) and svc_strong:
        label = MIXED
        reasons.append("product_and_service_signals")
    elif (likely or commodity_product) and not svc_strong:
        label = PRODUCT_LIKELY
        reasons.append("likely_product_token" if likely else "commodity_hint")
    elif svc_strong and not (strong or likely):
        label = SERVICE_STRONG
        reasons.append("strong_service_token")
    elif svc_likely and not (strong or likely):
        label = SERVICE_LIKELY
        reasons.append("likely_service_token")
    elif strong and svc_strong:
        label = MIXED
        reasons.append("mixed_strong_signals")
    else:
        label = MIXED if commodity_product else SERVICE_LIKELY
        reasons.append("default_low_signal")

    return {
        "product_confidence": label,
        "reasons": reasons,
        "is_product_priority": label in {PRODUCT_STRONG, PRODUCT_LIKELY, MIXED},
        "commodity_code": commodity or None,
    }


def product_density(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from collections import Counter

    c = Counter()
    for r in rows:
        conf = r.get("product_confidence") or classify_product_confidence(r).get("product_confidence")
        c[str(conf)] += 1
    total = max(1, len(rows))
    productish = c.get(PRODUCT_STRONG, 0) + c.get(PRODUCT_LIKELY, 0) + c.get(MIXED, 0)
    return {
        "total": len(rows),
        "counts": dict(c),
        "product_density": round(productish / total, 4),
        "product_strong_density": round(c.get(PRODUCT_STRONG, 0) / total, 4),
    }
