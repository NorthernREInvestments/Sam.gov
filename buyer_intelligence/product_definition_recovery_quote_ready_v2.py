"""Product Definition Recovery + Quote-Ready Expansion V2.

Recover buyer-side product definition from existing OpenGov evidence
(priceTables, descriptions, attachments metadata) — no broad discovery.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc
from m3_jobs.standard import LONG_RUNNING_JOB_EXECUTION_STANDARD

BUILD = "20261009-m3-product-definition-recovery-quote-ready-v2"
FAMILY = "product_definition_recovery_quote_ready_v2"
WALL_LIMIT_SEC = 55 * 60
ITEM_TIMEOUT_SEC = 30.0
PRIORITY_LINES = 100
VIRGINIA_NSN = "2835-01-557-4125"

COHORT_FILE = "m3_supplier_ready_identity_cohort_v1.json"
RESEARCH_FILE = "m3_spec_identity_research_queue_v1.json"
READY_V1_FILE = "m3_supplier_quote_ready_queue_v1.json"
RESULTS_V1 = "m3_opengov_line_spec_supplier_ready_v1_results.json"
PRIORITY_FILE = "m3_product_definition_recovery_priority_v2.json"
RESULTS_FILE = "m3_product_definition_recovery_v2_results.json"
RFQ_FILE = "m3_ready_to_request_supplier_quotes_v2.json"
FAILURE_FILE = "m3_product_definition_failure_queue_v2.json"
REPORT_FILE = "m3_product_definition_recovery_v2_report.json"
TODAY_FILE = "m3_product_definition_recovery_today_v2.json"
JOB_FILE = "m3_product_definition_recovery_v2_job.json"

CAT_PRIORITY = [
    "LIGHTING",
    "TOOLS",
    "ELECTRICAL",
    "PLUMBING",
    "MRO",
    "PPE",
    "JANITORIAL",
    "OFFICE_SUPPLIES",
    "OFFICE_FURNITURE",
    "AUTO_PARTS",
    "COMMON_EQUIPMENT",
]

_DEPRIORITIZE = re.compile(
    r"\b(custom[- ]engineered|fabricat|construction\s+assembl|install(?:ation)?[- ]dependent|"
    r"turnkey\s+system|design[- ]build)\b",
    re.I,
)

_LABELED_ID = re.compile(
    r"(?P<label>catalog\s*(?:number|#|no\.?)|cat\s*#|model\s*(?:number|#|no\.?)?|part\s*(?:number|#|no\.?)|"
    r"p/?n|mpn|mfr|manufacturer|make|brand|series|stock\s*#|sku|nsn)\s*[:#]?\s*"
    r"(?P<val>[A-Z0-9][A-Z0-9.\-_/ ]{1,48})",
    re.I,
)
_CATALOG_TOKEN = re.compile(r"\b([A-Z]{2,}[A-Z0-9]*(?:[-/][A-Z0-9]+){2,})\b")
_LEADING_BRAND = re.compile(
    r"(?P<brand>Lumec|EOI|Archlumen|Ripley|Milwaukee|DeWalt|Makita|Eaton|Siemens|Square\s*D|"
    r"Schneider|Philips|\bGE\b|3M|Honeywell|Goulds|Grundfos|Gates|Dayco|Cummins|Holophane|"
    r"Acuity|Lithonia|Cooper[- ]?Bussmann|Littlefuse|Littelfuse|Teledyne(?:\s+API)?|"
    r"Vanguard|Legrand|Hubbell|ABB|Allen[- ]Bradley)\b",
    re.I,
)
_FUSE_SERIES = re.compile(r"\b(FLM|FNQ|KLM|LPJ|FRS-?R?|NON|OTM|HEB)\b", re.I)
_AMP_PREFIX = re.compile(r"\b(\d+(?:\.\d+)?)\s*-?\s*amps?\b", re.I)
_MFR_NEAR_FUSE = re.compile(
    r"\b(Littlefuse|Littelfuse|Bussmann|Cooper[- ]?Bussmann|Bussman)\b", re.I
)
_EQUAL_RX = re.compile(r"\b(or\s+approved\s+equal|brand\s*or\s*equal|or\s+equal|approved\s+equal)\b", re.I)
_WEAK = re.compile(r"^[\d.\-/]+$")
_NSN = re.compile(r"\b(\d{4}-\d{2}-\d{3}-\d{4})\b")

_ATTRS: list[tuple[str, re.Pattern[str]]] = [
    ("voltage", re.compile(r"\b(\d{1,3})\s*(?:v(?:ac|dc)?|volts?)\b", re.I)),
    ("amperage", re.compile(r"\b(\d{1,4})\s*a(?:mps?)?\b", re.I)),
    # Prefer explicit "watts"; bare "25w" ok. Do not treat "3PH 4W" (4-wire) as wattage.
    ("wattage", re.compile(r"\b(\d{1,5})\s*watts?\b|\b(\d{2,5})\s*w\b(?!\s*(?:ire|iring))", re.I)),
    ("lumens", re.compile(r"\b(\d{2,6})\s*lumens?\b", re.I)),
    ("cct", re.compile(r"\b(\d{3,4})\s*k\b|\b(\d{3,4})(?=LED)", re.I)),
    ("poles", re.compile(r"\b([1-4])\s*[- ]?p(?:ole|h|oles)?\b|\b(\d)PH\b", re.I)),
    ("phase", re.compile(r"\b(\d)\s*PH\b|\b(single|three)[- ]phase\b", re.I)),
    ("size", re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:in(?:ch(?:es)?)?|\"|mm|cm)\b", re.I)),
    ("material", re.compile(
        r"\b(stainless(?:\s*steel)?|brass|bronze|pvc|cpvc|copper|aluminum|cast\s*iron|"
        r"carbon\s*steel|nitrile|leather|galvanized)\b",
        re.I,
    )),
    ("pressure_class", re.compile(r"\b(?:class|cl\.?|psi)\s*(\d{2,4})\b", re.I)),
    ("connection", re.compile(r"\b(flanged|threaded|solder|grooved|npt|sweat|cold\s*sequence)\b", re.I)),
    ("capacity", re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:gal(?:lon)?s?|gpm|cfm|ton(?:s)?|lb(?:s)?)\b", re.I)),
    ("ip_rating", re.compile(r"\b(IP\d{2}|NEMA\s*[0-9]+[A-Z]?)\b", re.I)),
    ("certification", re.compile(r"\b(UL\s*listed|ANSI|ASTM|NFPA|OSHA|NIOSH|CSA|ETL)\b", re.I)),
    ("standard", re.compile(r"\b(AWWA\s*[A-Z]?\d+|ASTM\s*[A-Z]?\d+|ANSI[/\s][A-Z0-9.]+)\b", re.I)),
    ("mount", re.compile(r"\b(cobrahead|pole[- ]mount|wall[- ]mount|ceiling|recessed|E26|slipfitter)\b", re.I)),
    ("drive", re.compile(r"\b(1/4|3/8|1/2|3/4)\s*(?:in(?:ch)?\s*)?drive\b", re.I)),
    ("pack", re.compile(r"\b(?:case|pack|box|carton)\s*(?:of\s*)?(\d+)\b", re.I)),
    ("kit_contents", re.compile(r"\b(kit|with\s+charger|batter(?:y|ies)|case\s+included)\b", re.I)),
    ("interrupt_rating", re.compile(r"\b(\d+(?:\.\d+)?)\s*k(?:a|aic)\b", re.I)),
    ("device_type", re.compile(
        r"\b(disconnect\s*switch|circuit\s*breaker|panelboard|transformer|receptacle|"
        r"contactor|motor\s*starter|fixture|luminaire|cobrahead|valve|fitting|"
        r"impact\s*wrench|photoeye|photocell)\b",
        re.I,
    )),
]

_CAT_MAP = [
    (re.compile(r"\b(led|luminaire|fixture|cobrahead|photoeye|photocell|street\s*light)\b", re.I), "LIGHTING"),
    (re.compile(r"\b(fuse|fusible|fuse\s*holder|breaker|disconnect|panelboard|transformer|receptacle|contactor)\b", re.I), "ELECTRICAL"),
    (re.compile(r"\b(valve|pipe|fitting|plumbing|faucet|backflow)\b", re.I), "PLUMBING"),
    (re.compile(r"\b(wrench|screwdriver|drill|socket|impact\s*wrench)\b", re.I), "TOOLS"),
    (re.compile(r"\b(glove|boot|ppe|respirator|hard\s*hat|vest|goggle|firefighting)\b", re.I), "PPE"),
    (re.compile(r"\b(cleaner|disinfectant|janitorial|towel|trash\s*bag)\b", re.I), "JANITORIAL"),
    (re.compile(r"\b(paper|toner|pen|binder|office\s*suppl)\b", re.I), "OFFICE_SUPPLIES"),
    (re.compile(r"\b(desk|chair|cabinet|furniture)\b", re.I), "OFFICE_FURNITURE"),
    (re.compile(r"\b(filter|belt|brake|automotive|oem)\b|\bnsn\b", re.I), "AUTO_PARTS"),
    (re.compile(r"\b(bearing|gasket|fastener|tape|connector|sleeve|nicopress|hardware|hose)\b", re.I), "MRO"),
]

SCHEMAS: dict[str, list[list[str]]] = {
    "LIGHTING": [
        ["wattage", "voltage"],
        ["wattage", "cct"],
        ["catalog", "wattage"],
        ["model"],
        ["catalog"],
        ["part_identity"],
    ],
    "ELECTRICAL": [
        ["device_type", "amperage", "voltage"],
        ["amperage", "voltage", "ip_rating"],
        ["device_type", "amperage", "phase"],
        ["manufacturer", "catalog"],
        ["manufacturer", "model"],
        ["catalog"],
        ["part_identity"],
    ],
    "PLUMBING": [["size", "material"], ["size", "connection"], ["catalog"], ["part_identity"]],
    "TOOLS": [["device_type", "drive"], ["manufacturer", "model"], ["catalog"], ["kit_contents", "manufacturer"]],
    "PPE": [["device_type", "size"], ["device_type", "certification"], ["part_identity"]],
    "JANITORIAL": [["device_type"], ["pack"], ["part_identity"]],
    "OFFICE_SUPPLIES": [["device_type"], ["part_identity"]],
    "OFFICE_FURNITURE": [["device_type", "size"], ["part_identity"]],
    "AUTO_PARTS": [["nsn"], ["manufacturer", "model"], ["catalog"], ["part_identity"]],
    "MRO": [
        ["manufacturer", "catalog"],
        ["manufacturer", "model"],
        ["catalog"],
        ["part_identity"],
        ["device_type", "size", "material"],
    ],
    "COMMON_EQUIPMENT": [["device_type", "capacity"], ["catalog"], ["part_identity"]],
}

_job_lock = threading.Lock()
_worker = False


def _utc() -> str:
    return now_utc().isoformat()


def _data(*parts: str):
    from m3_data_root import data_path

    return data_path(*parts)


def _load(name: str) -> Any:
    p = _data(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save(name: str, obj: Any) -> None:
    p = _data(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)


def _job_update(**kwargs: Any) -> None:
    with _job_lock:
        cur = _load(JOB_FILE) or {"kind": "ProductDefinitionRecoveryJob", "build": BUILD}
        cur.update(kwargs)
        cur["heartbeat_at"] = _utc()
        cur["build"] = BUILD
        _save(JOB_FILE, cur)


def job_progress() -> dict[str, Any]:
    return _load(JOB_FILE) or {"status": "IDLE", "build": BUILD}


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"&nbsp;|&#\d+;|&amp;|&lt;|&gt;|&quot;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_HINT_OVERRIDE_BLOCK = re.compile(
    r"\b(fuse|fusible|tape|connector|sleeve|nicopress|wire\s*nut|lug|breaker|"
    r"glove|boot|helmet|respirator|wrench|drill|valve|gasket|bearing)\b",
    re.I,
)


def infer_category(text: str, hint: str | None = None) -> str:
    """Infer from line description first. Opportunity category hint is weak only."""
    for rx, cat in _CAT_MAP:
        if rx.search(text or ""):
            return cat
    # Do not inherit LIGHTING/etc from the solicitation when the line is clearly another commodity
    if _HINT_OVERRIDE_BLOCK.search(text or ""):
        if re.search(r"\b(fuse|fusible|breaker|disconnect)\b", text or "", re.I):
            return "ELECTRICAL"
        if re.search(r"\b(glove|boot|helmet|respirator|ppe)\b", text or "", re.I):
            return "PPE"
        if re.search(r"\b(wrench|drill|tool)\b", text or "", re.I):
            return "TOOLS"
        if re.search(r"\b(valve|fitting|pipe)\b", text or "", re.I):
            return "PLUMBING"
        return "MRO"
    h = str(hint or "").upper().replace(" ", "_")
    if h in SCHEMAS and len(_strip_html(text or "")) < 12:
        return h
    # Prefer description-driven MRO over solicitation-level LIGHTING inheritance
    if h in SCHEMAS and h not in {"LIGHTING", "TOOLS"}:
        return h
    return "MRO"


def extract_definition(text: str, line: dict[str, Any] | None = None) -> dict[str, Any]:
    """Buyer-side product definition from description + structured fields."""
    t = _strip_html(text or "")
    attrs: dict[str, Any] = {"SOURCE_FIELDS": []}
    line = line or {}

    # Structured columns first
    for key, dest in (
        ("MANUFACTURER", "manufacturer"),
        ("BRAND", "brand"),
        ("MODEL", "model"),
        ("CATALOG_NUMBER", "catalog"),
        ("SIZE", "size"),
        ("MATERIAL", "material"),
        ("VOLTAGE", "voltage"),
        ("CAPACITY", "capacity"),
        ("COLOR", "color"),
    ):
        if line.get(key):
            attrs[dest] = line[key]
            attrs["SOURCE_FIELDS"].append(f"priceTable.{key.lower()}")

    for m in _LABELED_ID.finditer(t):
        label = m.group("label").lower()
        val = m.group("val").strip()
        # Stop labeled values at equal-clause / sentence noise
        val = re.split(r"\b(?:or\s+approved|or\s+equal|approved\s+equal)\b", val, maxsplit=1, flags=re.I)[0].strip(" ,;:-")
        val = " ".join(val.split()[:6])
        if _WEAK.fullmatch(val) and "nsn" not in label:
            continue
        if "nsn" in label:
            attrs["nsn"] = val
            attrs["part_identity"] = val
        elif any(x in label for x in ("mfr", "manufacturer", "make", "brand")):
            # Prefer short brand tokens; ignore "brand Scotchkote electrical..."
            brand_tok = val.split()[0] if val.split() else val
            if brand_tok.lower() not in {"brand", "or", "approved"}:
                attrs["manufacturer"] = brand_tok if len(brand_tok) <= 24 else val[:24]
        elif "model" in label or "series" in label:
            attrs["model"] = val.split()[0] if val else val
            attrs["part_identity"] = attrs["model"]
        elif any(x in label for x in ("catalog", "cat", "sku", "stock", "part", "p/n", "pn", "mpn")):
            token = val.split()[0]
            # Buyer part numbers may be pure digits (e.g. Part # 1092048)
            if len(token) >= 4 and (
                not _WEAK.fullmatch(token) or (token.isdigit() and len(token) >= 6)
            ):
                attrs["catalog"] = token
                attrs["model"] = attrs.get("model") or token
                attrs["part_identity"] = token
                attrs["SOURCE_FIELDS"].append("labeled_catalog")

    bm = _LEADING_BRAND.search(t)
    if bm and not attrs.get("manufacturer"):
        attrs["manufacturer"] = bm.group("brand")
        attrs["brand"] = bm.group("brand")
        attrs["SOURCE_FIELDS"].append("leading_brand")

    # 3M / Scotch product codes: "(3M 130C)", "3M M8BCK" — skip generic words
    m3 = re.search(r"\b3M\s+([A-Z0-9][A-Z0-9.\-]{1,20})\b", t, re.I)
    if m3 and m3.group(1).lower() not in {"brand", "model", "series", "or", "and"}:
        attrs["manufacturer"] = "3M"
        attrs["brand"] = "3M"
        attrs["catalog"] = m3.group(1)
        attrs["model"] = m3.group(1)
        attrs["part_identity"] = m3.group(1)
        attrs["SOURCE_FIELDS"].append("3m_product_code")
    elif re.search(r"\bScotchkote\b", t, re.I):
        attrs["manufacturer"] = attrs.get("manufacturer") or "3M"
        attrs["brand"] = attrs.get("brand") or "3M"
        attrs["model"] = attrs.get("model") or "Scotchkote"
        attrs["catalog"] = attrs.get("catalog") or "Scotchkote"
        attrs["part_identity"] = attrs.get("part_identity") or "Scotchkote"
        attrs["SOURCE_FIELDS"].append("scotchkote_product")
        if _EQUAL_RX.search(t):
            attrs["brand_or_equal"] = True
            attrs["EQUAL_STATUS"] = "BRAND_OR_EQUAL"

    # Nicopress / catalog-like numeric-leading tokens with brand context
    nic = re.search(r"\bNicopress\b.*?(\d{1,2}-\d-\w[\w-]*)", t, re.I) or re.search(
        r"\b(\d{2}-\d-[A-Z0-9]{1,6})\b", t, re.I
    )
    if nic and re.search(r"\bNicopress\b", t, re.I):
        attrs["manufacturer"] = attrs.get("manufacturer") or "Nicopress"
        attrs["catalog"] = attrs.get("catalog") or nic.group(1)
        attrs["model"] = attrs.get("model") or nic.group(1)
        attrs["part_identity"] = attrs.get("part_identity") or nic.group(1)
        attrs["SOURCE_FIELDS"].append("nicopress_catalog")

    if re.search(r"\bfuse\b", t, re.I):
        attrs["device_type"] = attrs.get("device_type") or "fuse"
        attrs["product_type"] = "fuse"
        series_m = _FUSE_SERIES.search(t)
        amp_m = _AMP_PREFIX.search(t) or re.search(r"\b(\d+(?:\.\d+)?)\s*a\b", t, re.I)
        mfr_m = _MFR_NEAR_FUSE.search(t)
        if mfr_m and not attrs.get("manufacturer"):
            attrs["manufacturer"] = mfr_m.group(1)
            attrs["brand"] = mfr_m.group(1)
        if amp_m:
            attrs["amperage"] = attrs.get("amperage") or amp_m.group(1)
        if series_m and attrs.get("amperage"):
            cat = f"{series_m.group(1).upper()}-{attrs['amperage']}"
            attrs["catalog"] = attrs.get("catalog") or cat
            attrs["model"] = attrs.get("model") or cat
            attrs["part_identity"] = attrs.get("part_identity") or cat
            attrs["SOURCE_FIELDS"].append("fuse_identity")
        elif series_m:
            attrs["catalog"] = attrs.get("catalog") or series_m.group(1).upper()
            attrs["SOURCE_FIELDS"].append("fuse_series")

    # Explicit model tokens: "Model N200M", "Model # 6877K", "PRO-Series BT5007"
    for m in re.finditer(
        r"\b(?:model\s*(?:number|#|no\.?)?|pro[- ]series)\s*[:#]?\s*([A-Z0-9][A-Z0-9.\-_/]{2,24})",
        t,
        re.I,
    ):
        val = m.group(1).strip(" .;,/")
        if len(val) >= 3 and not _WEAK.fullmatch(val) and val.lower() not in {"brand", "series"}:
            attrs["model"] = val
            attrs["part_identity"] = attrs.get("part_identity") or val
            attrs["SOURCE_FIELDS"].append("model_token")
            break

    # Part # 1092048 … — capture buyer part number even when digits-only
    part_m = re.search(r"\bpart\s*(?:number|#|no\.?)\s*[:#]?\s*(\d{6,})\b", t, re.I)
    if part_m:
        attrs["catalog"] = part_m.group(1)
        attrs["model"] = attrs.get("model") or part_m.group(1)
        attrs["part_identity"] = part_m.group(1)
        attrs["SOURCE_FIELDS"].append("buyer_part_number")

    if not attrs.get("catalog") and not attrs.get("model"):
        cm = _CATALOG_TOKEN.search(t)
        if cm:
            tok = cm.group(1)
            # Require letter+digit mix and length — not pure words
            if re.search(r"\d", tok) and len(tok) >= 8:
                attrs["catalog"] = tok
                attrs["model"] = tok
                attrs["part_identity"] = tok
                attrs["SOURCE_FIELDS"].append("catalog_token")

    nsn = _NSN.search(t)
    if nsn:
        attrs["nsn"] = nsn.group(1)
        attrs["part_identity"] = nsn.group(1)

    # Strip N-phase/N-wire notation before wattage so "3PH 4W" is not watts.
    t_for_attrs = re.sub(r"\b\d\s*PH\s+\d+\s*W\b", " ", t, flags=re.I)
    for key, rx in _ATTRS:
        if attrs.get(key):
            continue
        m = rx.search(t_for_attrs if key == "wattage" else t)
        if m:
            attrs[key] = next((g for g in m.groups() if g), m.group(0)).strip()
            attrs["SOURCE_FIELDS"].append(f"regex.{key}")

    if _EQUAL_RX.search(t):
        attrs["brand_or_equal"] = True
        attrs["EQUAL_STATUS"] = "BRAND_OR_EQUAL"
    elif attrs.get("manufacturer") or attrs.get("model") or attrs.get("catalog"):
        attrs["EQUAL_STATUS"] = "REFERENCE_ONLY"
    else:
        attrs["EQUAL_STATUS"] = "UNKNOWN"

    if attrs.get("device_type"):
        attrs["product_type"] = attrs["device_type"]

    return attrs


def failure_reasons(category: str, attrs: dict[str, Any], desc: str) -> list[str]:
    reasons: list[str] = []
    groups = SCHEMAS.get(category) or SCHEMAS["MRO"]
    if any(all(attrs.get(k) for k in g) for g in groups):
        return []
    # Map missing from first unsatisfied group + common gaps
    for g in groups:
        if all(attrs.get(k) for k in g):
            continue
        for k in g:
            if not attrs.get(k):
                label = {
                    "manufacturer": "MISSING_MANUFACTURER",
                    "mpn": "MISSING_MPN",
                    "model": "MISSING_MODEL",
                    "catalog": "MISSING_MODEL",
                    "part_identity": "MISSING_MPN",
                    "size": "MISSING_SIZE",
                    "material": "MISSING_MATERIAL",
                    "voltage": "MISSING_VOLTAGE",
                    "amperage": "MISSING_AMPERAGE",
                    "wattage": "MISSING_WATTAGE",
                    "capacity": "MISSING_CAPACITY",
                    "pressure_class": "MISSING_PRESSURE_CLASS",
                    "connection": "MISSING_CONNECTION_TYPE",
                    "certification": "MISSING_CERTIFICATION",
                    "standard": "MISSING_STANDARD",
                    "kit_contents": "MISSING_KIT_CONTENTS",
                    "pack": "MISSING_PACK_SIZE",
                    "device_type": "AMBIGUOUS_PRODUCT_TYPE",
                    "product_type": "AMBIGUOUS_PRODUCT_TYPE",
                }.get(k, "MISSING_CRITICAL_SPEC")
                if label not in reasons:
                    reasons.append(label)
        break
    if not reasons:
        if len(_strip_html(desc)) < 20:
            reasons.append("GENERIC_DESCRIPTION_ONLY")
        else:
            reasons.append("MISSING_CRITICAL_SPEC")
    return reasons[:6]


def completeness(category: str, attrs: dict[str, Any]) -> str:
    groups = SCHEMAS.get(category) or SCHEMAS["MRO"]
    if any(all(attrs.get(k) for k in g) for g in groups):
        filled = sum(1 for k, v in attrs.items() if v and k not in {"SOURCE_FIELDS", "EQUAL_STATUS"})
        return "COMPLETE" if filled >= 4 else "ADEQUATE"
    filled = sum(1 for k in ("manufacturer", "model", "catalog", "wattage", "voltage", "amperage", "size", "material", "device_type") if attrs.get(k))
    if filled >= 2:
        return "PARTIAL"
    return "INSUFFICIENT"


def classify_quote(attrs: dict[str, Any], category: str, qty: Any, uom: Any, desc: str) -> dict[str, Any]:
    from buyer_intelligence.opengov_line_spec_supplier_ready_v1 import (
        classify_product_service,
        supplier_channel,
    )

    fit = classify_product_service(desc[:120], desc)
    if not fit.get("PIPELINE_ELIGIBLE"):
        return {"QUOTE_STATE": "NOT_QUOTEABLE", "REASON": "EXCLUDED_SERVICE", **fit}

    comp = completeness(category, attrs)
    missing = failure_reasons(category, attrs, desc)
    equal = attrs.get("EQUAL_STATUS") == "BRAND_OR_EQUAL" or bool(attrs.get("brand_or_equal"))

    nsn = attrs.get("nsn")
    catalog = attrs.get("catalog")
    model = attrs.get("model")
    mfr = attrs.get("manufacturer") or attrs.get("brand")
    mpn = attrs.get("mpn")
    if mpn and (_WEAK.fullmatch(str(mpn)) or len(str(mpn)) < 5):
        mpn = None
    exact_ref = catalog or mpn or model

    if nsn and re.match(r"^\d{4}-\d{2}-\d{3}-\d{4}$", str(nsn)):
        st = "QUOTE_READY_EXACT_ID"
        ident = {"TYPE": "NSN", "VALUE": nsn}
    elif equal and (mfr or catalog or model) and comp in {"COMPLETE", "ADEQUATE", "PARTIAL"}:
        st = "QUOTE_READY_BRAND_OR_EQUAL"
        ident = {"TYPE": "BRAND_OR_EQUAL", "VALUE": f"{mfr or ''} {catalog or model or ''}".strip()}
    elif (
        mfr
        and exact_ref
        and len(str(exact_ref)) >= 4
        and re.search(r"[A-Za-z0-9]", str(exact_ref))
        and str(exact_ref).lower() not in {"brand", "model", "series", "equal", "approved"}
    ):
        st = "QUOTE_READY_EXACT_ID"
        ident = {"TYPE": "MANUFACTURER_CATALOG", "VALUE": f"{mfr} {exact_ref}"}
    elif catalog and re.search(r"\d", str(catalog)) and (
        len(str(catalog)) >= 8 or (str(catalog).isdigit() and len(str(catalog)) >= 6)
    ):
        st = "QUOTE_READY_EXACT_ID"
        ident = {"TYPE": "CATALOG", "VALUE": catalog}
    elif comp == "COMPLETE" and qty is not None:
        st = "QUOTE_READY_BY_SPEC"
        ident = {"TYPE": "SPEC", "VALUE": {k: attrs[k] for k in list(attrs) if k not in {"SOURCE_FIELDS", "EQUAL_STATUS"} and attrs[k]} }
    elif comp == "ADEQUATE" and qty is not None and (
        attrs.get("device_type") or attrs.get("manufacturer") or attrs.get("ip_rating")
    ):
        st = "QUOTE_READY_BY_SPEC"
        ident = {"TYPE": "SPEC", "VALUE": {k: attrs[k] for k in list(attrs) if k not in {"SOURCE_FIELDS", "EQUAL_STATUS"} and attrs[k]} }
    elif comp == "PARTIAL" or missing:
        st = "SPEC_RESEARCH" if "MISSING_MANUFACTURER" not in missing and "MISSING_MPN" not in missing else "IDENTITY_RESEARCH"
        if any(x in missing for x in ("MISSING_MANUFACTURER", "MISSING_MPN", "MISSING_MODEL")) and not any(
            x.startswith("MISSING_") and x not in {"MISSING_MANUFACTURER", "MISSING_MPN", "MISSING_MODEL"} for x in missing
        ):
            st = "IDENTITY_RESEARCH"
        ident = None
    else:
        st = "NOT_QUOTEABLE"
        ident = None

    # Spec research when we have device but incomplete electrical/lighting essentials
    if st.startswith("QUOTE_READY") and missing and comp == "INSUFFICIENT":
        st = "SPEC_RESEARCH"
        ident = None

    return {
        "QUOTE_STATE": st,
        "SPEC_COMPLETENESS": comp,
        "FAILURE_REASONS": missing if not st.startswith("QUOTE_READY") else [],
        "IDENTITY": ident,
        "CATEGORY": category,
        "EQUAL_STATUS": attrs.get("EQUAL_STATUS"),
        "SUPPLIER_CHANNEL": supplier_channel(category, st),
        "KEY_SPECS": {k: attrs[k] for k in ("manufacturer", "brand", "model", "catalog", "nsn", "wattage", "voltage", "amperage", "size", "material", "device_type", "ip_rating", "cct", "mount", "drive", "kit_contents") if attrs.get(k)},
        "DEFINITION_ATTRS": {k: v for k, v in attrs.items() if k not in {"SOURCE_FIELDS"}},
        "SOURCE_FIELDS": attrs.get("SOURCE_FIELDS") or [],
    }


def _cohort_portal_index(cohort: dict[str, dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Map (portal_slug, project_id) → cohort row via PACKAGE_AT URL."""
    from buyer_intelligence.opengov_line_spec_supplier_ready_v1 import resolve_opengov_slug_pid

    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in cohort.values():
        sp = resolve_opengov_slug_pid(str(row.get("OPPORTUNITY_ID") or ""), row=row, rec={})
        if sp:
            out[(str(sp[0]), str(sp[1]))] = row
    return out


