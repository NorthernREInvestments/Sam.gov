"""P0-2 — Hard line scope classification; block install/service/construction from product path."""

from __future__ import annotations

import re
from typing import Any

from p0_prescale_hardening.models import (
    CONSTRUCTION,
    INSTALLATION,
    LABOR,
    MIXED_SCOPE,
    NON_PRODUCT_SCOPE,
    PRODUCT,
    PRODUCT_SOURCING_OK,
    PRODUCT_WITH_INCIDENTAL_DELIVERY,
    SERVICE,
    UNKNOWN_SCOPE,
)

_CONSTRUCTION = re.compile(
    r"\b("
    r"exfiltration\s+trench|type\s+c\s+inlet|excavation|concrete|paving|grading|"
    r"demobilization|mobilization|general\s+conditions|MOT\s+plan|"
    r"construct(?:ing|ion)?\s+the|structure,\s*frame\s+and\s+grate"
    r")\b",
    re.I,
)
_INSTALL = re.compile(
    r"\b("
    r"furnishing\s+and\s+installing|installation\s+(?:of|only)|"
    r"install(?:ing|ation)\s+(?:labor|services?)|"
    r"labor\s+to\s+install|on[- ]site\s+install"
    r")\b",
    re.I,
)
_LABOR = re.compile(r"\b(labor\s+only|man[- ]?hours?|prevailing\s+wage\s+labor)\b", re.I)
_SERVICE = re.compile(
    r"\b(maintenance\s+service|warranty\s+service|training\s+services?|annual\s+service)\b",
    re.I,
)
_PRODUCT = re.compile(
    r"\b("
    r"model(?:\s+number)?\s*:|mpn|part\s*(?:#|no\.?|number)|catalog|"
    r"dumor|elkay|exofit|cummins|bound\s*tree|sensor|gasket|filter|"
    r"trauma|airway|glove|pharma|bottle\s+fill"
    r")\b",
    re.I,
)
_INCIDENTAL_DELIVERY = re.compile(
    r"\b(delivered|FOB|freight\s+included|shipped\s+to\s+site)\b",
    re.I,
)
_PLANTING_NOTE = re.compile(r"planting\s+shall\s+be\s+clear|all\s+planting", re.I)


def classify_line_scope(line: dict[str, Any]) -> dict[str, Any]:
    desc = str(line.get("description") or "")
    mfr = str(line.get("manufacturer") or "")
    blob = f"{mfr} {desc}"
    mpn = line.get("mpn") or line.get("model") or line.get("part_number")

    if _LABOR.search(blob) and not mpn:
        scope = LABOR
    elif _SERVICE.search(blob) and not mpn:
        scope = SERVICE
    elif _INSTALL.search(blob) and not mpn:
        # Prefer INSTALLATION over CONSTRUCTION when install language is explicit
        scope = INSTALLATION
    elif _CONSTRUCTION.search(blob) and not (mpn and _PRODUCT.search(blob)):
        scope = CONSTRUCTION
    elif _PLANTING_NOTE.search(desc):
        scope = CONSTRUCTION  # landscaping notes, not product
    elif _INSTALL.search(blob) and mpn:
        scope = MIXED_SCOPE
    elif mpn or _PRODUCT.search(blob):
        if _INCIDENTAL_DELIVERY.search(blob) and not _INSTALL.search(blob):
            scope = PRODUCT_WITH_INCIDENTAL_DELIVERY
        else:
            scope = PRODUCT
    elif _INSTALL.search(blob):
        scope = INSTALLATION
    else:
        scope = UNKNOWN_SCOPE

    product_sourcing_ok = scope in PRODUCT_SOURCING_OK
    return {
        "line_id": line.get("line_id"),
        "scope": scope,
        "product_sourcing_ok": product_sourcing_ok,
        "blocks_product_acquisition": scope in NON_PRODUCT_SCOPE or scope == UNKNOWN_SCOPE,
        "blocks_economics_until_separated": scope == MIXED_SCOPE,
        "reasons": _reasons(scope, blob, mpn),
    }


