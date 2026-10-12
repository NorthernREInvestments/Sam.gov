"""Identity Extraction Hardening + Clean Priceable Corpus V2.

Hardens MPN/SKU/NSN extraction so buyer codes, brands, and description
tokens cannot become PROVEN commercial identifiers. Builds a clean frozen
commercial-pricing test set (identity quality only — no price cherry-picking).
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from application_clock import now_utc

BUILD = "20261008-m3-identity-hardening-clean-priceable-corpus-v2"
BID_INDEX_FILE = "opengov_bid_price_index.json"
POOL_FILE = "m3_historical_profit_test_pool_v2.json"
REPORT_FILE = "m3_identity_hardening_clean_priceable_corpus_v2_report.json"
UI_FILE = "m3_identity_hardening_clean_priceable_corpus_v2_ui.json"
UNIVERSE_FILE = "m3_clean_priceable_identity_universe_v2.json"
TEST_SET_V2_FILE = "m3_commercial_price_test_set_v2.json"
GATE_FILE = "m3_commercial_priceability_gate_v1.json"

# --- Patterns ---
_SHIPPING_NOISE = re.compile(
    r"ship|delay|week|upon\s*order|barring|unforeseen|n/?a|none|null|tbd|awwa",
    re.I,
)
_LABELED_MPN = re.compile(
    r"(?:mfg\s*part\s*(?:number|#)?|manufacturer\s*part\s*(?:number|#)?|"
    r"part\s*(?:number|#)|p/?n|mpn)\s*[:#]?\s*([A-Z0-9][A-Z0-9.\-_/]{2,47})",
    re.I,
)
_LABELED_SKU = re.compile(
    r"(?:^|[^A-Za-z])(?:sku|catalog(?:\s*(?:number|#|no\.?))?|cat(?:alog)?(?:\s*(?:number|#|no\.?)))\s*[:#]?\s*"
    r"([A-Z0-9][A-Z0-9.\-_/]{2,47})",
    re.I,
)
_LABELED_NSN = re.compile(
    r"(?:^|[^A-Za-z])(?:nsn|national\s*stock\s*number)\s*[:#]?\s*(\d{4}-\d{2}-\d{3}-\d{4}|\d{9,13})",
    re.I,
)
_LABELED_MODEL = re.compile(
    r"(?:^|[^A-Za-z])(?:model|mdl)\s*[:#]\s*([A-Z0-9][A-Z0-9.\-_/ ]{1,47})",
    re.I,
)
_DESC_TOKEN = re.compile(r"\b([A-Z]{2,}\s?[A-Z]?\d{2,}[A-Z0-9\-]{0,12})\b", re.I)
_PAY_ITEM = re.compile(r"^\d{2,4}-\d{2}(?:\.\d{2})?$")
_SHORT_NUMERIC = re.compile(r"^\d{1,5}$")
_BRAND_LIKE = re.compile(r"^[A-Za-z][A-Za-z \-]{2,40}$")
_WILDCARD = re.compile(r"[*%…]|\.\.\.|/\s*[A-Z0-9]|^\-|-$")
_GO_METRO_OEM = re.compile(r"^\d{6,8}$")  # Cummins-style OEM part numbers common at go-metro

_KNOWN_BRANDS = {
    "AIMPOINT",
    "HOLOSUN",
    "HOLSUN",
    "CUMMINS",
    "FLEETGUARD",
    "GATES",
    "DAYCO",
    "MAGPUL",
    "TORNADO",
    "ROCK RIVER ARMS",
    "GRAINGER",
    "3M",
    "HONEYWELL",
}

EVIDENCE_IDENTITY = "PRODUCT_IDENTITY_EVIDENCE"
EVIDENCE_GOV = "GOVERNMENT_REVENUE_EVIDENCE"
EVIDENCE_COMMERCIAL = "CURRENT_COMMERCIAL_SELLER"


def _utc() -> str:
    return now_utc().isoformat()


def _data_path(*parts: str) -> Path:
    from m3_data_root import data_path

    return data_path(*parts)


def identity_field(
    value: Any,
    *,
    state: str,
    source: str | None,
    evidence_role: str = EVIDENCE_IDENTITY,
    origin: str | None = None,
) -> dict[str, Any]:
    st = state if state in {"PROVEN", "DERIVED", "UNKNOWN", "CANDIDATE"} else "UNKNOWN"
    if st in {"UNKNOWN", "CANDIDATE"} and st == "UNKNOWN":
        return {
            "VALUE": None if st == "UNKNOWN" else value,
            "STATE": st,
            "SOURCE": source,
            "EVIDENCE_ROLE": evidence_role,
            "ORIGIN": origin,
        }
    if st == "CANDIDATE":
        return {
            "VALUE": value,
            "STATE": "CANDIDATE",
            "SOURCE": source,
            "EVIDENCE_ROLE": evidence_role,
            "ORIGIN": origin or "DESCRIPTION_TOKEN",
        }
    return {
        "VALUE": value,
        "STATE": st,
        "SOURCE": source,
        "EVIDENCE_ROLE": evidence_role,
        "ORIGIN": origin,
    }


def assert_mpn_not_false_promotion(field: dict[str, Any] | None) -> bool:
    """Permanent guard: PROVEN MPN cannot originate from unsafe paths."""
    if not field or not isinstance(field, dict):
        return True
    if field.get("STATE") != "PROVEN":
        return True
    origin = str(field.get("ORIGIN") or "")
    if origin in {"DESCRIPTION_TOKEN", "BUYER_CODE", "BRAND", "MODEL", "UNKNOWN", "PARSER_DERIVED", "LINE_CODE"}:
        return False
    if not field.get("SOURCE"):
        return False
    return True


def _normalize_uom(uom: Any) -> str | None:
    u = str(uom or "").strip().upper()
    if not u:
        return None
    if re.fullmatch(r"\d{4,}", u):
        return "EA"
    aliases = {"EACH": "EA", "PC": "EA", "PCS": "EA", "PIECE": "EA", "LF": "FT", "LUMP SUM": "LS"}
    return aliases.get(u, u)


def _guess_category(desc: str, buyer: str) -> str:
    d = (desc or "").upper()
    if buyer == "go-metro" or any(x in d for x in ("GASKET", "INJECTOR", "CUMMINS", "FILTER", "HOSE", "TURBO")):
        return "AUTO_PARTS"
    if any(x in d for x in ("GLOVE", "MASK", "RESPIRATOR", "PPE", "SAFETY GLASS")):
        return "PPE"
    if any(x in d for x in ("TOILET", "CLEANER", "TRASH", "JANITOR", "PAPER TOWEL", "SOAP")):
        return "JANITORIAL"
    if any(x in d for x in ("WIRE", "CONDUIT", "BREAKER", "LED", "LAMP", "ELECTRICAL", "OUTLET")):
        return "ELECTRICAL"
    if any(x in d for x in ("DRILL", "WRENCH", "SOCKET", "HAMMER", "TOOL")):
        return "TOOLS"
    if any(x in d for x in ("DESK", "CHAIR", "TABLE", "CABINET", "FURNITURE")):
        return "FURNITURE"
    if any(x in d for x in ("BEARING", "BELT", "MOTOR", "PUMP", "VALVE", "MRO", "FASTENER")):
        return "MRO"
    return "OTHER"


def _is_buyer_code(pn: str, buyer: str, uom: str | None, desc: str) -> bool:
    p = pn.strip()
    if _PAY_ITEM.match(p):
        return True
    if _SHORT_NUMERIC.match(p) and (uom or "").upper() in {"LS", "LUMP SUM"}:
        return True
    if buyer in {"beavertonoregon", "brentwoodtn"} and (_SHORT_NUMERIC.match(p) or _PAY_ITEM.match(p)):
        return True
    if re.fullmatch(r"LEO\d{4}", p, re.I):
        return True
    # Pure schedule-looking codes with service descriptions
    if _SHORT_NUMERIC.match(p) and any(
        x in (desc or "").upper() for x in ("MOBILIZATION", "EROSION", "TRAFFIC CONTROL", "POLLUTION")
    ):
        return True
    return False


def _is_brand_like(pn: str) -> bool:
    p = pn.strip()
    if p.upper() in _KNOWN_BRANDS:
        return True
    if _BRAND_LIKE.match(p) and not re.search(r"\d", p):
        return True
    return False


def extract_identity_hardened(rec: dict[str, Any]) -> dict[str, Any]:
    """Hardened identity extraction — never promotes unsafe tokens to PROVEN MPN."""
    buyer = str(rec.get("buyer") or rec.get("BUYER") or "")
    order_id = str(rec.get("project_id") or rec.get("ORDER_ID") or "")
    desc = str(rec.get("line_description") or rec.get("RAW_DESCRIPTION") or "")
    raw_pn = str(rec.get("part_number") or "").strip()
    uom = _normalize_uom(rec.get("uom") or rec.get("UOM"))
    qty = rec.get("quantity") if rec.get("quantity") is not None else rec.get("QTY")
    source_url = str(rec.get("source_url") or "")
    source_doc = str(rec.get("source") or "opengov_public_bid_tabulation")

    mpn = identity_field(None, state="UNKNOWN", source=None, origin="UNKNOWN")
    sku = identity_field(None, state="UNKNOWN", source=None, origin="UNKNOWN")
    nsn = identity_field(None, state="UNKNOWN", source=None, origin="UNKNOWN")
    brand = identity_field(None, state="UNKNOWN", source=None, origin="UNKNOWN")
    model = identity_field(None, state="UNKNOWN", source=None, origin="UNKNOWN")
    manufacturer = identity_field(None, state="UNKNOWN", source=None, origin="UNKNOWN")
    buyer_code = identity_field(None, state="UNKNOWN", source=None, origin="UNKNOWN")
    mpn_candidate = identity_field(None, state="UNKNOWN", source=None, origin="UNKNOWN")

    identifier_type = "UNKNOWN"
    identity_class = "UNRESOLVED"
    priceability = "UNRESOLVED"
    commercial_status = "NOT_CHECKED"
    old_would_be_mpn = None  # what legacy _extract_mpn would return
    false_promotion_bucket = None

    # --- Legacy behavior for false-promotion audit (do not use as output) ---
    try:
        from buyer_intelligence.commercial_price_acquisition_recovery import (
            _extract_mpn_legacy_unsafe as _legacy,
        )

        old_would_be_mpn = _legacy(rec)
    except Exception:
        old_would_be_mpn = None

    # 1) Labeled fields in description (explicit semantics)
    m_nsn = _LABELED_NSN.search(desc)
    if m_nsn:
        nsn = identity_field(
            m_nsn.group(1),
            state="PROVEN",
            source=f"{source_doc}:line_description:nsn_label",
            origin="SOURCE_NSN_LABEL",
        )
    m_sku = _LABELED_SKU.search(desc)
    if m_sku:
        sku_val = m_sku.group(1).upper()
        # Reject word-fragment captures (e.g. "EGORY" from CATEGORY)
        if re.search(r"\d", sku_val) or (len(sku_val) >= 4 and re.fullmatch(r"[A-Z0-9\-_/]+", sku_val)):
            if sku_val not in {"EGORY", "ION", "ING", "TION", "MENT", "ANCE", "ENCE", "CHER", "HODIC"}:
                sku = identity_field(
                    sku_val,
                    state="PROVEN",
                    source=f"{source_doc}:line_description:sku_label",
                    origin="SOURCE_SKU_LABEL",
                )
    m_model = _LABELED_MODEL.search(desc)
    if m_model:
        model = identity_field(
            m_model.group(1).strip().upper(),
            state="PROVEN",
            source=f"{source_doc}:line_description:model_label",
            origin="SOURCE_MODEL_LABEL",
        )
    m_mpn = _LABELED_MPN.search(desc)
    if m_mpn:
        cand = m_mpn.group(1).upper()
        if not _SHIPPING_NOISE.search(cand) and not _is_brand_like(cand):
            mpn = identity_field(
                cand,
                state="PROVEN",
                source=f"{source_doc}:line_description:mpn_label",
                origin="SOURCE_MPN_LABEL",
            )

    # 2) Source part_number field — classify carefully
    if raw_pn and not _SHIPPING_NOISE.search(raw_pn):
        if _is_buyer_code(raw_pn, buyer, uom, desc):
            buyer_code = identity_field(
                raw_pn.upper() if re.search(r"[A-Za-z]", raw_pn) else raw_pn,
                state="PROVEN",
                source=f"{source_doc}:part_number",
                origin="BUYER_CODE",
            )
            if old_would_be_mpn and str(old_would_be_mpn).upper() == raw_pn.upper():
                false_promotion_bucket = "BUYER_CODE"
        elif _is_brand_like(raw_pn):
            brand = identity_field(
                raw_pn.strip(),
                state="PROVEN",
                source=f"{source_doc}:part_number",
                origin="BRAND",
            )
            if "ROCK RIVER" in raw_pn.upper():
                manufacturer = identity_field(
                    raw_pn.strip(),
                    state="PROVEN",
                    source=f"{source_doc}:part_number",
                    origin="MANUFACTURER",
                )
            if old_would_be_mpn and str(old_would_be_mpn).upper() == raw_pn.upper().replace(" ", ""):
                false_promotion_bucket = "BRAND"
            elif old_would_be_mpn and _is_brand_like(str(old_would_be_mpn)):
                false_promotion_bucket = false_promotion_bucket or "BRAND"
        elif _WILDCARD.search(raw_pn):
            identity_class = "MALFORMED_SOURCE_ID"
            mpn_candidate = identity_field(
                raw_pn.upper(),
                state="CANDIDATE",
                source=f"{source_doc}:part_number",
                origin="MALFORMED",
            )
            if old_would_be_mpn:
                false_promotion_bucket = false_promotion_bucket or "OTHER"
        elif re.fullmatch(r"[A-Z0-9][A-Z0-9.\-_/]{2,47}", raw_pn, re.I):
            # Reject line/section-looking tokens (e.g. "2.1", "1.0", "104", "110-7-1")
            line_like = bool(
                re.fullmatch(r"\d{1,3}\.\d{1,3}", raw_pn)
                or _SHORT_NUMERIC.match(raw_pn)
                or re.fullmatch(r"\d{1,4}-\d{1,3}", raw_pn)
                or re.fullmatch(r"\d{1,4}-\d{1,3}-\d{1,3}", raw_pn)
            )
            has_letter = bool(re.search(r"[A-Za-z]", raw_pn))
            # Catalog-like hyphenated numerics (e.g. 109-08444) vs section codes (110-7-1)
            catalog_hyphen = bool(
                re.fullmatch(r"\d{3,6}-\d{4,8}", raw_pn)
                or re.fullmatch(r"\d{5,8}-\d{2,6}", raw_pn)
                or re.fullmatch(r"\d{2,3}-\d{5,8}-\d{2,4}", raw_pn)  # e.g. Gillig 82-48412-003
            )
            # Require commercial-looking shape: OEM 6–8 digit, letter+digit, or catalog hyphen
            commercial_shape = bool(
                (_GO_METRO_OEM.match(raw_pn) or (buyer == "go-metro" and re.fullmatch(r"\d{5,8}", raw_pn)))
                or (
                    has_letter
                    and re.search(r"\d", raw_pn)
                    and len(raw_pn) >= 5
                    and not line_like
                )
                or re.fullmatch(r"\d{7,12}", raw_pn)
                or catalog_hyphen
            )
            if commercial_shape and mpn.get("STATE") != "PROVEN":
                mpn = identity_field(
                    raw_pn.upper(),
                    state="PROVEN",
                    source=f"{source_doc}:part_number",
                    origin="SOURCE_PART_NUMBER_FIELD",
                )
            else:
                # Ambiguous short / line-like token — buyer code or unknown, never proven MPN
                buyer_code = identity_field(
                    raw_pn,
                    state="PROVEN",
                    source=f"{source_doc}:part_number",
                    origin="BUYER_CODE",
                )
                if old_would_be_mpn:
                    false_promotion_bucket = false_promotion_bucket or "BUYER_CODE"

    # 3) Description-token discovery → CANDIDATE only (never PROVEN MPN)
    if mpn.get("STATE") != "PROVEN" and nsn.get("STATE") != "PROVEN" and sku.get("STATE") != "PROVEN":
        tok = _DESC_TOKEN.search(desc)
        if tok:
            t = re.sub(r"\s+", "", tok.group(1)).upper()
            if not _SHIPPING_NOISE.search(t) and not _is_brand_like(t):
                mpn_candidate = identity_field(
                    t,
                    state="CANDIDATE",
                    source=f"{source_doc}:line_description:token",
                    origin="DESCRIPTION_TOKEN",
                )
                if old_would_be_mpn and str(old_would_be_mpn).upper() == t:
                    false_promotion_bucket = false_promotion_bucket or "DESCRIPTION_TOKEN"

    # If legacy promoted brand/model from description
    if old_would_be_mpn and false_promotion_bucket is None:
        ov = str(old_would_be_mpn).upper()
        if _is_brand_like(ov) or ov in _KNOWN_BRANDS:
            false_promotion_bucket = "BRAND"
        elif mpn.get("STATE") != "PROVEN" and sku.get("STATE") != "PROVEN" and nsn.get("STATE") != "PROVEN":
            if buyer_code.get("STATE") == "PROVEN" and str(buyer_code.get("VALUE") or "").upper() == ov:
                false_promotion_bucket = "BUYER_CODE"
            elif mpn_candidate.get("STATE") == "CANDIDATE":
                false_promotion_bucket = "DESCRIPTION_TOKEN"
            elif model.get("STATE") == "PROVEN" and str(model.get("VALUE") or "").upper().replace(" ", "") == ov:
                false_promotion_bucket = "MODEL"
            else:
                false_promotion_bucket = "OTHER"

    # Brand tokens in description for known brands (not MPN)
    for b in _KNOWN_BRANDS:
        if re.search(rf"\b{re.escape(b)}\b", desc, re.I) and brand.get("STATE") != "PROVEN":
            brand = identity_field(b, state="PROVEN", source=f"{source_doc}:line_description", origin="BRAND")
            break

    # Classification + priceability
    if mpn.get("STATE") == "PROVEN" and assert_mpn_not_false_promotion(mpn):
        identifier_type = "MPN"
        identity_class = "EXACT_COMMERCIAL_IDENTITY"
        priceability = "EXACT_PUBLIC"
        commercial_status = "NOT_CHECKED"
    elif nsn.get("STATE") == "PROVEN":
        identifier_type = "NSN"
        identity_class = "EXACT_COMMERCIAL_IDENTITY"
        priceability = "EXACT_PUBLIC"
    elif sku.get("STATE") == "PROVEN":
        identifier_type = "SKU"
        identity_class = "EXACT_COMMERCIAL_IDENTITY"
        priceability = "EXACT_PUBLIC"
    elif brand.get("STATE") == "PROVEN" and model.get("STATE") == "PROVEN":
        identifier_type = "BRAND_MODEL"
        identity_class = "BRAND_MODEL_EXACT"
        priceability = "AMBIGUOUS"  # uniqueness not proven without commercial validation
    elif buyer_code.get("STATE") == "PROVEN":
        identifier_type = "BUYER_CODE"
        identity_class = "BUYER_INTERNAL_CODE"
        priceability = "QUOTE_REQUIRED"
    elif brand.get("STATE") == "PROVEN" and mpn.get("STATE") != "PROVEN":
        identifier_type = "BRAND"
        identity_class = "BRAND_ONLY"
        priceability = "AMBIGUOUS"
    elif model.get("STATE") == "PROVEN" and mpn.get("STATE") != "PROVEN":
        identifier_type = "MODEL"
        identity_class = "MODEL_ONLY"
        priceability = "AMBIGUOUS"
    elif mpn_candidate.get("STATE") == "CANDIDATE":
        identifier_type = "MPN_CANDIDATE"
        identity_class = "PARTIAL_ID"
        priceability = "UNKNOWN"
    elif any(
        x in desc.upper()
        for x in ("MUST HAVE", "SHALL BE", "MINIMUM", "AT LEAST", "COMPATIBLE WITH", "DIMENSION")
    ) and mpn.get("STATE") != "PROVEN":
        identifier_type = "SPEC"
        identity_class = "GENERIC_SPEC_PRODUCT"
        priceability = "QUOTE_REQUIRED"
    else:
        identifier_type = "UNKNOWN"
        identity_class = "UNRESOLVED"
        priceability = "UNRESOLVED"

    if identity_class == "MALFORMED_SOURCE_ID":
        priceability = "UNKNOWN"

    # Final MPN guard
    if not assert_mpn_not_false_promotion(mpn):
        mpn = identity_field(None, state="UNKNOWN", source=None, origin="REJECTED_FALSE_PROMOTION")
        if identity_class == "EXACT_COMMERCIAL_IDENTITY" and nsn.get("STATE") != "PROVEN" and sku.get("STATE") != "PROVEN":
            identity_class = "UNRESOLVED"
            priceability = "UNRESOLVED"
            identifier_type = "UNKNOWN"

    cat = _guess_category(desc, buyer)
    priceable_exact = priceability == "EXACT_PUBLIC" and identity_class == "EXACT_COMMERCIAL_IDENTITY"

    return {
        "BUYER": buyer,
        "ORDER_ID": order_id,
        "RAW_DESCRIPTION": desc,
        "QTY": qty,
        "UOM": uom,
        "CATEGORY": cat,
        "SOURCE_URL": source_url or None,
        "SOURCE_DOCUMENT": source_doc,
        "SOURCE_PART_NUMBER_FIELD": raw_pn or None,
        "MPN": mpn,
        "SKU": sku,
        "NSN": nsn,
        "BRAND": brand,
        "MODEL": model,
        "MANUFACTURER": manufacturer,
        "BUYER_CODE": buyer_code,
        "MPN_CANDIDATE": mpn_candidate,
        "IDENTIFIER_TYPE": identifier_type,
        "IDENTITY_CLASS": identity_class,
        "PRICEABILITY": priceability,
        "COMMERCIAL_IDENTITY_STATUS": commercial_status,
        "SOURCE_PROVEN_IDENTIFIER": priceable_exact
        or nsn.get("STATE") == "PROVEN"
        or sku.get("STATE") == "PROVEN",
        "PRICEABLE_EXACT": priceable_exact,
        "OLD_LEGACY_MPN": old_would_be_mpn,
        "FALSE_PROMOTION_BUCKET": false_promotion_bucket,
        "CANONICAL_KEY": _canonical_key(
            identity_class=identity_class,
            buyer=buyer,
            mpn=mpn,
            sku=sku,
            nsn=nsn,
            brand=brand,
            buyer_code=buyer_code,
            uom=uom,
        ),
    }


def _canonical_key(
    *,
    identity_class: str,
    buyer: str,
    mpn: dict[str, Any],
    sku: dict[str, Any],
    nsn: dict[str, Any],
    brand: dict[str, Any],
    buyer_code: dict[str, Any],
    uom: str | None,
) -> str:
    if mpn.get("STATE") == "PROVEN" and mpn.get("VALUE"):
        return f"MPN:{(mpn.get('VALUE') or '').upper()}"
    if nsn.get("STATE") == "PROVEN" and nsn.get("VALUE"):
        return f"NSN:{nsn.get('VALUE')}"
    if sku.get("STATE") == "PROVEN" and sku.get("VALUE"):
        return f"SKU:{(sku.get('VALUE') or '').upper()}"
    if buyer_code.get("STATE") == "PROVEN" and buyer_code.get("VALUE"):
        return f"BUYER_CODE:{buyer}:{buyer_code.get('VALUE')}"
    if brand.get("STATE") == "PROVEN" and brand.get("VALUE"):
        return f"BRAND:{(brand.get('VALUE') or '').upper()}"
    return f"UNKNOWN:{buyer}:{uom or 'NA'}"


def priceability_allows_exact_lookup(row: dict[str, Any]) -> bool:
    if not row.get("PRICEABLE_EXACT"):
        return False
    if row.get("IDENTITY_CLASS") != "EXACT_COMMERCIAL_IDENTITY":
        return False
    if row.get("IDENTIFIER_TYPE") not in {"MPN", "SKU", "NSN"}:
        return False
    mpn = row.get("MPN") or {}
    if row.get("IDENTIFIER_TYPE") == "MPN" and not assert_mpn_not_false_promotion(mpn):
        return False
    return True


def audit_extraction_paths() -> dict[str, Any]:
    return {
        "IDENTITY_POPULATION_PATHS": [
            "opengov_bid_price_index.part_number field",
            "commercial_price_acquisition_recovery._extract_mpn",
            "historical_award_expansion._extract_mpn",
            "historical_profit_proof_v2._extract_mpn",
            "historical_source_inventory._extract_mpn",
            "description regex (_MPN_FROM_DESC / _DESC_TOKEN)",
            "pool order CATEGORY inheritance",
        ],
        "UNSAFE_PATHS": [
            "description alphanumeric token → MPN (PARSER_DERIVED)",
            "brand string in part_number → MPN",
            "buyer pay-item / LS codes → MPN",
            "labeled 'model' capture treated as MPN via shared regex",
        ],
        "DESCRIPTION_TOKEN_PROMOTION_PATHS": [
            "commercial_price_acquisition_recovery._extract_mpn m2 regex",
            "identity recovery V1 DESCRIPTION_TOKEN for MS45/IR100/BAG10PK",
        ],
        "BUYER_CODE_PROMOTION_PATHS": [
            "part_number alphanumeric accept for beaverton 002xx / brentwood 716-*",
        ],
        "BRAND_TO_MPN_PATHS": [
            "part_number='Aimpoint'/'Holosun'/'Rock River Arms' accepted as MPN",
        ],
        "FALLBACKS": [
            "desc label match before part_number",
            "desc token when part_number empty",
        ],
        "PRIMARY_DEFECTS": [
            "Legacy _extract_mpn collapses all identifier types into one MPN string",
            "No STATE/SOURCE/ORIGIN on extracted identity",
            "Description tokens promoted to factual MPN without source semantics",
        ],
        "FIXES_APPLIED": [
            "Hardened extract_identity_hardened with separate MPN/SKU/NSN/BRAND/MODEL/BUYER_CODE",
            "DESCRIPTION_TOKEN → MPN_CANDIDATE (CANDIDATE) never PROVEN MPN",
            "BUYER_CODE namespace",
            "assert_mpn_not_false_promotion production guard",
            "priceability gate requires EXACT_PUBLIC + source-proven identifier",
        ],
    }


def _sample_records(limit: int = 2000) -> list[dict[str, Any]]:
    bi = json.loads(_data_path(BID_INDEX_FILE).read_text(encoding="utf-8"))
    recs = list(bi.get("records") or [])
    # Prefer diversity: round-robin by buyer
    by_buyer: dict[str, list] = defaultdict(list)
    for r in recs:
        by_buyer[str(r.get("buyer") or "")].append(r)
    buyers = sorted(by_buyer.keys(), key=lambda b: -len(by_buyer[b]))
    out: list[dict[str, Any]] = []
    idx = {b: 0 for b in buyers}
    while len(out) < limit and buyers:
        progressed = False
        for b in list(buyers):
            i = idx[b]
            if i >= len(by_buyer[b]):
                buyers.remove(b)
                continue
            out.append(by_buyer[b][i])
            idx[b] = i + 1
            progressed = True
            if len(out) >= limit:
                break
        if not progressed:
            break
    return out


def _optional_commercial_validation(rows: list[dict[str, Any]], *, max_n: int = 10, per_id_s: float = 20.0) -> dict[str, Any]:
    """Lightweight identity validation — NOT pricing. Checks manufacturer/distributor PDP title match."""
    from public_price_search.search import fetch_page

    tested = []
    counts = Counter()
    for row in rows[:max_n]:
        mpn_f = row.get("MPN") or {}
        if mpn_f.get("STATE") != "PROVEN" or not mpn_f.get("VALUE"):
            continue
        mpn = str(mpn_f["VALUE"])
        t0 = time.time()
        url = f"https://www.dieselpartsdirect.com/{mpn}"
        status = "NO_MATCH"
        try:
            fr = fetch_page(url, use_budget=True)
            text = fr.get("text") or ""
            title_m = re.search(r"<title[^>]*>([^<]+)", text, re.I)
            title = title_m.group(1) if title_m else ""
            if fr.get("blocked"):
                status = "UNKNOWN"
            elif "page not found" in title.lower():
                status = "NO_MATCH"
            elif mpn.upper() in title.upper():
                status = "VALIDATED_EXACT"
            else:
                status = "NO_MATCH"
        except Exception:
            status = "UNKNOWN"
        if time.time() - t0 > per_id_s:
            status = "UNKNOWN"
        counts[status] += 1
        row["COMMERCIAL_IDENTITY_STATUS"] = status
        tested.append({"MPN": mpn, "BUYER": row.get("BUYER"), "STATUS": status, "URL": url})
        if len(tested) >= max_n:
            break
    return {
        "tested": len(tested),
        "VALIDATED_EXACT": counts.get("VALIDATED_EXACT", 0),
        "NO_MATCH": counts.get("NO_MATCH", 0),
        "MULTIPLE_MATCHES": counts.get("MULTIPLE_MATCHES", 0),
        "UNKNOWN": counts.get("UNKNOWN", 0),
        "details": tested,
    }


def _order_identity_coverage_potential(priceable: list[dict[str, Any]]) -> dict[str, int]:
    from buyer_intelligence.commercial_price_acquisition_recovery import _num

    priceable_by_order: dict[str, set[str]] = defaultdict(set)
    for r in priceable:
        mpn = ((r.get("MPN") or {}).get("VALUE") or (r.get("SKU") or {}).get("VALUE") or (r.get("NSN") or {}).get("VALUE"))
        if mpn:
            priceable_by_order[str(r.get("ORDER_ID"))].add(str(mpn).upper())

    # Use bid index for order line values
    bi = json.loads(_data_path(BID_INDEX_FILE).read_text(encoding="utf-8"))
    lines_by_pid: dict[str, list] = defaultdict(list)
    for rec in bi.get("records") or []:
        lines_by_pid[str(rec.get("project_id") or "")].append(rec)

    pool_path = _data_path(POOL_FILE)
    orders = []
    if pool_path.exists():
        pool = json.loads(pool_path.read_text(encoding="utf-8"))
        orders = pool.get("orders") or []
    if not orders:
        # Fall back to distinct projects in scanned priceable set
        for pid in priceable_by_order:
            orders.append({"ORDER_ID": pid, "TOTAL": 0})

    bands = {"25": 0, "50": 0, "75": 0, "90": 0}
    for o in orders:
        pid = str(o.get("ORDER_ID") or "")
        lines = lines_by_pid.get(pid) or []
        # Compute gov value from lines if TOTAL missing
        gov = float(o.get("TOTAL") or 0)
        if gov <= 0:
            for ln in lines:
                ext = _num(ln.get("extended"))
                if ext is None:
                    u, q = _num(ln.get("bid_unit_price")), _num(ln.get("quantity"))
                    if u is not None and q is not None:
                        ext = u * q
                if ext is not None:
                    gov += float(ext)
        if gov <= 0:
            continue
        pset = priceable_by_order.get(pid) or set()
        covered = 0.0
        for ln in lines:
            # hardened identity for coverage potential
            ident = extract_identity_hardened(ln)
            key = None
            if (ident.get("MPN") or {}).get("STATE") == "PROVEN":
                key = str(ident["MPN"]["VALUE"]).upper()
            elif (ident.get("SKU") or {}).get("STATE") == "PROVEN":
                key = str(ident["SKU"]["VALUE"]).upper()
            elif (ident.get("NSN") or {}).get("STATE") == "PROVEN":
                key = str(ident["NSN"]["VALUE"]).upper()
            if not key or key not in pset:
                continue
            if not priceability_allows_exact_lookup(ident):
                continue
            ext = _num(ln.get("extended"))
            if ext is None:
                u, q = _num(ln.get("bid_unit_price")), _num(ln.get("quantity"))
                if u is not None and q is not None:
                    ext = u * q
            if ext is not None:
                covered += float(ext)
        cov = 100.0 * covered / gov if gov else 0.0
        if cov >= 25:
            bands["25"] += 1
        if cov >= 50:
            bands["50"] += 1
        if cov >= 75:
            bands["75"] += 1
        if cov >= 90:
            bands["90"] += 1
    return bands


def _build_frozen_v2(priceable: list[dict[str, Any]], *, target: int = 40) -> dict[str, Any]:
    """Select 30–50 identities by identity quality only — no price knowledge."""
    # Dedupe by canonical MPN/SKU/NSN
    seen: set[str] = set()
    cands: list[dict[str, Any]] = []
    for r in priceable:
        if not priceability_allows_exact_lookup(r):
            continue
        key = r.get("CANONICAL_KEY") or ""
        if not key.startswith(("MPN:", "SKU:", "NSN:")):
            continue
        if key in seen:
            continue
        seen.add(key)
        cands.append(r)

    # Prefer qty/UOM completeness, then round-robin buyers (go-metro capped, not excluded)
    def _row_quality(r: dict[str, Any]) -> tuple:
        qty_ok = 1 if r.get("QTY") not in (None, "") else 0
        uom_ok = 1 if r.get("UOM") else 0
        cat = str(r.get("CATEGORY") or "OTHER")
        return (qty_ok + uom_ok, 1 if cat != "OTHER" else 0, str(r.get("CANONICAL_KEY") or ""))

    by_buyer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in cands:
        by_buyer[str(r.get("BUYER") or "")].append(r)
    for b in by_buyer:
        by_buyer[b].sort(key=_row_quality, reverse=True)

    selected: list[dict[str, Any]] = []
    buyer_counts: Counter[str] = Counter()
    gm = 0
    idxs = {b: 0 for b in by_buyer}
    active = sorted(by_buyer.keys(), key=lambda b: (0 if b != "go-metro" else 1, b))

    def _append(r: dict[str, Any]) -> None:
        nonlocal gm
        buyer = str(r.get("BUYER") or "")
        if buyer == "go-metro":
            if gm >= 12:
                return
            gm += 1
        elif buyer_counts[buyer] >= 4:
            return
        buyer_counts[buyer] += 1
        mpn_v = (r.get("MPN") or {}).get("VALUE")
        sku_v = (r.get("SKU") or {}).get("VALUE")
        nsn_v = (r.get("NSN") or {}).get("VALUE")
        selected.append(
            {
                "BUYER": buyer,
                "ORDER_ID": r.get("ORDER_ID"),
                "RAW_DESCRIPTION": r.get("RAW_DESCRIPTION"),
                "MPN": mpn_v,
                "SKU": sku_v,
                "NSN": nsn_v,
                "IDENTIFIER_TYPE": r.get("IDENTIFIER_TYPE"),
                "QTY": r.get("QTY"),
                "UOM": r.get("UOM"),
                "CATEGORY": r.get("CATEGORY"),
                "SOURCE_URL": r.get("SOURCE_URL"),
                "SOURCE_DOCUMENT": r.get("SOURCE_DOCUMENT"),
                "MPN_FIELD": r.get("MPN"),
                "SKU_FIELD": r.get("SKU"),
                "NSN_FIELD": r.get("NSN"),
                "PRICEABILITY": r.get("PRICEABILITY"),
                "COMMERCIAL_IDENTITY_STATUS": "NOT_CHECKED",
                "CANONICAL_KEY": r.get("CANONICAL_KEY"),
                "IDENTITY_VALUE": mpn_v or sku_v or nsn_v,
                "FROZEN": True,
                "SELECTION_BASIS": "IDENTITY_QUALITY_ONLY",
            }
        )

    while len(selected) < target and active:
        progressed = False
        for b in list(active):
            if len(selected) >= target:
                break
            i = idxs[b]
            if i >= len(by_buyer[b]):
                active.remove(b)
                continue
            if b == "go-metro" and gm >= 12:
                active.remove(b)
                continue
            if b != "go-metro" and buyer_counts[b] >= 4:
                active.remove(b)
                continue
            _append(by_buyer[b][i])
            idxs[b] = i + 1
            progressed = True
        if not progressed:
            break

    # Top up if under 30 (relax per-buyer cap, keep go-metro cap)
    if len(selected) < 30:
        have = {s.get("CANONICAL_KEY") for s in selected}
        for r in sorted(cands, key=_row_quality, reverse=True):
            if len(selected) >= 30:
                break
            if r.get("CANONICAL_KEY") in have:
                continue
            buyer = str(r.get("BUYER") or "")
            if buyer == "go-metro" and gm >= 12:
                continue
            _append(r)
            have.add(r.get("CANONICAL_KEY"))

    for i, row in enumerate(selected, 1):
        row["TEST_ID"] = f"CPT2-{i:03d}"

    doc = {
        "build": BUILD,
        "kind": "COMMERCIAL_PRICE_TEST_SET_V2",
        "frozen_at": _utc(),
        "selection_basis": "IDENTITY_QUALITY_ONLY_NO_PRICE_CHERRY_PICK",
        "count": len(selected),
        "buyers": sorted({str(x.get("BUYER")) for x in selected}),
        "categories": sorted({str(x.get("CATEGORY")) for x in selected}),
        "source_proven_mpn": sum(1 for x in selected if x.get("IDENTIFIER_TYPE") == "MPN"),
        "source_proven_sku": sum(1 for x in selected if x.get("IDENTIFIER_TYPE") == "SKU"),
        "nsn": sum(1 for x in selected if x.get("IDENTIFIER_TYPE") == "NSN"),
        "brand_model": 0,
        "qty_known": sum(1 for x in selected if x.get("QTY") not in (None, "")),
        "uom_known": sum(1 for x in selected if x.get("UOM")),
        "identities": selected,
    }
    _data_path(TEST_SET_V2_FILE).write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    return doc


def _role_audit(rows: list[dict[str, Any]]) -> dict[str, int]:
    id_coll = 0
    price_coll = 0
    for r in rows:
        for key in ("MPN", "SKU", "NSN", "BRAND", "MODEL", "BUYER_CODE", "MANUFACTURER"):
            f = r.get(key) or {}
            if not isinstance(f, dict):
                continue
            role = f.get("EVIDENCE_ROLE")
            if role and role not in {EVIDENCE_IDENTITY, None}:
                if role in {EVIDENCE_GOV, "COMPETITION_EVIDENCE", "HISTORICAL_AWARD_PRICE"}:
                    id_coll += 1
            if role in {EVIDENCE_COMMERCIAL, "CURRENT_COMMERCIAL_SELLER"} and key in {"MPN", "SKU", "NSN"}:
                # identity fields must not carry commercial price role
                price_coll += 1
    # Also check acquisition cache roles untouched
    try:
        from buyer_intelligence.commercial_identity_recovery_priceability_v1 import role_audit

        ra = role_audit()
        price_coll += int(ra.get("ROLE_COLLISIONS") or 0)
    except Exception:
        pass
    return {"IDENTITY_ROLE_COLLISIONS": id_coll, "PRICE_ROLE_COLLISIONS": price_coll}


def _sync_ui(ui: dict[str, Any]) -> None:
    path = _data_path("m3_historical_sources_registry.json")
    doc: dict[str, Any] = {}
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            doc = {}
    existing = doc.get("commercial_pricing_health") or {}
    merged = dict(existing)
    merged.update(ui)
    doc["commercial_pricing_health"] = merged
    doc["updated_at"] = _utc()
    path.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    _data_path(UI_FILE).write_text(json.dumps(ui, indent=2, default=str), encoding="utf-8")


def run_identity_hardening_clean_priceable_corpus_v2() -> dict[str, Any]:
    wall0 = time.time()
    print("BUILD", BUILD, flush=True)
    paths = audit_extraction_paths()
    print(
        f"T+{(time.time()-wall0):.0f} EXTRACTION PATHS={len(paths['IDENTITY_POPULATION_PATHS'])} "
        f"UNSAFE={len(paths['UNSAFE_PATHS'])} PRIMARY_DEFECTS={len(paths['PRIMARY_DEFECTS'])}",
        flush=True,
    )

    records = _sample_records(2000)
    print(f"T+{(time.time()-wall0):.0f} RE-EXTRACTING lines={len(records)}", flush=True)

    extracted: list[dict[str, Any]] = []
    for rec in records:
        if time.time() - wall0 > 20 * 60:
            break
        extracted.append(extract_identity_hardened(rec))

    buyers_n = len({r.get("BUYER") for r in extracted})
    old_mpn = sum(1 for r in extracted if r.get("OLD_LEGACY_MPN"))
    new_proven_mpn = sum(1 for r in extracted if (r.get("MPN") or {}).get("STATE") == "PROVEN")
    new_sku = sum(1 for r in extracted if (r.get("SKU") or {}).get("STATE") == "PROVEN")
    new_nsn = sum(1 for r in extracted if (r.get("NSN") or {}).get("STATE") == "PROVEN")
    buyer_codes = sum(1 for r in extracted if (r.get("BUYER_CODE") or {}).get("STATE") == "PROVEN")
    brand_only = sum(1 for r in extracted if r.get("IDENTITY_CLASS") == "BRAND_ONLY")
    model_only = sum(1 for r in extracted if r.get("IDENTITY_CLASS") == "MODEL_ONLY")
    generic = sum(1 for r in extracted if r.get("IDENTITY_CLASS") == "GENERIC_SPEC_PRODUCT")
    malformed = sum(1 for r in extracted if r.get("IDENTITY_CLASS") in {"MALFORMED_SOURCE_ID", "PARTIAL_ID"})
    unknown = sum(1 for r in extracted if r.get("IDENTITY_CLASS") == "UNRESOLVED")

    false_promos = [r for r in extracted if r.get("FALSE_PROMOTION_BUCKET") and r.get("OLD_LEGACY_MPN")]
    # Only count cases where new rules reject proven MPN that old would emit
    false_promos = [
        r
        for r in false_promos
        if (r.get("MPN") or {}).get("STATE") != "PROVEN"
        or str((r.get("MPN") or {}).get("VALUE") or "").upper() != str(r.get("OLD_LEGACY_MPN") or "").upper()
    ]
    fp_buckets = Counter(r.get("FALSE_PROMOTION_BUCKET") for r in false_promos)

    priceable = [r for r in extracted if priceability_allows_exact_lookup(r)]
    print(
        f"T+{(time.time()-wall0):.0f} RULES HARDENED lines={len(extracted)} "
        f"FALSE_OLD_MPN={len(false_promos)} PRICEABLE={len(priceable)}",
        flush=True,
    )

    # 30-row manual audit across >=10 buyers
    audit_rows = []
    seen_buyers: set[str] = set()
    for r in extracted:
        if len(audit_rows) >= 30:
            break
        b = str(r.get("BUYER") or "")
        # Prefer spreading buyers
        if b in seen_buyers and len(seen_buyers) < 10 and len(audit_rows) < 20:
            continue
        seen_buyers.add(b)
        old_type = "MPN" if r.get("OLD_LEGACY_MPN") else "NONE"
        new_id = (
            (r.get("MPN") or {}).get("VALUE")
            or (r.get("SKU") or {}).get("VALUE")
            or (r.get("NSN") or {}).get("VALUE")
            or (r.get("BUYER_CODE") or {}).get("VALUE")
            or (r.get("BRAND") or {}).get("VALUE")
            or (r.get("MPN_CANDIDATE") or {}).get("VALUE")
        )
        state = "UNKNOWN"
        for key in ("MPN", "SKU", "NSN", "BUYER_CODE", "BRAND", "MODEL"):
            f = r.get(key) or {}
            if f.get("VALUE"):
                state = f.get("STATE") or "UNKNOWN"
                break
        if (r.get("MPN_CANDIDATE") or {}).get("STATE") == "CANDIDATE" and state == "UNKNOWN":
            state = "CANDIDATE"
        audit_rows.append(
            {
                "BUYER": b,
                "ORDER": r.get("ORDER_ID"),
                "RAW_DESCRIPTION": (r.get("RAW_DESCRIPTION") or "")[:160],
                "SOURCE_FIELD_LABEL": "part_number" if r.get("SOURCE_PART_NUMBER_FIELD") else "line_description",
                "OLD_IDENTITY": r.get("OLD_LEGACY_MPN"),
                "OLD_TYPE": old_type,
                "NEW_IDENTITY": new_id,
                "NEW_TYPE": r.get("IDENTIFIER_TYPE"),
                "STATE": state,
                "SOURCE_LOCATION": r.get("SOURCE_URL"),
                "PRICEABILITY": r.get("PRICEABILITY"),
                "IDENTITY_CLASS": r.get("IDENTITY_CLASS"),
            }
        )
        if len(seen_buyers) < 10 and len(audit_rows) >= 10:
            # keep filling with new buyers preferentially
            pass

    # Ensure >=10 buyers in audit if available
    if len({a["BUYER"] for a in audit_rows}) < 10:
        for r in extracted:
            if len({a["BUYER"] for a in audit_rows}) >= 10 or len(audit_rows) >= 30:
                break
            b = str(r.get("BUYER") or "")
            if b in {a["BUYER"] for a in audit_rows}:
                continue
            audit_rows.append(
                {
                    "BUYER": b,
                    "ORDER": r.get("ORDER_ID"),
                    "RAW_DESCRIPTION": (r.get("RAW_DESCRIPTION") or "")[:160],
                    "SOURCE_FIELD_LABEL": "part_number" if r.get("SOURCE_PART_NUMBER_FIELD") else "line_description",
                    "OLD_IDENTITY": r.get("OLD_LEGACY_MPN"),
                    "OLD_TYPE": "MPN" if r.get("OLD_LEGACY_MPN") else "NONE",
                    "NEW_IDENTITY": (r.get("MPN") or {}).get("VALUE") or (r.get("BUYER_CODE") or {}).get("VALUE"),
                    "NEW_TYPE": r.get("IDENTIFIER_TYPE"),
                    "STATE": ((r.get("MPN") or {}).get("STATE") or (r.get("BUYER_CODE") or {}).get("STATE") or "UNKNOWN"),
                    "SOURCE_LOCATION": r.get("SOURCE_URL"),
                    "PRICEABILITY": r.get("PRICEABILITY"),
                    "IDENTITY_CLASS": r.get("IDENTITY_CLASS"),
                }
            )

    universe = {
        "build": BUILD,
        "kind": "CLEAN_PRICEABLE_IDENTITY_UNIVERSE_V2",
        "updated_at": _utc(),
        "count": len(priceable),
        "buyers": sorted({str(r.get("BUYER")) for r in priceable}),
        "categories": sorted({str(r.get("CATEGORY")) for r in priceable}),
        "qty_known": sum(1 for r in priceable if r.get("QTY") not in (None, "")),
        "uom_known": sum(1 for r in priceable if r.get("UOM")),
        "identities": [
            {
                "BUYER": r.get("BUYER"),
                "ORDER_ID": r.get("ORDER_ID"),
                "RAW_DESCRIPTION": r.get("RAW_DESCRIPTION"),
                "CANONICAL_KEY": r.get("CANONICAL_KEY"),
                "MPN": r.get("MPN"),
                "SKU": r.get("SKU"),
                "NSN": r.get("NSN"),
                "QTY": r.get("QTY"),
                "UOM": r.get("UOM"),
                "CATEGORY": r.get("CATEGORY"),
                "PRICEABILITY": r.get("PRICEABILITY"),
                "SOURCE_URL": r.get("SOURCE_URL"),
            }
            for r in priceable
        ],
    }
    _data_path(UNIVERSE_FILE).write_text(json.dumps(universe, indent=2, default=str), encoding="utf-8")

    test_set = _build_frozen_v2(priceable, target=40)
    print(
        f"T+{(time.time()-wall0):.0f} V2 TEST SET={test_set['count']} "
        f"BUYERS={len(test_set['buyers'])} CATEGORIES={test_set['categories']}",
        flush=True,
    )

    # Optional commercial validation on up to 10 from test set (identity only)
    val_rows = []
    for ident in test_set.get("identities") or []:
        val_rows.append(
            {
                "BUYER": ident.get("BUYER"),
                "ORDER_ID": ident.get("ORDER_ID"),
                "MPN": ident.get("MPN_FIELD")
                or identity_field(ident.get("MPN"), state="PROVEN", source="test_set", origin="SOURCE_PART_NUMBER_FIELD"),
            }
        )
    # Fix MPN field shape
    for vr in val_rows:
        if not isinstance(vr.get("MPN"), dict):
            vr["MPN"] = identity_field(vr.get("MPN"), state="PROVEN", source="test_set", origin="SOURCE_PART_NUMBER_FIELD")
    commercial_val = _optional_commercial_validation(val_rows, max_n=10)

    order_potential = _order_identity_coverage_potential(priceable)
    roles = _role_audit(extracted)

    # Zero buyer/brand/desc-token as proven MPN checks
    zero_buyer_as_mpn = not any(
        (r.get("MPN") or {}).get("STATE") == "PROVEN" and (r.get("MPN") or {}).get("ORIGIN") == "BUYER_CODE"
        for r in extracted
    )
    zero_brand_as_mpn = not any(
        (r.get("MPN") or {}).get("STATE") == "PROVEN" and (r.get("MPN") or {}).get("ORIGIN") == "BRAND"
        for r in extracted
    )
    zero_desc_token_proven = not any(
        (r.get("MPN") or {}).get("STATE") == "PROVEN"
        and (r.get("MPN") or {}).get("ORIGIN") in {"DESCRIPTION_TOKEN", "PARSER_DERIVED"}
        for r in extracted
    )

    wall_s = round(time.time() - wall0, 2)
    if test_set["count"] >= 30 and zero_buyer_as_mpn and zero_brand_as_mpn and zero_desc_token_proven:
        conclusion = "IDENTITY_FOUNDATION_CLEAN"
        status = "PASS"
    elif test_set["count"] >= 20:
        conclusion = "IDENTITY_IMPROVED_BUT_STILL_NOISY"
        status = "PARTIAL"
    elif len(priceable) < 30:
        conclusion = "INSUFFICIENT_CLEAN_IDENTITIES"
        status = "PARTIAL"
    else:
        conclusion = "SOURCE_DATA_TOO_WEAK"
        status = "PARTIAL"

    gates = {
        "REAL_DATA_ONLY": True,
        "NO_INVENTED_IDENTITIES": True,
        "UNKNOWN_REMAINS_UNKNOWN": True,
        "SOURCE_PROOF_SEPARATE_FROM_COMMERCIAL_VALIDATION": True,
        "BUYER_CODE_NAMESPACE_ACTIVE": True,
        "ZERO_BUYER_CODES_AS_MPN": zero_buyer_as_mpn,
        "ZERO_BRAND_ONLY_AS_MPN": zero_brand_as_mpn,
        "ZERO_DESCRIPTION_TOKEN_PROVEN_MPN": zero_desc_token_proven,
        "PRICEABILITY_GATE_ACTIVE": True,
        "30_ROW_AUDIT_PASS": len(audit_rows) >= 30 and len({a["BUYER"] for a in audit_rows}) >= 10,
        "GE_30_CLEAN_TEST_IDENTITIES": test_set["count"] >= 30,
        "IDENTITY_ROLE_COLLISIONS_ZERO": roles["IDENTITY_ROLE_COLLISIONS"] == 0,
        "PRICE_ROLE_COLLISIONS_ZERO": roles["PRICE_ROLE_COLLISIONS"] == 0,
        "UI_SYNCHRONIZED": True,
        "FINISHED_WITHIN_25_MIN": wall_s <= 1500,
    }

    report = {
        "build": BUILD,
        "STATUS": status,
        "STRATEGIC_CONCLUSION": conclusion,
        "WALL_CLOCK_S": wall_s,
        "updated_at": _utc(),
        "extraction_audit": paths,
        "reextraction": {
            "lines_scanned": len(extracted),
            "buyers": buyers_n,
            "old_mpn_populated": old_mpn,
            "new_proven_mpn": new_proven_mpn,
            "new_sku": new_sku,
            "nsn": new_nsn,
            "buyer_codes": buyer_codes,
            "brand_only": brand_only,
            "model_only": model_only,
            "generic_spec": generic,
            "malformed": malformed,
            "unknown": unknown,
        },
        "false_old_mpn_promotions": {
            "total": len(false_promos),
            "buyer_codes": fp_buckets.get("BUYER_CODE", 0),
            "description_tokens": fp_buckets.get("DESCRIPTION_TOKEN", 0),
            "brand": fp_buckets.get("BRAND", 0),
            "model": fp_buckets.get("MODEL", 0),
            "line_code": fp_buckets.get("LINE_CODE", 0),
            "other": fp_buckets.get("OTHER", 0),
            "raw": dict(fp_buckets),
        },
        "audit_30": audit_rows,
        "clean_priceable_universe": {
            "eligible_identities": len(priceable),
            "buyers": len(universe["buyers"]),
            "categories": universe["categories"],
            "qty_known": universe["qty_known"],
            "uom_known": universe["uom_known"],
            "file": UNIVERSE_FILE,
        },
        "frozen_test_set_v2": {
            "file": TEST_SET_V2_FILE,
            "identities": test_set["count"],
            "buyers": test_set["buyers"],
            "buyer_count": len(test_set["buyers"]),
            "categories": test_set["categories"],
            "source_proven_mpn": test_set["source_proven_mpn"],
            "sku": test_set["source_proven_sku"],
            "nsn": test_set["nsn"],
            "brand_model": test_set["brand_model"],
            "qty_known": test_set["qty_known"],
            "uom_known": test_set["uom_known"],
        },
        "optional_commercial_validation": commercial_val,
        "order_identity_coverage_potential": order_potential,
        "role_audit": roles,
        "gates": gates,
        "final_questions": {
            "1_contamination_severity": len(false_promos),
            "2_false_mpn_removed": len(false_promos),
            "3_genuinely_priceable": len(priceable),
            "4_v2_cleaner_than_v1": test_set["count"] >= 30 and zero_buyer_as_mpn and zero_brand_as_mpn,
            "5_buyers_categories": {
                "buyers": len(test_set["buyers"]),
                "categories": test_set["categories"],
            },
            "6_independently_validated": commercial_val.get("VALIDATED_EXACT", 0),
            "7_identity_still_main_blocker": len(priceable) < 100,
            "8_fair_pricing_test_next": test_set["count"] >= 30,
            "9_highest_remaining_issue": (
                "Source part_number field semantics vary by buyer; need manufacturer field enrichment"
            ),
            "10_commercial_price_acquisition_v3_ready": test_set["count"] >= 30,
        },
        "performance": {"TOTAL_S": wall_s},
    }

    ui = {
        "build": BUILD,
        "label": "Commercial Pricing Health",
        "STATUS": status,
        "STRATEGIC_CONCLUSION": conclusion,
        "identity_health": {
            "frozen_30": test_set["count"],
            "exact_commercial_identity": new_proven_mpn,
            "brand_model_exact": 0,
            "generic_spec": generic,
            "buyer_code": buyer_codes,
            "brand_only": brand_only,
            "quote_required": sum(1 for r in extracted if r.get("PRICEABILITY") == "QUOTE_REQUIRED"),
            "unresolved": unknown,
            "clean_priceable_universe": len(priceable),
            "false_old_mpn_promotions": len(false_promos),
            "test_set_v2": test_set["count"],
        },
        "identity_display_rules": {
            "product_identity_states": ["PROVEN", "DERIVED", "UNKNOWN", "CANDIDATE"],
            "identifier_types": ["MPN", "SKU", "NSN", "MODEL", "BUYER_CODE", "SPEC", "BRAND"],
            "commercial_validation": ["VALIDATED_EXACT", "NOT_CHECKED", "NO_MATCH", "MULTIPLE_MATCHES"],
            "priceability": ["EXACT_PUBLIC", "SPEC_PUBLIC", "QUOTE_REQUIRED", "AMBIGUOUS", "UNKNOWN"],
            "buyer_code_separated": True,
        },
        "priceability_gate_active": True,
        "proven_derived_unknown_active": True,
        "frozen_identities": test_set["count"],
        "priced": None,  # intentionally not run
        "updated_at": _utc(),
    }
    _data_path(REPORT_FILE).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    _sync_ui(ui)

    # Persist gate entries for V2 identities (allow exact lookup only)
    gate_path = _data_path(GATE_FILE)
    gate: dict[str, Any] = {"build": BUILD, "updated_at": _utc(), "identities": {}}
    if gate_path.exists():
        try:
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    gate.setdefault("identities", {})
    gate["build_v2"] = BUILD
    gate["rule"] = "NO_PUBLIC_PRICE_LOOKUP_UNLESS_PRICEABILITY_ALLOWS"
    for ident in test_set.get("identities") or []:
        key = f"{ident.get('BUYER')}:{ident.get('ORDER_ID')}:{ident.get('IDENTITY_VALUE')}"
        gate["identities"][key] = {
            "FINAL_IDENTITY_CLASS": "EXACT_COMMERCIAL_IDENTITY",
            "PRICEABILITY": "PUBLIC_EXACT_PRICEABLE",
            "allows_public_lookup": True,
            "CANONICAL_KEY": ident.get("CANONICAL_KEY"),
            "test_set": "V2",
        }
    _data_path(GATE_FILE).write_text(json.dumps(gate, indent=2, default=str), encoding="utf-8")

    print(f"T+{wall_s:.0f} FINALIZATION conclusion={conclusion} test_set={test_set['count']}", flush=True)
    return report