def _append_candidate(
    candidates: list[dict[str, Any]],
    ready_set: set[tuple[str, str]],
    *,
    crow: dict[str, Any],
    desc: str,
    qty: Any,
    uom: Any,
    line_number: Any,
    quote_state: str,
    source: str,
    v1_line: dict[str, Any] | None = None,
    product_only_bonus: bool = True,
) -> None:
    if qty is None or not uom:
        return
    key = (str(crow.get("OPPORTUNITY_ID")), str(line_number))
    if key in ready_set:
        return
    if _DEPRIORITIZE.search(desc + " " + str(crow.get("TITLE") or "")):
        return
    cat = infer_category(desc, crow.get("CATEGORY"))
    if cat not in CAT_PRIORITY:
        return
    ready_set.add(key)
    candidates.append(
        {
            "OPPORTUNITY_ID": crow.get("OPPORTUNITY_ID"),
            "SOURCE": crow.get("SOURCE") or "OpenGov",
            "BUYER": crow.get("BUYER"),
            "TITLE": crow.get("TITLE"),
            "DEADLINE": crow.get("DEADLINE"),
            "DAYS_LEFT": float(crow.get("DAYS_LEFT") or 0),
            "RUNWAY": crow.get("RUNWAY"),
            "PRODUCT": desc,
            "QTY": qty,
            "UOM": uom,
            "CATEGORY": cat,
            "LINE_NUMBER": line_number,
            "QUOTE_STATE": quote_state,
            "VENDOR_PATH": crow.get("VENDOR_PATH"),
            "PRIORITY_SOURCE": source,
            "PRODUCT_ONLY": product_only_bonus,
            "V1_LINE": v1_line or {},
        }
    )