def _reasons(scope: str, blob: str, mpn: Any) -> list[str]:
    out = [f"scope={scope}"]
    if mpn:
        out.append(f"mpn_or_model={mpn}")
    return out


def may_enter_product_quote_packet(line: dict[str, Any], *, classified: dict[str, Any] | None = None) -> bool:
    c = classified or classify_line_scope(line)
    return bool(c.get("product_sourcing_ok"))


def may_enter_product_acquisition(line: dict[str, Any], *, classified: dict[str, Any] | None = None) -> bool:
    c = classified or classify_line_scope(line)
    return bool(c.get("product_sourcing_ok")) and c.get("scope") != MIXED_SCOPE


# Regression fixtures
SCOPE_REGRESSION_CASES = [
    {
        "id": "dania_trench",
        "line": {
            "line_id": "dania-trench",
            "description": "Unit price shall include constructing the exfiltration trench (6 ft wide by 6 ft deep) with",
            "manufacturer": None,
            "mpn": None,
        },
        "expected_scope": CONSTRUCTION,
        "must_not_enter_product_packet": True,
    },
    {
        "id": "dania_inlet",
        "line": {
            "line_id": "dania-inlet",
            "description": "Unit price shall include furnishing and installing the Type C inlet (4 ft diameter, less than",
            "manufacturer": None,
            "mpn": None,
        },
        "expected_scope": INSTALLATION,
        "must_not_enter_product_packet": True,
    },
    {
        "id": "dania_planting",
        "line": {
            "line_id": "dania-plant",
            "description": "12.ALL PLANTING SHALL BE CLEAR 7 1/2 FT. IN FRONT & SIDES WITH 4 FT. IN REAR AROUND ALL",
            "manufacturer": "EXOFIT",
            "mpn": "12.ALL",
        },
        "expected_scope": CONSTRUCTION,
        "must_not_enter_product_packet": True,
    },
    {
        "id": "dania_elkay",
        "line": {
            "line_id": "dania-elkay",
            "description": "MODEL NUMBER: LK4430BF1U",
            "manufacturer": "ELKAY",
            "mpn": "LK4430BF1U",
        },
        "expected_scope": PRODUCT,
        "must_not_enter_product_packet": False,
    },
    {
        "id": "collier_install_heavy",
        "line": {
            "line_id": "collier-install",
            "description": "Furnishing and installing irrigation controller including labor and excavation",
            "manufacturer": None,
            "mpn": None,
        },
        "expected_scope": INSTALLATION,
        "must_not_enter_product_packet": True,
    },
    {
        "id": "go_metro_sensor",
        "line": {
            "line_id": "gm-sensor",
            "description": "SENSOR, ISM INTAKE MANIFOLD",
            "manufacturer": "Cummins",
            "mpn": "13-69938-00",
        },
        "expected_scope": PRODUCT,
        "must_not_enter_product_packet": False,
    },
]


def run_scope_regression() -> dict[str, Any]:
    results = []
    passed = 0
    for case in SCOPE_REGRESSION_CASES:
        c = classify_line_scope(case["line"])
        ok = c["scope"] == case["expected_scope"]
        enter = may_enter_product_quote_packet(case["line"], classified=c)
        leak_ok = (not enter) if case["must_not_enter_product_packet"] else enter
        row_pass = ok and leak_ok
        if row_pass:
            passed += 1
        results.append(
            {
                "id": case["id"],
                "expected": case["expected_scope"],
                "got": c["scope"],
                "enter_packet": enter,
                "pass": row_pass,
            }
        )
    return {
        "total": len(SCOPE_REGRESSION_CASES),
        "passed": passed,
        "failed": len(SCOPE_REGRESSION_CASES) - passed,
        "all_pass": passed == len(SCOPE_REGRESSION_CASES),
        "results": results,
    }