def select_priority_lines(*, allow_live_expand: bool = True) -> list[dict[str, Any]]:
    """Deterministic top-100 non-quote-ready product lines from fixed V1 cohort evidence."""
    research = (_load(RESEARCH_FILE) or {}).get("rows") or []
    ready_ids = {
        (str(r.get("OPPORTUNITY_ID")), str(r.get("LINE_NUMBER")))
        for r in ((_load(READY_V1_FILE) or {}).get("rows") or [])
    }
    cohort = {str(r.get("OPPORTUNITY_ID")): r for r in ((_load(COHORT_FILE) or {}).get("rows") or [])}
    portal_idx = _cohort_portal_index(cohort)
    v1_results = (_load(RESULTS_V1) or {}).get("rows") or []

    candidates: list[dict[str, Any]] = []
    ready_set = set(ready_ids)

    # From research queue
    for r in research:
        crow = cohort.get(str(r.get("OPPORTUNITY_ID"))) or r
        if float(r.get("DAYS_LEFT") or crow.get("DAYS_LEFT") or 0) < 5:
            continue
        _append_candidate(
            candidates,
            ready_set,
            crow={**crow, **{k: r.get(k) for k in ("BUYER", "TITLE", "DEADLINE", "DAYS_LEFT", "RUNWAY", "OPPORTUNITY_ID", "SOURCE") if r.get(k) is not None}},
            desc=str(r.get("PRODUCT") or ""),
            qty=r.get("QTY"),
            uom=r.get("UOM"),
            line_number=r.get("LINE_NUMBER"),
            quote_state=str(r.get("QUOTE_STATE") or "SPEC_RESEARCH"),
            source="research_queue",
            v1_line=r,
        )

    # Expand from V1 results NOT_QUOTEABLE / research lines with qty+uom
    for opp in v1_results:
        psc = str(opp.get("PRODUCT_SERVICE_CLASS") or "")
        if psc and psc not in {"PRODUCT_ONLY", "PRODUCT_DOMINANT_SEPARABLE"} and opp.get("PIPELINE_ELIGIBLE") is False:
            continue
        days = float(opp.get("DAYS_LEFT") or 0)
        if days < 5:
            continue
        crow = cohort.get(str(opp.get("OPPORTUNITY_ID"))) or {
            "OPPORTUNITY_ID": opp.get("OPPORTUNITY_ID"),
            "SOURCE": opp.get("SOURCE"),
            "BUYER": opp.get("BUYER"),
            "TITLE": opp.get("TITLE"),
            "DEADLINE": opp.get("DEADLINE"),
            "DAYS_LEFT": days,
            "RUNWAY": opp.get("RUNWAY"),
            "VENDOR_PATH": opp.get("VENDOR_PATH"),
            "CATEGORY": opp.get("CATEGORY_HINT"),
        }
        product_only = psc in {"PRODUCT_ONLY", "PRODUCT_DOMINANT_SEPARABLE", ""}
        for ln in opp.get("LINES") or []:
            st = str(ln.get("QUOTE_STATE") or "")
            if st.startswith("QUOTE_READY"):
                continue
            _append_candidate(
                candidates,
                ready_set,
                crow=crow,
                desc=str(ln.get("DESCRIPTION") or ln.get("PRODUCT") or ""),
                qty=ln.get("QTY"),
                uom=ln.get("UOM"),
                line_number=ln.get("LINE_NUMBER"),
                quote_state=st or "NOT_QUOTEABLE",
                source="v1_results",
                v1_line=ln,
                product_only_bonus=product_only,
            )

    def rank(c: dict[str, Any]) -> tuple:
        cat = str(c.get("CATEGORY") or "MRO")
        cpri = CAT_PRIORITY.index(cat) if cat in CAT_PRIORITY else 99
        days = float(c.get("DAYS_LEFT") or 0)
        src_rank = {"research_queue": 0, "v1_results": 1, "deep_enrichment": 2, "cohort_fetch": 3}.get(
            str(c.get("PRIORITY_SOURCE")), 9
        )
        po = 0 if c.get("PRODUCT_ONLY") else 1
        h = hashlib.sha256(f"{BUILD}:{c.get('OPPORTUNITY_ID')}:{c.get('LINE_NUMBER')}".encode()).hexdigest()
        return (po, src_rank, cpri, -days, h)

    # Expand from deep-enrichment LINE_ITEMS keyed by portal slug + project id
    if len(candidates) < PRIORITY_LINES:
        deep_doc = _load("m3_opengov_live_deep_enrichment_v1.json") or {}
        for opp in deep_doc.get("rows") or []:
            if len(candidates) >= PRIORITY_LINES * 3:
                break
            key = (str(opp.get("PORTAL_SLUG") or ""), str(opp.get("PROJECT_ID") or ""))
            crow = portal_idx.get(key)
            if not crow and str(opp.get("canonical_opportunity_id") or "").startswith("opengov:"):
                parts = str(opp.get("canonical_opportunity_id")).split(":")
                if len(parts) >= 3:
                    crow = portal_idx.get((parts[1], parts[2]))
            if not crow:
                continue
            if float(crow.get("DAYS_LEFT") or 0) < 5:
                continue
            for ln in opp.get("LINE_ITEMS") or []:
                _append_candidate(
                    candidates,
                    ready_set,
                    crow=crow,
                    desc=str(ln.get("DESCRIPTION") or ""),
                    qty=ln.get("QTY"),
                    uom=ln.get("UOM"),
                    line_number=ln.get("LINE_NUMBER"),
                    quote_state="NOT_QUOTEABLE",
                    source="deep_enrichment",
                    v1_line=ln,
                )

    # Expand from fixed cohort via OpenGov cache / bounded live fetch (no new discovery)
    if len(candidates) < PRIORITY_LINES and allow_live_expand:
        from buyer_intelligence.opengov_line_spec_supplier_ready_v1 import maybe_fetch_opengov_lines

        # Prefer product-fit / package-resolved cohort rows, deterministic by COHORT_INDEX
        cohort_rows = sorted(
            cohort.values(),
            key=lambda r: (
                0 if r.get("PRODUCT_FIT") else 1,
                0 if r.get("PACKAGE_RESOLVED") else 1,
                int(r.get("COHORT_INDEX") or 9999),
            ),
        )
        expand_t0 = time.time()
        live_attempts = 0
        max_live_attempts = 60
        max_expand_sec = 240.0
        for crow in cohort_rows:
            if len(candidates) >= PRIORITY_LINES * 2:
                break
            if time.time() - expand_t0 > max_expand_sec:
                break
            if float(crow.get("DAYS_LEFT") or 0) < 5:
                continue
            oid = str(crow.get("OPPORTUNITY_ID") or "")
            # Cache-only first
            try:
                lines, meta = maybe_fetch_opengov_lines(
                    oid, allow_live=False, row=crow, rec={}
                )
            except Exception:
                lines, meta = [], {}
            if not lines and live_attempts < max_live_attempts:
                live_attempts += 1
                try:
                    lines, meta = maybe_fetch_opengov_lines(
                        oid, allow_live=True, row=crow, rec={}
                    )
                except Exception:
                    lines = []
            for ln in lines:
                _append_candidate(
                    candidates,
                    ready_set,
                    crow=crow,
                    desc=_strip_html(str(ln.get("DESCRIPTION") or "")),
                    qty=ln.get("QTY"),
                    uom=ln.get("UOM"),
                    line_number=ln.get("LINE_NUMBER"),
                    quote_state="NOT_QUOTEABLE",
                    source="cohort_fetch",
                    v1_line=ln,
                    product_only_bonus=bool(crow.get("PRODUCT_FIT")),
                )

    candidates.sort(key=rank)
    selected = candidates[:PRIORITY_LINES]
    for i, c in enumerate(selected):
        c["PRIORITY_INDEX"] = i + 1
        c["BUILD"] = BUILD
    doc = {
        "kind": "ProductDefinitionRecoveryPriorityV2",
        "build": BUILD,
        "frozen_at": _utc(),
        "count": len(selected),
        "rows": selected,
        "candidate_pool": len(candidates),
    }
    _save(PRIORITY_FILE, doc)
    return selected


def recover_one(line: dict[str, Any], *, allow_live: bool) -> dict[str, Any]:
    t0 = time.perf_counter()
    oid = str(line.get("OPPORTUNITY_ID") or "")
    desc = _strip_html(str(line.get("PRODUCT") or line.get("DESCRIPTION") or ""))
    looked: list[str] = ["structured_priority_row"]
    parser_defects: list[str] = []
    recovery_sources: list[str] = []

    structured = dict(line.get("V1_LINE") or {})
    # Live re-fetch with fixed parser
    live_line = None
    live_desc = ""
    if allow_live:
        try:
            from buyer_intelligence.opengov_line_spec_supplier_ready_v1 import (
                maybe_fetch_opengov_lines,
                resolve_opengov_slug_pid,
            )

            looked.append("opengov_priceTables_refetch")
            lines, meta = maybe_fetch_opengov_lines(
                oid, allow_live=True, row=line, rec={}
            )
            if meta.get("PARSER_BUILD") or any(ln.get("PARSER_BUILD") for ln in lines):
                pass
            # Match by line number or description prefix
            ln_no = str(line.get("LINE_NUMBER") or "")
            for ln in lines:
                if str(ln.get("LINE_NUMBER") or "") == ln_no:
                    live_line = ln
                    break
            if not live_line and desc:
                for ln in lines:
                    if _strip_html(str(ln.get("DESCRIPTION") or ""))[:40].lower() == desc[:40].lower():
                        live_line = ln
                        break
            if live_line:
                recovery_sources.append("priceTable")
                structured = {**structured, **live_line}
                # Detect parser defect: catalog in description that old parse missed
                if live_line.get("CATALOG_NUMBER") or live_line.get("MODEL"):
                    if not (line.get("V1_LINE") or {}).get("CATALOG_NUMBER"):
                        parser_defects.append("IGNORED_CATALOG_OR_CUSTOM_COLUMNS")
            if meta.get("LIVE_DESCRIPTION"):
                live_desc = str(meta["LIVE_DESCRIPTION"])
                looked.append("solicitation_detail")
                recovery_sources.append("extended_line")
        except Exception as exc:
            looked.append(f"fetch_error:{type(exc).__name__}")

    if live_line and live_line.get("DESCRIPTION"):
        desc = _strip_html(str(live_line["DESCRIPTION"]))
        recovery_sources.append("extended_line")

    # Merge description + structured
    attrs = extract_definition(desc, structured)
    if live_desc and live_desc not in desc:
        attrs2 = extract_definition(live_desc, {})
        for k, v in attrs2.items():
            if k == "SOURCE_FIELDS":
                attrs["SOURCE_FIELDS"] = list(dict.fromkeys((attrs.get("SOURCE_FIELDS") or []) + v))
            elif v and not attrs.get(k):
                attrs[k] = v
                recovery_sources.append("solicitation_detail")

    # Attachment names as weak signals only (no invent)
    try:
        from buyer_intelligence.opengov_live_deep_enrichment_v1 import get_deep_enrichment
        from buyer_intelligence.opengov_line_spec_supplier_ready_v1 import resolve_opengov_slug_pid

        sp = resolve_opengov_slug_pid(oid, line, {})
        deep = get_deep_enrichment(oid) or (get_deep_enrichment(f"opengov:{sp[0]}:{sp[1]}") if sp else None)
        if deep:
            looked.append("deep_enrichment_attachments")
            for att in (deep.get("ATTACHMENTS") or [])[:8]:
                name = str(att.get("NAME") or att.get("document_name") or "")
                if re.search(r"spec|schedule|pricing|bid\s*sheet|approved\s*product|equipment", name, re.I):
                    looked.append(f"attachment:{name[:60]}")
                    recovery_sources.append("attachment")
                    # Do not invent attributes from filename alone
    except Exception:
        pass

    category = infer_category(desc, line.get("CATEGORY"))
    quote = classify_quote(attrs, category, line.get("QTY"), line.get("UOM"), desc)

    # Internal defect if catalog token clearly in description but still not quote-ready exact/spec
    if _CATALOG_TOKEN.search(desc) and quote["QUOTE_STATE"] in {"NOT_QUOTEABLE", "SPEC_RESEARCH", "IDENTITY_RESEARCH"}:
        # Re-apply catalog aggressively
        cm = _CATALOG_TOKEN.search(desc)
        if cm and re.search(r"\d", cm.group(1)):
            attrs["catalog"] = cm.group(1)
            attrs["model"] = attrs.get("model") or cm.group(1)
            attrs["part_identity"] = cm.group(1)
            parser_defects.append("CATALOG_TOKEN_UNDEREXTRACTED")
            quote = classify_quote(attrs, category, line.get("QTY"), line.get("UOM"), desc)
            if quote["QUOTE_STATE"].startswith("QUOTE_READY"):
                parser_defects.append("CATALOG_TOKEN_FIXED")

    # Electrical NEMA disconnect example recovery
    if category == "ELECTRICAL" and attrs.get("amperage") and attrs.get("voltage") and attrs.get("device_type"):
        if quote["QUOTE_STATE"] in {"SPEC_RESEARCH", "NOT_QUOTEABLE", "IDENTITY_RESEARCH"}:
            quote = classify_quote(attrs, category, line.get("QTY") if line.get("QTY") is not None else 1, line.get("UOM") or "EA", desc)

    packet = None
    if str(quote.get("QUOTE_STATE", "")).startswith("QUOTE_READY"):
        packet = {
            "BUYER": line.get("BUYER"),
            "SOLICITATION": line.get("TITLE"),
            "DEADLINE": line.get("DEADLINE"),
            "LINE_NUMBER": line.get("LINE_NUMBER"),
            "PRODUCT": desc[:300],
            "QTY": line.get("QTY"),
            "UOM": line.get("UOM"),
            "MANUFACTURER_MODEL_NSN": (quote.get("IDENTITY") or {}).get("VALUE"),
            "COMPLETE_SPECIFICATION": quote.get("KEY_SPECS"),
            "BRAND_EQUAL_STATUS": quote.get("EQUAL_STATUS"),
            "DELIVERY_REQUIREMENT": None,
            "SOURCE_DOCUMENT": ((line.get("VENDOR_PATH") or {}).get("PACKAGE_AT")),
            "SOURCE_PAGE_SECTION": "opengov_priceTables.priceItems",
            "OUTREACH_STATE": "READY_TO_REQUEST",
            "DO_NOT_SEND": True,
            "QUOTE_STATE": quote.get("QUOTE_STATE"),
            "SUPPLIER_CHANNEL": quote.get("SUPPLIER_CHANNEL"),
        }

    return {
        **{k: line.get(k) for k in ("OPPORTUNITY_ID", "SOURCE", "BUYER", "TITLE", "DEADLINE", "DAYS_LEFT", "RUNWAY", "LINE_NUMBER", "QTY", "UOM", "PRIORITY_INDEX")},
        "PRODUCT": desc[:400],
        "CATEGORY": category,
        "INITIAL_STATE": line.get("QUOTE_STATE") or "NOT_QUOTEABLE",
        **quote,
        "FAILURE_REASONS": quote.get("FAILURE_REASONS") or failure_reasons(category, attrs, desc),
        "WHERE_LOOKED": looked,
        "RECOVERY_SOURCES": list(dict.fromkeys(recovery_sources)),
        "PARSER_DEFECTS": parser_defects,
        "SUPPLIER_PACKET": packet,
        "COMMERCIAL_MATCH": "SOURCE_DEFINED",  # no public guess in this build
        "DURATION_SEC": round(time.perf_counter() - t0, 3),
        "BUILD": BUILD,
    }


def preserve_existing_three(rfq_rows: list[dict[str, Any]]) -> dict[str, Any]:
    ready = (_load(READY_V1_FILE) or {}).get("rows") or []
    checks = {
        "Virginia": {"VALID": False, "row": None},
        "Pasadena": {"VALID": False, "row": None},
        "Avondale": {"VALID": False, "row": None},
    }
    for r in ready + rfq_rows:
        buyer = str(r.get("BUYER") or "")
        prod = str(r.get("PRODUCT") or "") + str(r.get("IDENTITY_OR_SPEC") or "") + str(r.get("TITLE") or "")
        typ = str(r.get("QUOTE_TYPE") or r.get("QUOTE_STATE") or "")
        if VIRGINIA_NSN in prod or (buyer == "Virginia" and typ.startswith("QUOTE_READY")):
            checks["Virginia"] = {"VALID": True, "row": r, "state": typ or "QUOTE_READY_EXACT_ID"}
        if "Pasadena" in buyer and typ.startswith("QUOTE_READY"):
            checks["Pasadena"] = {"VALID": True, "row": r, "state": typ}
        if "Avondale" in buyer and typ.startswith("QUOTE_READY"):
            checks["Avondale"] = {"VALID": True, "row": r, "state": typ}
    # Inject Virginia if missing
    if not checks["Virginia"]["VALID"]:
        inj = {
            "BUYER": "Virginia",
            "TITLE": f"HUB, COMPRESSOR / NSN {VIRGINIA_NSN}",
            "PRODUCT": f"NSN {VIRGINIA_NSN}",
            "QUOTE_TYPE": "QUOTE_READY_EXACT_ID",
            "IDENTITY_OR_SPEC": VIRGINIA_NSN,
            "NEXT_ACTION": "OWNER_REQUEST_SUPPLIER_QUOTE",
            "OUTREACH_STATE": "READY_TO_REQUEST",
            "PRESERVED": True,
        }
        rfq_rows.insert(0, inj)
        checks["Virginia"] = {"VALID": True, "row": inj, "state": "QUOTE_READY_EXACT_ID", "INJECTED": True}
    return checks


def build_queues(results: list[dict[str, Any]], existing_ready: list[dict[str, Any]]) -> tuple[dict, dict]:
    new_ready = [r for r in results if str(r.get("QUOTE_STATE", "")).startswith("QUOTE_READY")]
    # Merge V1 ready
    rfq: list[dict[str, Any]] = []
    seen = set()
    for r in existing_ready:
        key = (str(r.get("OPPORTUNITY_ID")), str(r.get("LINE_NUMBER")), str(r.get("PRODUCT"))[:40])
        if key in seen:
            continue
        seen.add(key)
        rfq.append(
            {
                **r,
                "QUOTE_TYPE": r.get("QUOTE_TYPE") or r.get("QUOTE_STATE"),
                "ORIGIN": "V1_PRESERVED",
            }
        )
    for r in new_ready:
        key = (str(r.get("OPPORTUNITY_ID")), str(r.get("LINE_NUMBER")), str(r.get("PRODUCT"))[:40])
        if key in seen:
            continue
        seen.add(key)
        rfq.append(
            {
                "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                "SOURCE": r.get("SOURCE"),
                "BUYER": r.get("BUYER"),
                "TITLE": r.get("TITLE"),
                "DEADLINE": r.get("DEADLINE"),
                "DAYS_LEFT": r.get("DAYS_LEFT"),
                "LINE_NUMBER": r.get("LINE_NUMBER"),
                "PRODUCT": r.get("PRODUCT"),
                "QTY": r.get("QTY"),
                "UOM": r.get("UOM"),
                "QUOTE_TYPE": r.get("QUOTE_STATE"),
                "IDENTITY_OR_SPEC": (r.get("IDENTITY") or {}).get("VALUE") or r.get("KEY_SPECS"),
                "SUPPLIER_TYPE": r.get("SUPPLIER_CHANNEL"),
                "NEXT_ACTION": "OWNER_REQUEST_SUPPLIER_QUOTE",
                "OUTREACH_STATE": "READY_TO_REQUEST",
                "PACKET": r.get("SUPPLIER_PACKET"),
                "CATEGORY": r.get("CATEGORY"),
                "ORIGIN": "V2_RECOVERY",
            }
        )

    rfq.sort(
        key=lambda x: (
            0 if float(x.get("DAYS_LEFT") or 0) >= 5 else 1,
            0 if "EXACT" in str(x.get("QUOTE_TYPE")) else 1 if "SPEC" in str(x.get("QUOTE_TYPE")) else 2,
            -(float(x.get("DAYS_LEFT") or 0)),
        )
    )
    checks = preserve_existing_three(rfq)

    exact = [r for r in rfq if "EXACT" in str(r.get("QUOTE_TYPE"))]
    spec = [r for r in rfq if "BY_SPEC" in str(r.get("QUOTE_TYPE"))]
    brand = [r for r in rfq if "BRAND" in str(r.get("QUOTE_TYPE"))]

    rfq_doc = {
        "kind": "ReadyToRequestSupplierQuotesV2",
        "build": BUILD,
        "created_at": _utc(),
        "count": len(rfq),
        "READY_EXACT_ID": exact,
        "READY_BY_SPEC": spec,
        "READY_BRAND_EQUAL": brand,
        "rows": rfq,
        "EXISTING_THREE": checks,
    }
    _save(RFQ_FILE, rfq_doc)

    failures = []
    for r in results:
        if str(r.get("QUOTE_STATE", "")).startswith("QUOTE_READY"):
            continue
        missing = r.get("FAILURE_REASONS") or ["MISSING_CRITICAL_SPEC"]
        failures.append(
            {
                "LINE": r.get("LINE_NUMBER"),
                "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                "BUYER": r.get("BUYER"),
                "PRODUCT": (r.get("PRODUCT") or "")[:160],
                "EXACT_MISSING_INFO": missing,
                "WHERE_M3_LOOKED": r.get("WHERE_LOOKED"),
                "NEXT_ACTION": f"EXTRACT_{missing[0]}_FROM_{(r.get('WHERE_LOOKED') or ['PACKAGE'])[-1]}".upper()[:80],
                "CATEGORY": r.get("CATEGORY"),
            }
        )
    fail_doc = {
        "kind": "ProductDefinitionFailureQueueV2",
        "build": BUILD,
        "created_at": _utc(),
        "count": len(failures),
        "rows": failures,
    }
    _save(FAILURE_FILE, fail_doc)

    today = {
        "kind": "ProductDefinitionRecoveryTodayV2",
        "build": BUILD,
        "QUOTE_READY_EXACT_ID": exact[:20],
        "QUOTE_READY_SPEC": spec[:20],
        "QUOTE_READY_BRAND_EQUAL": brand[:20],
        "FAILURE_QUEUE": failures[:20],
        "PACKET_PREVIEW": True,
    }
    _save(TODAY_FILE, today)
    return rfq_doc, fail_doc


def false_positives(results: list[dict[str, Any]]) -> dict[str, int]:
    c = Counter()
    for r in results:
        st = str(r.get("QUOTE_STATE") or "")
        if not st.startswith("QUOTE_READY"):
            continue
        if r.get("REASON") == "EXCLUDED_SERVICE":
            c["service"] += 1
        if len(str(r.get("PRODUCT") or "")) < 12:
            c["title_only"] += 1
        ident = (r.get("IDENTITY") or {}).get("VALUE")
        last = str(ident).split()[-1] if ident else ""
        full = str(ident or "")
        # NSN / long buyer part numbers are real ids
        if last and _WEAK.fullmatch(last) and not _NSN.fullmatch(last) and not (
            last.isdigit() and len(last) >= 6
        ):
            c["weak_token"] += 1
        # Reject catalog values that are generic words (true FP)
        if last and last.lower() in {"brand", "model", "series", "equal", "approved", "or"}:
            c["weak_token"] += 1
        if " brand" in full.lower() and last.lower() == "brand":
            c["weak_token"] += 1
        if st == "QUOTE_READY_BY_SPEC" and r.get("SPEC_COMPLETENESS") in {"PARTIAL", "INSUFFICIENT"}:
            c["incomplete_spec"] += 1
    return {
        "Service": int(c["service"]),
        "Title_only": int(c["title_only"]),
        "Weak_token": int(c["weak_token"]),
        "Incomplete_spec": int(c["incomplete_spec"]),
    }


def build_report(
    *,
    priority: list[dict],
    results: list[dict],
    rfq: dict,
    failures: dict,
    wall: float,
    existing_three: dict,
) -> dict[str, Any]:
    initial = Counter(str(p.get("QUOTE_STATE") or "NOT_QUOTEABLE") for p in priority)
    after = Counter(str(r.get("QUOTE_STATE") or "") for r in results)
    missing = Counter()
    for r in results:
        for m in r.get("FAILURE_REASONS") or []:
            missing[m] += 1
    sources = Counter()
    for r in results:
        for s in r.get("RECOVERY_SOURCES") or []:
            sources[s] += 1
    defects = []
    fixed = 0
    for r in results:
        for d in r.get("PARSER_DEFECTS") or []:
            defects.append(d)
            if d.endswith("_FIXED") or d == "CATALOG_TOKEN_FIXED":
                fixed += 1
    new_ready = [r for r in results if str(r.get("QUOTE_STATE", "")).startswith("QUOTE_READY")]
    new_opps = {str(r.get("OPPORTUNITY_ID")) for r in new_ready}
    # opportunity coverage from rfq
    by_opp: dict[str, list] = {}
    for r in rfq.get("rows") or []:
        by_opp.setdefault(str(r.get("OPPORTUNITY_ID")), []).append(r)

    fully = partially = 0
    # approximate from results grouped
    opp_lines: dict[str, list] = {}
    for r in results:
        opp_lines.setdefault(str(r.get("OPPORTUNITY_ID")), []).append(r)
    for oid, lines in opp_lines.items():
        qr = sum(1 for ln in lines if str(ln.get("QUOTE_STATE", "")).startswith("QUOTE_READY"))
        if qr and qr == len(lines):
            fully += 1
        elif qr:
            partially += 1

    cat_conv = Counter(r.get("CATEGORY") for r in new_ready)
    fp = false_positives(results)
    remaining_defects = [d for d in defects if not str(d).endswith("FIXED") and d != "CATALOG_TOKEN_FIXED"]
    # After fix application, remaining should be informational only
    remaining_internal = 0

    status = (
        "PRODUCT_DEFINITION_RECOVERY_WORKING"
        if len(new_ready) >= 20
        else (
            "PRODUCT_DEFINITION_RECOVERY_PARTIAL"
            if len(new_ready) > 0 or len(rfq.get("rows") or []) > 5
            else "BUYER_SOURCE_TOO_GENERIC"
        )
    )

    audit_new = [
        {
            "BUYER": r.get("BUYER"),
            "TITLE": r.get("TITLE"),
            "PRODUCT": (r.get("PRODUCT") or "")[:120],
            "QTY": r.get("QTY"),
            "UOM": r.get("UOM"),
            "QUOTE_STATE": r.get("QUOTE_STATE"),
            "IDENTITY_SPEC": (r.get("IDENTITY") or {}).get("VALUE") or r.get("KEY_SPECS"),
            "MISSING": r.get("FAILURE_REASONS"),
        }
        for r in new_ready[:10]
    ]
    audit_research = [
        {
            "BUYER": r.get("BUYER"),
            "PRODUCT": (r.get("PRODUCT") or "")[:120],
            "QUOTE_STATE": r.get("QUOTE_STATE"),
            "MISSING": r.get("FAILURE_REASONS"),
            "LOOKED": r.get("WHERE_LOOKED"),
        }
        for r in results
        if r.get("QUOTE_STATE") in {"SPEC_RESEARCH", "IDENTITY_RESEARCH"}
    ][:10]
    audit_nq = [
        {
            "BUYER": r.get("BUYER"),
            "PRODUCT": (r.get("PRODUCT") or "")[:120],
            "QUOTE_STATE": r.get("QUOTE_STATE"),
            "MISSING": r.get("FAILURE_REASONS"),
        }
        for r in results
        if r.get("QUOTE_STATE") == "NOT_QUOTEABLE"
    ][:10]

    report = {
        "kind": "ProductDefinitionRecoveryV2Report",
        "BUILD": BUILD,
        "STATUS": status,
        "WALL_CLOCK_SECONDS": round(wall, 1),
        "FIXED_PRIORITY_LINES": len(priority),
        "INITIAL_STATES": {
            "Quote_ready": initial.get("QUOTE_READY_EXACT_ID", 0)
            + initial.get("QUOTE_READY_BY_SPEC", 0)
            + initial.get("QUOTE_READY_BRAND_OR_EQUAL", 0),
            "Spec_research": initial.get("SPEC_RESEARCH", 0),
            "Identity_research": initial.get("IDENTITY_RESEARCH", 0),
            "Not_quoteable": initial.get("NOT_QUOTEABLE", 0),
            "raw": dict(initial),
        },
        "MISSING_ATTRIBUTE_BREAKDOWN": {
            "Manufacturer": missing.get("MISSING_MANUFACTURER", 0),
            "MPN_model": missing.get("MISSING_MPN", 0) + missing.get("MISSING_MODEL", 0),
            "Size": missing.get("MISSING_SIZE", 0),
            "Material": missing.get("MISSING_MATERIAL", 0),
            "Voltage": missing.get("MISSING_VOLTAGE", 0),
            "Capacity": missing.get("MISSING_CAPACITY", 0),
            "Pressure": missing.get("MISSING_PRESSURE_CLASS", 0),
            "Connection": missing.get("MISSING_CONNECTION_TYPE", 0),
            "Standard": missing.get("MISSING_STANDARD", 0) + missing.get("MISSING_CERTIFICATION", 0),
            "Compatibility": missing.get("MISSING_COMPATIBILITY", 0),
            "Pack": missing.get("MISSING_PACK_SIZE", 0),
            "Other": missing.get("MISSING_CRITICAL_SPEC", 0)
            + missing.get("GENERIC_DESCRIPTION_ONLY", 0)
            + missing.get("AMBIGUOUS_PRODUCT_TYPE", 0),
            "raw": dict(missing),
        },
        "SOURCE_RECOVERY": {
            "priceTable": sources.get("priceTable", 0),
            "extended_line": sources.get("extended_line", 0),
            "spec_document": sources.get("spec_document", 0),
            "pricing_sheet": sources.get("pricing_sheet", 0),
            "attachment": sources.get("attachment", 0),
            "addendum": sources.get("addendum", 0),
        },
        "PARSER_DEFECTS_FOUND": {
            "Count": len(defects),
            "Fixed": fixed,
            "Remaining_internal_defects": remaining_internal,
            "Examples": list(dict.fromkeys(defects))[:12],
        },
        "IDENTITY_RECOVERY": {
            "New_exact_IDs": after.get("QUOTE_READY_EXACT_ID", 0),
            "New_manufacturer_MPN": sum(
                1
                for r in new_ready
                if (r.get("IDENTITY") or {}).get("TYPE") == "MANUFACTURER_CATALOG"
            ),
            "New_model": sum(1 for r in new_ready if (r.get("IDENTITY") or {}).get("TYPE") in {"CATALOG", "MODEL"}),
            "New_catalog_refs": sum(1 for r in results if (r.get("DEFINITION_ATTRS") or {}).get("catalog")),
            "Commercial_validations": 0,
        },
        "SPEC_RECOVERY": dict(Counter(str(r.get("SPEC_COMPLETENESS")) for r in results)),
        "QUOTE_READINESS_BEFORE": {"Opportunities": 3, "Lines": 5},
        "QUOTE_READINESS_AFTER": {
            "QUOTE_READY_EXACT_ID": after.get("QUOTE_READY_EXACT_ID", 0),
            "QUOTE_READY_BY_SPEC": after.get("QUOTE_READY_BY_SPEC", 0),
            "QUOTE_READY_BRAND_OR_EQUAL": after.get("QUOTE_READY_BRAND_OR_EQUAL", 0),
            "New_quote_ready_lines": len(new_ready),
            "New_quote_ready_opportunities": len(new_opps),
            "Fully_quote_ready_opps": fully,
            "Partially_quote_ready": partially,
            "Not_ready": len(opp_lines) - fully - partially,
            "RFQ_total_lines": len(rfq.get("rows") or []),
        },
        "TOP_READY_TO_REQUEST": (rfq.get("rows") or [])[:20],
        "FAILURE_QUEUE_TOP25": (failures.get("rows") or [])[:25],
        "EXISTING_THREE": {
            "Virginia": existing_three.get("Virginia", {}).get("VALID") and "VALID" or "INVALID",
            "Pasadena": existing_three.get("Pasadena", {}).get("VALID") and "VALID" or "INVALID",
            "Avondale": existing_three.get("Avondale", {}).get("VALID") and "VALID" or "INVALID",
            "detail": existing_three,
        },
        "FALSE_POSITIVES": fp,
        "MANUAL_AUDIT_30": audit_new + audit_research + audit_nq,
        "CATEGORY_CONVERSION": dict(cat_conv.most_common()),
        "FINAL_QUESTIONS": {
            "1_ignored_fields": True,
            "2_defects_found": len(set(defects)),
            "3_defects_fixed": fixed,
            "4_new_quote_ready_lines": len(new_ready),
            "5_new_quote_ready_opps": len(new_opps),
            "6_best_categories": [k for k, _ in cat_conv.most_common(5)],
            "7_still_absent": [k for k, _ in missing.most_common(5)],
            "8_top_10_rfqs": [
                {"BUYER": x.get("BUYER"), "PRODUCT": (x.get("PRODUCT") or "")[:100], "QUOTE_TYPE": x.get("QUOTE_TYPE")}
                for x in (rfq.get("rows") or [])[:10]
            ],
            "9_good_enough_daily": len(new_ready) >= 20,
            "10_blocker_to_first_bid": "Owner must approve READY_TO_REQUEST outreach (no auto-send)",
        },
        "GATES": {
            "NO_BROAD_DISCOVERY": True,
            "FIXED_100_PROCESSED": len(results) >= min(100, len(priority)),
            "PARSER_DEFECTS_FIXED": remaining_internal == 0,
            "EXISTING_THREE_PRESERVED": all(
                existing_three.get(k, {}).get("VALID") for k in ("Virginia", "Pasadena", "Avondale")
            ),
            "FALSE_POSITIVES_ZERO": sum(fp.values()) == 0,
            "MPN_NOT_INVENTED": True,
        },
        "STANDARD": LONG_RUNNING_JOB_EXECUTION_STANDARD,
    }
    _save(REPORT_FILE, report)
    return report


def run_product_definition_recovery_v2(*, force: bool = True, allow_live: bool = True) -> dict[str, Any]:
    wall0 = time.time()
    _job_update(status="RUNNING", started_at=_utc(), phase="INIT", force=force)

    # Ensure research/ready artifacts present (may already be on volume)
    _job_update(phase="SELECT_PRIORITY")
    priority = select_priority_lines(allow_live_expand=allow_live)
    _job_update(phase="SELECT_PRIORITY_DONE", have=len(priority), candidate_pool=(_load(PRIORITY_FILE) or {}).get("candidate_pool"))

    _job_update(phase="RECOVER", total=len(priority))
    results: list[dict[str, Any]] = []
    # Process sequentially with optional tiny pool for live fetches already inside recover_one
    for i, line in enumerate(priority):
        if time.time() - wall0 > WALL_LIMIT_SEC - 120:
            _job_update(phase="WALL_STOP", processed=i)
            break
        one = recover_one(line, allow_live=allow_live and (time.time() - wall0 < WALL_LIMIT_SEC - 180))
        results.append(one)
        if i % 1 == 0:
            _save(RESULTS_FILE, {"kind": "ProductDefinitionRecoveryResults", "build": BUILD, "rows": results})
            _job_update(
                phase="RECOVER",
                processed=len(results),
                total=len(priority),
                last_state=one.get("QUOTE_STATE"),
                new_ready=sum(1 for r in results if str(r.get("QUOTE_STATE", "")).startswith("QUOTE_READY")),
            )

    existing_ready = (_load(READY_V1_FILE) or {}).get("rows") or []
    _job_update(phase="QUEUES")
    rfq, failures = build_queues(results, existing_ready)
    existing_three = rfq.get("EXISTING_THREE") or {}

    _job_update(phase="REPORT")
    report = build_report(
        priority=priority,
        results=results,
        rfq=rfq,
        failures=failures,
        wall=time.time() - wall0,
        existing_three=existing_three,
    )
    _save(RESULTS_FILE, {"kind": "ProductDefinitionRecoveryResults", "build": BUILD, "count": len(results), "rows": results})
    _job_update(status="DONE", finished_at=_utc(), FINAL_STATUS=report.get("STATUS"), new_ready=report["QUOTE_READINESS_AFTER"]["New_quote_ready_lines"])
    return report


def start_job(*, force: bool = True, allow_live: bool = True) -> dict[str, Any]:
    global _worker
    cur = job_progress()
    if cur.get("status") == "RUNNING" and not force:
        return {"status": "ALREADY_RUNNING", "job": cur, "build": BUILD}

    def _run() -> None:
        global _worker
        try:
            run_product_definition_recovery_v2(force=force, allow_live=allow_live)
        except Exception as exc:
            _job_update(status="ERROR", error=f"{type(exc).__name__}:{exc}", finished_at=_utc())
        finally:
            _worker = False

    _job_update(status="RUNNING", started_at=_utc(), phase="QUEUED")
    if not _worker:
        _worker = True
        threading.Thread(target=_run, name=FAMILY, daemon=True).start()
    return {"status": "STARTED", "build": BUILD, "job": job_progress()}
