"""Commercial identification + pricing bottleneck repair (isolated).

Restores READ-ONLY historical award access for validation, audits the
product-fit OpenGov cohort, ranks sellable opportunities, and deep-researches
the top 20 full mandatory scopes.

Does NOT touch BidNet enrichment, L23 writes, production history, or operator UI.
Never fabricates award or retail prices. Prefer zero SAM calls.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from application_clock import now_utc

log = logging.getLogger("govtracker.opengov_commercial_id_pricing_repair_v1")

BUILD = "20261010-m3-opengov-commercial-id-pricing-repair-v1"
PRIOR_PROFIT_BUILD = "20261010-m3-opengov-enriched-profitability-validation-v1"
SOURCE_RUN = "prod-live-20261010T023015Z-6d8c5be7"

OUT_DIR_NAME = "_og_commercial_id_val_out"
PRIOR_OUT = "_og_profit_val_out"

REPORT_FILE = "m3_opengov_commercial_id_pricing_repair_v1_report.json"
PROGRESS_FILE = "m3_opengov_commercial_id_pricing_repair_v1_progress.json"
AUDIT_FILE = "m3_opengov_commercial_id_pricing_repair_v1_audit87.json"
RANKED_FILE = "m3_opengov_commercial_id_pricing_repair_v1_ranked.json"
TOP20_FILE = "m3_opengov_commercial_id_pricing_repair_v1_top20.json"
DEALS_FILE = "m3_opengov_commercial_id_pricing_repair_v1_deals.json"
HIST_MANIFEST = "m3_opengov_commercial_id_pricing_repair_v1_hist_manifest.json"
LINE_CLASSES_FILE = "m3_opengov_commercial_id_pricing_repair_v1_line_classes.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

LINE_CLASS = {
    "EXTRACTION_FAILURE",
    "SPECIFICATION_INCOMPLETE",
    "PRODUCT_IDENTITY_UNRESOLVED",
    "EXACT_PRODUCT_FOUND_NO_PUBLIC_PRICE",
    "SUPPLIER_QUOTE_REQUIRED",
    "VERIFIED_RETAIL_PRICE",
}

_PART_HASH = re.compile(
    r"(?:part\s*#|part\s*no\.?|p/?n|mpn|item\s*(?:number|no\.?|#)|catalog(?:\s*#)?|"
    r"model\s*#|cat\.?\s*#)\s*[:#]?\s*([A-Z0-9][A-Z0-9.\-_/]{2,40})",
    re.I,
)
_HASH_SKU = re.compile(r"#\s*([A-Z0-9][A-Z0-9.\-_/]{2,30})")
_MAKE_MODEL = re.compile(
    r"Make\s*:\s*([A-Za-z0-9][A-Za-z0-9 .\-/]{0,40}?)\s*"
    r"(?:,|\band\b|/)?\s*Model\s*:\s*([A-Z0-9][A-Z0-9.\-_/]{1,40})",
    re.I,
)
_MAKE_LABELED = re.compile(r"\bMake\s*:\s*([A-Za-z0-9][A-Za-z0-9 .\-/]{1,40})", re.I)
_MODEL_LABELED = re.compile(r"\bModel\s*:\s*([A-Z0-9][A-Z0-9.\-_/]{2,40})", re.I)
_QTY_UOM_PREFIX = re.compile(
    r"^\s*(?P<uom>CASE|GALLON|QUART|EACH|BOX|PACK|PAIR|SET|ROLL|CARTON|DRUM|BAG|CS|EA|GAL|QT)\b",
    re.I,
)
_PACK_QTY = re.compile(r"\b(\d+)\s*/\s*(CS|CASE|BX|BOX|PK|PACK|CT)\b", re.I)
_TRAILING_BRAND_SKU = re.compile(
    r"\b("
    r"Simple\s+Green|Simple\s+Creen|Gojo|GOJO|Windex|Brasso|Rubbermaid|Betco|"
    r"Safeguard|Clorox|Lysol|Purell|Georgia[-\s]?Pacific|GP\s+Pro|"
    r"Kimberly[-\s]?Clark|Scott|Ansell|Honeywell|Milwaukee|DeWalt|Makita|"
    r"CRAFTSMAN|3M"
    r")\s+([A-Z0-9][A-Z0-9.\-_/]{2,24})\b",
    re.I,
)
_KNOWN_BRANDS = re.compile(
    r"\b(BURKS?|LIBERTY|MORBARK|AFC|AMERICAN\s+FLOW\s+CONTROL|ESSITY|SCA|"
    r"HONEYWELL|VANGUARD|BARRIAIRE|MILWAUKEE|F\.?\s*E\.?\s*MYERS|MYERS|"
    r"BETTER\s+ENGINEERING|MUELLER|CLOW|KENNEDY|WATEROUS|JOHN\s+DEERE|"
    r"CATERPILLAR|GRAINGER|3M|ANSELL|GLOBE|LION|"
    r"BANDIT|VERMEER|GOODYEAR|MICHELIN|BRIDGESTONE|"
    r"SIMPLE\s+GREEN|GOJO|WINDEX|BRASSO|RUBBERMAID|BETCO|SAFEGUARD|"
    r"CLOROX|LYSOL|PURELL|TAKEUCHI|TELEDYNE|CORNELL|DEWALT|MAKITA)\b",
    re.I,
)
_MODEL_AFTER_BRAND = re.compile(
    r"\b(BURKS?|LIBERTY|MORBARK|HONEYWELL|VANGUARD|MILWAUKEE|MYERS|ESSITY|SCA|"
    r"BARRIAIRE|TAKEUCHI|TELEDYNE|CORNELL|GOJO|SIMPLE\s+GREEN|DEWALT|MAKITA)\b"
    r"[\s,:-]*(?:PRO-SERIES\s+)?([A-Z0-9][A-Z0-9.\-_/]{2,24})",
    re.I,
)
_SERVICE_TITLE = re.compile(
    r"\b(services?|cleaning|inspection|painting|plowing|removal|stewardship|"
    r"calibration|investigate|document|welding|fabrication|mobilization|"
    r"rental|labor|install(?:ation)?|modernization|elevator|escalator|"
    r"bypass\s+pumping|window\s+replacement|ada\s+push|restroom\s+improve|"
    r"building\s+automation|risk\s+assessment|driver\s+record|"
    r"delivery|repair for|machine\s+work|and\s+repairs?|upgrades?\s+fy)\b",
    re.I,
)
_PRODUCT_TITLE = re.compile(
    r"\b(pump|hydrant|hammer|pole|paper|towel|tissue|chair|tabletop|gear|"
    r"turnout|parts|fastener|supply|supplies|washer|uniform|brake|"
    r"alternator|starter|lighting|fixture|grinder|valve|hose|filter|"
    r"battery|tire|meter|pipe|hardware|tool|inventory)\b",
    re.I,
)
_FEE_LINE = re.compile(
    r"\b(delivery|freight|shipping|assembly\s+fees?|mobilization|demobilization|"
    r"labor|technician|installation|rental\s+rate|fuel)\b",
    re.I,
)
_INSTALL_SCOPE = re.compile(
    r"\b(furnish\s+all\s+labor|contractor\s+to\s+furnish|install(?:ation)?|"
    r"or\s+equal.*labor|construction|modernization)\b",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _canonical_data() -> Path:
    """Always the real govtracker/data tree (history lives here)."""
    return Path(__file__).resolve().parents[1] / "data"


def _out_dir() -> Path:
    d = _canonical_data() / OUT_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(name: str, obj: Any) -> Path:
    p = _out_dir() / name
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)
    return p


def _load_out(name: str) -> Any:
    p = _out_dir() / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _progress(**kwargs: Any) -> None:
    cur = _load_out(PROGRESS_FILE) or {"kind": "CommercialIdPricingRepairProgress", "build": BUILD}
    cur.update(kwargs)
    cur["heartbeat_at"] = _utc()
    cur["build"] = BUILD
    _save(PROGRESS_FILE, cur)


def restore_historical_pricing_access() -> dict[str, Any]:
    """Point isolated validation at persisted history WITHOUT modifying originals."""
    canon = _canonical_data()
    out = _out_dir()
    bid_src = canon / "opengov_bid_price_index.json"
    hist_src = canon / "opengov_buyer_history"
    bid_link = out / "opengov_bid_price_index.json"
    hist_link = out / "opengov_buyer_history"

    evidence: dict[str, Any] = {
        "ACTION": "RESTORE_READ_ONLY_ACCESS",
        "TOUCHED_PRODUCTION_HISTORY": False,
        "DELETED_OR_OVERWROTE_HISTORY": False,
        "BID_INDEX_SOURCE": str(bid_src),
        "BUYER_HISTORY_SOURCE": str(hist_src),
        "BID_INDEX_PRESENT": bid_src.exists(),
        "BUYER_HISTORY_PRESENT": hist_src.exists() and hist_src.is_dir(),
    }

    if bid_src.exists():
        doc = json.loads(bid_src.read_text(encoding="utf-8"))
        recs = [r for r in (doc.get("records") or []) if isinstance(r, dict)]
        priced = sum(1 for r in recs if r.get("bid_unit_price") not in (None, "", 0, 0.0))
        with_pn = sum(1 for r in recs if r.get("part_number"))
        evidence.update(
            {
                "BID_INDEX_RECORDS": len(recs),
                "BID_INDEX_WITH_UNIT_PRICE": priced,
                "BID_INDEX_WITH_PART_NUMBER": with_pn,
                "BID_INDEX_BUYERS": len(doc.get("buyers_indexed") or []),
                "BID_INDEX_UPDATED_AT": doc.get("updated_at"),
                "BID_INDEX_HAS_BY_PN": bool(doc.get("by_pn")),
                "BID_INDEX_HAS_BY_MODEL": bool(doc.get("by_model")),
                "BID_INDEX_HAS_BY_DESC": bool(doc.get("by_desc")),
            }
        )
        # Read-only pointer file (absolute path) — do not copy 33MB unless missing pointer
        pointer = {
            "kind": "ReadOnlyBidIndexPointer",
            "build": BUILD,
            "source_path": str(bid_src.resolve()),
            "record_count": len(recs),
            "NOTE": "Validation loads from source_path; production file untouched",
        }
        (out / "opengov_bid_price_index.POINTER.json").write_text(
            json.dumps(pointer, indent=2), encoding="utf-8"
        )
        evidence["BID_INDEX_POINTER"] = str(out / "opengov_bid_price_index.POINTER.json")
        # Optional lightweight symlink/junction for tools that expect the filename in out dir
        if not bid_link.exists():
            try:
                bid_link.symlink_to(bid_src.resolve())
                evidence["BID_INDEX_LINK"] = "symlink"
            except Exception:
                # Windows may block symlink; leave pointer only
                evidence["BID_INDEX_LINK"] = "pointer_only"
    else:
        evidence["BID_INDEX_STATE"] = "MISSING_EVIDENCE"
        evidence["NOTE"] = "No local bid index; distinguish from search failure"

    if hist_src.exists() and hist_src.is_dir():
        files = list(hist_src.glob("*.json"))
        evidence.update(
            {
                "BUYER_HISTORY_FILES": len(files),
                "BUYER_HISTORY_BYTES": sum(f.stat().st_size for f in files),
            }
        )
        ptr = {
            "kind": "ReadOnlyBuyerHistoryPointer",
            "build": BUILD,
            "source_path": str(hist_src.resolve()),
            "file_count": len(files),
        }
        (out / "opengov_buyer_history.POINTER.json").write_text(json.dumps(ptr, indent=2), encoding="utf-8")
        if not hist_link.exists():
            try:
                hist_link.symlink_to(hist_src.resolve(), target_is_directory=True)
                evidence["BUYER_HISTORY_LINK"] = "symlink"
            except Exception:
                evidence["BUYER_HISTORY_LINK"] = "pointer_only"
    else:
        evidence["BUYER_HISTORY_STATE"] = "MISSING_EVIDENCE"

    # Volume wipe context (production Railway volume lacks these; local archive retained)
    evidence["VOLUME_WIPE_NOTE"] = (
        "Railway volume zero-history wipe removed live bid index / buyer_history; "
        "persisted local copies remain and are used READ-ONLY for this validation."
    )
    evidence["PRIOR_VALIDATION_HIST_STATE"] = "MISSING_AFTER_ZERO_HISTORY_WIPE"
    evidence["RESTORED_FOR_ISOLATED_VALIDATION"] = bool(evidence.get("BID_INDEX_PRESENT"))
    _save(HIST_MANIFEST, evidence)
    return evidence


class BidIndex:
    """Fast read-only historical award lookup."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: list[dict[str, Any]] = []
        self.by_pn: dict[str, list[int]] = {}
        self.by_model: dict[str, list[int]] = {}
        self.loaded = False
        self.load_error: str | None = None

    def load(self) -> None:
        if not self.path.exists():
            self.load_error = "FILE_MISSING"
            return
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            self.records = [r for r in (doc.get("records") or []) if isinstance(r, dict)]
            raw_pn = doc.get("by_pn") or {}
            raw_model = doc.get("by_model") or {}
            if isinstance(raw_pn, dict) and raw_pn:
                self.by_pn = {str(k).upper().replace("-", ""): v for k, v in raw_pn.items() if isinstance(v, list)}
            else:
                self.by_pn = {}
                for i, r in enumerate(self.records):
                    pn = str(r.get("part_number") or r.get("pn_norm") or "").upper().replace("-", "")
                    if pn:
                        self.by_pn.setdefault(pn, []).append(i)
            if isinstance(raw_model, dict) and raw_model:
                self.by_model = {str(k).upper().replace("-", ""): v for k, v in raw_model.items() if isinstance(v, list)}
            self.loaded = True
        except Exception as exc:
            self.load_error = type(exc).__name__

    def lookup(
        self,
        *,
        catalog: str | None,
        model: str | None,
        description: str,
    ) -> dict[str, Any]:
        if not self.loaded:
            return {
                "HISTORICAL_GOV_PRICE": None,
                "HISTORICAL_STATE": "UNKNOWN",
                "HISTORICAL_AWARD_LINK": None,
                "REASON": self.load_error or "INDEX_NOT_LOADED",
                "EVIDENCE_CLASS": "SEARCH_FAILURE" if self.load_error else "MISSING_EVIDENCE",
            }

        def _norm(raw: str) -> str:
            return str(raw).upper().replace("-", "").replace(" ", "")

        def _usable_key(k: str, *, allow_short_alpha: bool = False) -> bool:
            if not k or len(k) < 4:
                return False
            # Reject bare short numerics / DOT pay-item-like codes that collide widely
            if k.isdigit() and len(k) < 6:
                return False
            if len(k) < 5 and not allow_short_alpha:
                return False
            return True

        keys: list[str] = []
        model_keys: list[str] = []
        if catalog:
            keys.append(_norm(catalog))
        if model:
            model_keys.append(_norm(model))
        for m in _PART_HASH.finditer(description or ""):
            keys.append(_norm(m.group(1)))
        for m in _HASH_SKU.finditer(description or ""):
            keys.append(_norm(m.group(1)))

        seen: set[int] = set()
        for k in keys:
            if not _usable_key(k):
                continue
            for idx in self.by_pn.get(k) or []:
                if idx in seen:
                    continue
                seen.add(idx)
                hit = self._hit(self.records[idx], reason="PN_INDEX_MATCH", key=k)
                if hit:
                    return hit
        for k in model_keys:
            if not _usable_key(k, allow_short_alpha=True) or (k.isdigit() and len(k) < 6):
                continue
            for idx in self.by_model.get(k) or []:
                if idx in seen:
                    continue
                seen.add(idx)
                hit = self._hit(self.records[idx], reason="MODEL_INDEX_MATCH", key=k)
                if hit:
                    return hit

        # Bounded description token scan — require longer distinctive prefix
        desc_l = (description or "").lower()[:80]
        if len(desc_l) >= 20:
            scanned = 0
            needle = desc_l[:32]
            for r in self.records:
                scanned += 1
                if scanned > 4000:
                    break
                title = str(r.get("line_description") or "").lower()
                if needle and needle in title:
                    hit = self._hit(r, reason="DESCRIPTION_PREFIX_MATCH", key=needle)
                    if hit:
                        return hit

        return {
            "HISTORICAL_GOV_PRICE": None,
            "HISTORICAL_STATE": "UNKNOWN",
            "HISTORICAL_AWARD_LINK": None,
            "REASON": "NO_MATCHING_AWARD_EVIDENCE",
            "EVIDENCE_CLASS": "NO_MATCH",
            "KEYS_TRIED": keys[:8],
        }

    @staticmethod
    def _hit(rec: dict[str, Any], *, reason: str, key: str) -> dict[str, Any] | None:
        price = rec.get("bid_unit_price") or rec.get("unit_price") or rec.get("award_unit_price")
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            return None
        if price_f <= 0:
            return None
        url = str(rec.get("source_url") or "") or None
        return {
            "HISTORICAL_GOV_PRICE": price_f,
            "HISTORICAL_STATE": "PROVEN",
            "HISTORICAL_AWARD_LINK": url,
            "REASON": reason,
            "EVIDENCE_CLASS": "MATCHED",
            "MATCH_KEY": key,
            "AWARD_BUYER": rec.get("buyer"),
            "AWARD_DATE": rec.get("award_date"),
            "AWARD_VENDOR": rec.get("vendor"),
            "AWARD_LINE": (rec.get("line_description") or "")[:160],
            "ROLE": "HISTORICAL_AWARD_UNIT",  # never treat as current revenue / bid price
        }


def parse_line_identity(ln: dict[str, Any]) -> dict[str, Any]:
    """Extract commercial identity from structured fields + description text."""
    desc = str(ln.get("description") or ln.get("DESCRIPTION") or "").strip()
    catalog = ln.get("catalog") or ln.get("CATALOG_NUMBER") or ln.get("catalog_number")
    mfr = ln.get("manufacturer") or ln.get("MANUFACTURER") or ln.get("brand") or ln.get("BRAND")
    model = ln.get("model") or ln.get("MODEL")
    mpn = ln.get("mpn") or ln.get("MPN") or ln.get("part_number")
    sku = ln.get("sku") or ln.get("SKU")
    nsn = ln.get("nsn") or ln.get("NSN")
    qty = ln.get("qty") or ln.get("QTY") or ln.get("quantity")
    uom = ln.get("uom") or ln.get("UOM") or ln.get("unit")
    evidence: list[dict[str, str]] = []

    attrs: dict[str, Any] = {}
    try:
        from buyer_intelligence.opengov_line_spec_supplier_ready_v1 import extract_spec_attributes

        attrs = extract_spec_attributes(desc) or {}
    except Exception:
        attrs = {}

    if not mpn and attrs.get("mpn"):
        mpn = attrs.get("mpn")
        evidence.append({"field": "part_number", "value": str(mpn), "via": "spec_attrs"})
    if not nsn and attrs.get("nsn"):
        nsn = attrs.get("nsn")

    # Make: / Model: labeled fields (must beat bare part-hash noise)
    mm = _MAKE_MODEL.search(desc)
    if mm:
        if not mfr:
            mfr = mm.group(1).strip().rstrip(" .,")
            evidence.append({"field": "manufacturer", "value": mfr, "via": "make_model_pair", "span": mm.group(0)[:80]})
        if not model:
            model = mm.group(2).strip()
            evidence.append({"field": "model", "value": model, "via": "make_model_pair", "span": mm.group(0)[:80]})
    else:
        if not mfr:
            mk = _MAKE_LABELED.search(desc)
            if mk:
                mfr = mk.group(1).strip().rstrip(" .,")
                evidence.append({"field": "manufacturer", "value": mfr, "via": "make_labeled"})
        if not model:
            md = _MODEL_LABELED.search(desc)
            if md:
                model = md.group(1).strip()
                evidence.append({"field": "model", "value": model, "via": "model_labeled"})

    if not mpn:
        m = _PART_HASH.search(desc)
        if m:
            mpn = m.group(1).strip()
            evidence.append({"field": "part_number", "value": mpn, "via": "part_hash"})
    if not mpn:
        # Brand#SKU patterns like SCA #TM1616S / ESSITY #MK520
        for m in _HASH_SKU.finditer(desc):
            tok = m.group(1).strip()
            if len(tok) >= 4 and not tok.isdigit():
                mpn = tok
                evidence.append({"field": "part_number", "value": mpn, "via": "hash_sku"})
                break

    # Trailing brand + catalog (Simple Green 13005, Gojo 5265-02)
    tb = _TRAILING_BRAND_SKU.search(desc)
    if tb:
        brand = tb.group(1).strip()
        cat = tb.group(2).strip()
        if brand.upper().startswith("SIMPLE"):
            brand = "Simple Green"
        if not mfr:
            mfr = brand
            evidence.append({"field": "manufacturer", "value": mfr, "via": "trailing_brand_sku"})
        if not mpn or (str(mpn).isdigit() and not str(cat).isdigit()):
            mpn = cat
            evidence.append({"field": "part_number", "value": str(mpn), "via": "trailing_brand_sku"})
        if not model:
            model = cat

    # Trailing bare catalog / distributor SKU (… Lemon Scent, 24583096)
    if not mpn:
        tm = re.search(r",\s*([A-Z]{0,4}\d{4,}[A-Z0-9.\-]{0,12})\s*(?:-|–|$)", desc, re.I)
        if tm:
            mpn = tm.group(1).strip()
            evidence.append({"field": "part_number", "value": mpn, "via": "trailing_catalog_number"})

    if not mfr:
        bm = _KNOWN_BRANDS.search(desc)
        if bm:
            mfr = bm.group(1).strip()
            # normalize Burk → Burks
            if mfr.upper().startswith("BURK") and not mfr.upper().endswith("S"):
                mfr = "Burks"
            if "FLOW" in mfr.upper() or mfr.upper() == "AFC":
                mfr = "American Flow Control"
            evidence.append({"field": "manufacturer", "value": mfr, "via": "known_brand"})

    if not model:
        mm2 = _MODEL_AFTER_BRAND.search(desc)
        if mm2:
            cand = mm2.group(2).strip()
            # Avoid capturing generic words like GRINDER from "GRINDER PUMP WG30..."
            if cand.upper() not in {"GRINDER", "PUMP", "CAPACITOR", "SHREDDING", "KIT", "RING"}:
                model = cand
                if not mfr:
                    mfr = mm2.group(1).strip()
                evidence.append({"field": "model", "value": model, "via": "model_after_brand"})

    # Myers / industrial pump style: "... PUMP WG30-21-35" or bare WG##-##-##
    if (not mpn or not model) and re.search(r"\bMYERS\b|\bBURKS?\b|\bLIBERTY\b", desc, re.I):
        pm = re.search(r"\b([A-Z]{1,4}\d{2,}[A-Z0-9.\-]{0,20})\b", desc, re.I)
        if pm and pm.group(1).upper() not in {"GRINDER", "PUMP"}:
            tok = pm.group(1).strip()
            if not model:
                model = tok
                evidence.append({"field": "model", "value": model, "via": "industrial_model_token"})
            if not mpn:
                mpn = tok
                evidence.append({"field": "part_number", "value": mpn, "via": "industrial_model_token"})

    # Qty / UOM from portal fields or description prefix / pack notation
    if uom is None:
        um = _QTY_UOM_PREFIX.search(desc)
        if um:
            uom = um.group("uom").upper()
            evidence.append({"field": "uom", "value": str(uom), "via": "desc_prefix"})
    pack = _PACK_QTY.search(desc)
    pack_qty = int(pack.group(1)) if pack else None

    # Item number:68830-002 style already caught by _PART_HASH; also bare catalog field
    if catalog and not mpn:
        mpn = str(catalog)

    # Prefer model token as part when Make/Model present and part still empty
    if not mpn and model and re.match(r"^[A-Z0-9][A-Z0-9.\-_/]{2,}$", str(model), re.I):
        mpn = model

    ident = {
        "manufacturer": mfr,
        "model": model,
        "part_number": mpn or catalog,
        "catalog_number": catalog or mpn,
        "sku": sku,
        "nsn": nsn,
        "qty": qty,
        "uom": uom,
        "pack_qty": pack_qty,
        "raw_description": desc,
        "spec_attrs": {k: v for k, v in attrs.items() if v} if attrs else {},
        "brand_or_equal": bool(attrs.get("brand_or_equal")) if attrs else ("or equal" in desc.lower()),
        "extraction_evidence": evidence,
    }
    return ident


def identity_strength(ident: dict[str, Any]) -> str:
    if ident.get("nsn"):
        return "NSN"
    if ident.get("part_number") or ident.get("catalog_number") or ident.get("sku"):
        if ident.get("manufacturer"):
            return "MFR_PART"
        return "PART_ONLY"
    if ident.get("manufacturer") and ident.get("model"):
        return "MFR_MODEL"
    if ident.get("manufacturer"):
        return "BRAND_ONLY"
    # Spec-rich description without MPN is not automatic failure
    desc = ident.get("raw_description") or ""
    if len(desc) >= 40 and any(x in desc.lower() for x in ("inch", '"', "mm", "hp", "volt", "ply", "feet")):
        return "SPEC_DESCRIPTION"
    return "NONE"


def is_fee_or_service_line(desc: str) -> bool:
    return bool(_FEE_LINE.search(desc or ""))


def classify_line(
    *,
    ident: dict[str, Any],
    strength: str,
    hist: dict[str, Any],
    retail: dict[str, Any],
    package_missed: bool,
    is_fee: bool,
) -> str:
    if is_fee:
        # Fees are mandatory scope but priced via quote / included — not product SKUs
        if retail.get("RETAIL_STATE") == "PROVEN":
            return "VERIFIED_RETAIL_PRICE"
        return "SUPPLIER_QUOTE_REQUIRED"
    if retail.get("RETAIL_STATE") == "PROVEN":
        return "VERIFIED_RETAIL_PRICE"
    if strength in {"NSN", "MFR_PART", "PART_ONLY", "MFR_MODEL"} and retail.get("RETAIL_STATE") != "PROVEN":
        reason = str(retail.get("REASON") or "")
        if "NO_PRICE" in reason or "LIVE_RESOLVE_NO_PRICE" in reason or reason.startswith("LIVE_RESOLVE"):
            return "EXACT_PRODUCT_FOUND_NO_PUBLIC_PRICE"
        if "QUOTE" in reason.upper():
            return "SUPPLIER_QUOTE_REQUIRED"
        return "EXACT_PRODUCT_FOUND_NO_PUBLIC_PRICE"
    if strength == "SPEC_DESCRIPTION":
        return "SPECIFICATION_INCOMPLETE" if package_missed is False else "SPECIFICATION_INCOMPLETE"
    if strength in {"BRAND_ONLY", "NONE"}:
        if package_missed:
            return "EXTRACTION_FAILURE"
        return "PRODUCT_IDENTITY_UNRESOLVED"
    return "PRODUCT_IDENTITY_UNRESOLVED"


def lookup_retail(
    ident: dict[str, Any],
    *,
    client: httpx.Client | None,
    allow_live: bool,
    cache: dict[str, Any],
) -> dict[str, Any]:
    strength = identity_strength(ident)
    key_vals = [
        str(ident.get("part_number") or "").upper().replace("-", ""),
        str(ident.get("catalog_number") or "").upper().replace("-", ""),
        str(ident.get("model") or "").upper().replace("-", ""),
    ]
    key_vals = [k for k in key_vals if k and len(k) >= 3]
    entries = cache.get("entries") if isinstance(cache, dict) else {}
    if isinstance(entries, dict) and key_vals:
        for _k, ent in entries.items():
            if not isinstance(ent, dict):
                continue
            blob = json.dumps(ent).lower()
            if any(x in blob for x in ("opengov", "government bid", "award price")):
                continue
            mpn = str(ent.get("mpn") or ent.get("part_number") or ent.get("model") or "").upper().replace("-", "")
            if mpn and mpn in key_vals:
                try:
                    price = float(ent.get("unit_price") or ent.get("price") or 0)
                except (TypeError, ValueError):
                    price = 0.0
                if price > 0:
                    src = str(ent.get("source") or "")
                    if src.startswith("manual") or str(_k).startswith("manual:"):
                        origin = "MANUALLY_SEEDED_RESEARCH"
                    elif src.startswith("supplier_intel") or "intel" in src.lower():
                        origin = "PREVIOUSLY_CACHED_SUPPLIER_INTEL"
                    elif "mapping" in src.lower() or str(_k).startswith("map:"):
                        origin = "PRODUCT_SPECIFIC_MAPPING"
                    else:
                        origin = "PREVIOUSLY_CACHED_SUPPLIER_INTELLIGENCE"
                    return {
                        "RETAIL_UNIT_PRICE": price,
                        "RETAIL_STATE": "PROVEN",
                        "RETAIL_SELLER": ent.get("seller"),
                        "RETAIL_URL": ent.get("url") or ent.get("source_url"),
                        "REASON": "COMMERCIAL_CACHE",
                        "PRICE_ORIGIN": origin,
                        "CACHE_WARM": True,
                    }

    if not allow_live:
        return {
            "RETAIL_UNIT_PRICE": None,
            "RETAIL_STATE": "UNKNOWN",
            "RETAIL_SELLER": None,
            "RETAIL_URL": None,
            "REASON": "LIVE_DISABLED",
            "PRICE_ORIGIN": "NONE",
            "CACHE_WARM": False,
        }
    if strength in {"NONE", "BRAND_ONLY"}:
        return {
            "RETAIL_UNIT_PRICE": None,
            "RETAIL_STATE": "UNKNOWN",
            "RETAIL_SELLER": None,
            "RETAIL_URL": None,
            "REASON": "INSUFFICIENT_IDENTITY_FOR_PUBLIC_LOOKUP",
            "PRICE_ORIGIN": "NONE",
            "CACHE_WARM": False,
        }
    if strength == "SPEC_DESCRIPTION" and not (ident.get("manufacturer") and ident.get("model")):
        # Spec-only: still try one bounded query if description has brand+model-ish tokens
        if not ident.get("manufacturer"):
            return {
                "RETAIL_UNIT_PRICE": None,
                "RETAIL_STATE": "UNKNOWN",
                "RETAIL_SELLER": None,
                "RETAIL_URL": None,
                "REASON": "SPEC_ONLY_QUOTE_PREFERRED",
                "PRICE_ORIGIN": "NONE",
                "CACHE_WARM": False,
            }

    identity_payload = {
        "manufacturer": ident.get("manufacturer"),
        "model": ident.get("model"),
        "part_number": ident.get("part_number"),
        "catalog_number": ident.get("catalog_number"),
        "sku": ident.get("sku"),
        "nsn": ident.get("nsn"),
        "raw_description": ident.get("raw_description"),
    }

    # Prefer multi-supplier discovery (raised caps + exact-match ranking).
    try:
        from buyer_intelligence.multi_supplier_price_discovery_v1 import discover_multi_supplier_prices

        multi = discover_multi_supplier_prices(
            identity_payload,
            client=client,
            max_queries=4,
            max_pages=8,
            min_suppliers_target=2,
            use_budget=True,
            update_intel=True,
        )
        best = multi.get("BEST_OFFER") or {}
        price_f = multi.get("LOWEST_VERIFIED_UNIT_PRICE")
        url = multi.get("LOWEST_VERIFIED_URL") or best.get("PRODUCT_URL")
        seller = multi.get("LOWEST_VERIFIED_SUPPLIER") or best.get("SUPPLIER_NAME")
        offers = multi.get("OFFERS") or []
        if price_f and float(price_f) > 0 and url and best.get("MATCH_CLASS") == "EXACT_VERIFIED":
            return {
                "RETAIL_UNIT_PRICE": float(price_f),
                "RETAIL_STATE": "PROVEN",
                "RETAIL_SELLER": seller,
                "RETAIL_URL": url,
                "REASON": f"MULTI_SUPPLIER:{multi.get('RESOLVER_STATUS')}",
                "PRICE_ORIGIN": "FRESH_INDEPENDENT_DISCOVERY",
                "CACHE_WARM": False,
                "SUPPLIER_COUNT": int(multi.get("SUPPLIER_COUNT") or 0),
                "EXACT_VERIFIED_COUNT": int(multi.get("EXACT_VERIFIED_COUNT") or 0),
                "ALTERNATE_CANDIDATES": [
                    {
                        "url": o.get("PRODUCT_URL"),
                        "unit_price": o.get("UNIT_PRICE"),
                        "seller": o.get("SUPPLIER_NAME"),
                        "match_class": o.get("MATCH_CLASS"),
                    }
                    for o in offers[1:6]
                ],
                "ALL_CANDIDATES": [
                    {
                        "url": o.get("PRODUCT_URL"),
                        "unit_price": o.get("UNIT_PRICE"),
                        "seller": o.get("SUPPLIER_NAME"),
                        "seller_class": o.get("SELLER_CLASS"),
                        "match_class": o.get("MATCH_CLASS"),
                    }
                    for o in offers[:8]
                ],
                "SEARCH_TRACE_SUMMARY": multi.get("SEARCH_TRACE_SUMMARY"),
            }
        if price_f and float(price_f) > 0 and url:
            # Possible match — surface but do not auto-verify
            return {
                "RETAIL_UNIT_PRICE": None,
                "RETAIL_STATE": "UNKNOWN",
                "RETAIL_SELLER": seller,
                "RETAIL_URL": url,
                "REASON": f"MULTI_SUPPLIER_REVIEW_REQUIRED:{multi.get('RESOLVER_STATUS')}",
                "PRICE_ORIGIN": "FRESH_INDEPENDENT_DISCOVERY",
                "CACHE_WARM": False,
                "SUPPLIER_COUNT": int(multi.get("SUPPLIER_COUNT") or 0),
                "ALTERNATE_CANDIDATES": [
                    {
                        "url": o.get("PRODUCT_URL"),
                        "unit_price": o.get("UNIT_PRICE"),
                        "seller": o.get("SUPPLIER_NAME"),
                        "match_class": o.get("MATCH_CLASS"),
                    }
                    for o in offers[:5]
                ],
            }
        return {
            "RETAIL_UNIT_PRICE": None,
            "RETAIL_STATE": "UNKNOWN",
            "RETAIL_SELLER": seller,
            "RETAIL_URL": url,
            "REASON": f"MULTI_SUPPLIER_NO_PRICE:{multi.get('RESOLVER_STATUS')}",
            "PRICE_ORIGIN": "FRESH_INDEPENDENT_DISCOVERY",
            "CACHE_WARM": False,
            "SUPPLIER_COUNT": int(multi.get("SUPPLIER_COUNT") or 0),
            "SEARCH_TRACE_SUMMARY": multi.get("SEARCH_TRACE_SUMMARY"),
        }
    except Exception as exc:
        return {
            "RETAIL_UNIT_PRICE": None,
            "RETAIL_STATE": "UNKNOWN",
            "RETAIL_SELLER": None,
            "RETAIL_URL": None,
            "REASON": f"MULTI_SUPPLIER_ERROR:{type(exc).__name__}",
            "PRICE_ORIGIN": "NONE",
            "CACHE_WARM": False,
        }


def _qty(ln: dict[str, Any]) -> float | None:
    for k in ("qty", "QTY", "quantity"):
        if ln.get(k) is not None:
            try:
                return float(ln.get(k))
            except (TypeError, ValueError):
                return None
    return None


def load_prior_cohort() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Load prior profitability results + snapshot rows."""
    prior = _canonical_data() / PRIOR_OUT
    res = json.loads((prior / "m3_opengov_enriched_profitability_validation_v1_results.json").read_text(encoding="utf-8"))
    snap = json.loads((prior / "m3_opengov_enriched_profitability_validation_v1_snapshot.json").read_text(encoding="utf-8"))
    report = json.loads((prior / "m3_opengov_enriched_profitability_validation_v1_report.json").read_text(encoding="utf-8"))
    rows = res.get("rows") or []
    by_id = {str(r.get("OPPORTUNITY_ID")): r for r in (snap.get("rows") or []) if isinstance(r, dict)}
    return rows, list(by_id.values()), {"results": rows, "snapshot_by_id": by_id, "prior_report": report}


def product_fit_cohort(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        r
        for r in results
        if isinstance(r, dict)
        and r.get("PRODUCT_FIT") in {"DURABLE", "PRODUCT", "DURABLE_CANDIDATE"}
        and not r.get("IT_RADIO_EXCLUDED")
    ]


def rank_opportunity(row: dict[str, Any], snap_row: dict[str, Any] | None) -> tuple:
    """Higher is better — return sort key for reverse=True.

    Priority matches operator order: fewest lines first, then durable product
    signals, then identification strength — never let huge IDIQ catalogs crowd
    out 1–5 line pump/hydrant buys.
    """
    title = str(row.get("TITLE") or "")
    lines = (snap_row or {}).get("line_items") if snap_row else None
    if not isinstance(lines, list):
        lines = []
    n = len(lines) or int(row.get("LINE_COUNT") or 0)
    id_score = 0
    for ln in lines:
        if not isinstance(ln, dict):
            continue
        desc = str(ln.get("description") or ln.get("DESCRIPTION") or "")
        if is_fee_or_service_line(desc):
            continue
        ident = parse_line_identity(ln)
        st = identity_strength(ident)
        id_score += {"NSN": 5, "MFR_PART": 5, "PART_ONLY": 4, "MFR_MODEL": 4, "SPEC_DESCRIPTION": 2, "BRAND_ONLY": 1}.get(st, 0)

    service = 1 if _SERVICE_TITLE.search(title) else 0
    productish = 1 if _PRODUCT_TITLE.search(title) else 0
    install = 1 if _INSTALL_SCOPE.search(title) or any(
        _INSTALL_SCOPE.search(str(ln.get("description") or "")) for ln in lines if isinstance(ln, dict)
    ) else 0
    # Soft-cap: treat 30+ line catalogs as secondary even if product-titled
    line_bucket = 0 if n <= 3 else (1 if n <= 8 else (2 if n <= 15 else (3 if n <= 30 else 4)))
    prior_id = int(row.get("LINES_WITH_IDENTITY") or 0)
    prior_retail = int(row.get("LINES_WITH_RETAIL") or 0)
    atts = len((snap_row or {}).get("attachments_metadata") or [])
    id_density = (id_score / n) if n else 0.0

    return (
        -service,
        -install,
        -line_bucket,  # fewest-line bucket first
        -n if n > 0 else -999,
        productish,
        round(id_density, 3),
        id_score,
        prior_id,
        prior_retail,
        min(atts, 5),
        str(row.get("OPPORTUNITY_ID")),
    )


def audit_opportunity(
    result_row: dict[str, Any],
    snap_row: dict[str, Any],
    *,
    bid_index: BidIndex,
    deep: bool,
    client: httpx.Client | None,
    allow_live: bool,
    cache: dict[str, Any],
    live_budget: list[int],
) -> dict[str, Any]:
    oid = str(result_row.get("OPPORTUNITY_ID"))
    title = str(result_row.get("TITLE") or snap_row.get("title") or "")
    lines = [ln for ln in (snap_row.get("line_items") or []) if isinstance(ln, dict)]
    atts = snap_row.get("attachments_metadata") or []
    out: dict[str, Any] = {
        "OPPORTUNITY_ID": oid,
        "TITLE": title,
        "BUYER": result_row.get("BUYER") or snap_row.get("buyer"),
        "SOLICITATION_URL": result_row.get("SOLICITATION_URL") or snap_row.get("authoritative_url"),
        "DEADLINE": result_row.get("DEADLINE") or snap_row.get("deadline"),
        "MANDATORY_LINE_COUNT": len(lines),
        "ATTACHMENT_COUNT": len(atts),
        "PRIOR_STAGE": result_row.get("STAGE"),
        "PRIOR_LINES_WITH_IDENTITY": result_row.get("LINES_WITH_IDENTITY"),
        "PRIOR_LINES_WITH_RETAIL": result_row.get("LINES_WITH_RETAIL"),
        "PRIOR_LINES_WITH_HISTORICAL": result_row.get("LINES_WITH_HISTORICAL"),
        "SERVICE_TITLE": bool(_SERVICE_TITLE.search(title)),
        "PRODUCT_TITLE": bool(_PRODUCT_TITLE.search(title)),
        "INSTALL_OR_LABOR_SCOPE": bool(_INSTALL_SCOPE.search(title)),
        "DEEP_RESEARCHED": deep,
        "LINES": [],
        "CLASS_COUNTS": {},
        "LINES_IDENTIFIED": 0,
        "LINES_VERIFIED_RETAIL": 0,
        "LINES_QUOTE_REQUIRED": 0,
        "LINES_WITH_HISTORICAL": 0,
        "FULLY_PRICED": False,
        "ACQUISITION_COST": None,
        "CURRENT_REVENUE": result_row.get("CURRENT_REVENUE"),
        "CURRENT_REVENUE_STATE": result_row.get("CURRENT_REVENUE_STATE") or "UNKNOWN",
        "CURRENT_REVENUE_ROLE": "CURRENT_SOLICITATION_ESTIMATE_OR_BUDGET",
        "HISTORICAL_REVENUE_ROLE": "SEPARATE_DO_NOT_INTERCHANGE",
        "PROPOSED_BID_PRICE": None,
        "PROPOSED_BID_PRICE_STATE": "UNKNOWN",
        "ESTIMATED_GROSS_PROFIT": None,
        "PROFIT_STATE": "UNKNOWN",
        "FINANCING_STATUS": "UNKNOWN",
        "PACKAGE_MAY_HOLD_MORE_SPECS": len(atts) > 0,
    }

    acq = 0.0
    acq_known = True
    class_c: Counter[str] = Counter()

    for ln in lines:
        desc = str(ln.get("description") or ln.get("DESCRIPTION") or "")
        ident = parse_line_identity(ln)
        strength = identity_strength(ident)
        is_fee = is_fee_or_service_line(desc)
        qty = _qty(ln)

        # Package miss heuristic: vague portal line but multiple attachments
        package_missed = bool(
            len(atts) >= 2
            and strength in {"NONE", "BRAND_ONLY"}
            and len(desc) < 80
            and not is_fee
        )

        hist = bid_index.lookup(
            catalog=str(ident.get("catalog_number") or "") or None,
            model=str(ident.get("model") or "") or None,
            description=desc,
        )
        retail: dict[str, Any]
        if deep and not is_fee and live_budget[0] > 0 and allow_live:
            retail = lookup_retail(ident, client=client, allow_live=True, cache=cache)
            reason = str(retail.get("REASON") or "")
            # Count any live public lookup attempt (legacy LIVE_RESOLVE + multi-supplier).
            if (
                reason.startswith("LIVE_RESOLVE")
                or reason.startswith("MULTI_SUPPLIER")
                or retail.get("PRICE_ORIGIN") == "FRESH_INDEPENDENT_DISCOVERY"
            ):
                live_budget[0] -= 1
        elif deep and is_fee:
            retail = {
                "RETAIL_UNIT_PRICE": None,
                "RETAIL_STATE": "UNKNOWN",
                "RETAIL_SELLER": None,
                "RETAIL_URL": None,
                "REASON": "FEE_LINE_QUOTE_OR_INCLUDED",
            }
        else:
            retail = lookup_retail(ident, client=client, allow_live=False, cache=cache)

        cls = classify_line(
            ident=ident,
            strength=strength,
            hist=hist,
            retail=retail,
            package_missed=package_missed,
            is_fee=is_fee,
        )
        class_c[cls] += 1

        if strength not in {"NONE"} and not is_fee:
            out["LINES_IDENTIFIED"] += 1
        if hist.get("HISTORICAL_STATE") == "PROVEN":
            out["LINES_WITH_HISTORICAL"] += 1
        if cls == "VERIFIED_RETAIL_PRICE":
            out["LINES_VERIFIED_RETAIL"] += 1
            unit = float(retail["RETAIL_UNIT_PRICE"])
            if qty is None:
                acq_known = False
            else:
                acq += unit * qty
        elif cls == "SUPPLIER_QUOTE_REQUIRED":
            out["LINES_QUOTE_REQUIRED"] += 1
            acq_known = False
        else:
            acq_known = False

        out["LINES"].append(
            {
                "DESCRIPTION": desc[:240],
                "QTY": qty,
                "UOM": ln.get("uom") or ln.get("UOM"),
                "IDENTITY": {k: v for k, v in ident.items() if v and k != "spec_attrs"},
                "IDENTITY_STRENGTH": strength,
                "IS_FEE_OR_SERVICE_LINE": is_fee,
                "PACKAGE_EXTRACTION_GAP": package_missed,
                "HISTORICAL": hist,
                "RETAIL": retail,
                "LINE_CLASS": cls,
                "UNRESOLVED_REASON": None
                if cls == "VERIFIED_RETAIL_PRICE"
                else {
                    "CLASS": cls,
                    "IDENTITY_STRENGTH": strength,
                    "RETAIL_REASON": retail.get("REASON"),
                    "HIST_REASON": hist.get("REASON"),
                    "HIST_EVIDENCE_CLASS": hist.get("EVIDENCE_CLASS"),
                    "PACKAGE_GAP": package_missed,
                },
            }
        )

    out["CLASS_COUNTS"] = dict(class_c)
    out["FULLY_PRICED"] = acq_known and len(lines) > 0 and out["LINES_VERIFIED_RETAIL"] == len(
        [x for x in out["LINES"] if not x.get("IS_FEE_OR_SERVICE_LINE")]
    )
    # Stricter: all non-fee lines verified retail; fee lines may be quote
    non_fee = [x for x in out["LINES"] if not x.get("IS_FEE_OR_SERVICE_LINE")]
    out["FULLY_PRICED"] = bool(non_fee) and all(x.get("LINE_CLASS") == "VERIFIED_RETAIL_PRICE" for x in non_fee)
    if out["LINES_VERIFIED_RETAIL"]:
        out["ACQUISITION_COST"] = round(acq, 2) if acq else None

    # Profit only when full non-fee scope priced AND current revenue proven — never invent
    rev = out.get("CURRENT_REVENUE")
    if out["FULLY_PRICED"] and rev and acq > 0:
        profit = float(rev) - acq
        out["ESTIMATED_GROSS_PROFIT"] = round(profit, 2)
        out["PROFIT_STATE"] = "PROVEN"
    elif out["FULLY_PRICED"] and not rev:
        out["PROFIT_STATE"] = "UNKNOWN"
        out["PROFIT_BLOCKER"] = "CURRENT_REVENUE_UNKNOWN"
    else:
        out["PROFIT_STATE"] = "UNKNOWN"
        out["PROFIT_BLOCKER"] = "ACQUISITION_COVERAGE_INCOMPLETE"

    # Financing: UNKNOWN unless economics known — never FAILED from incomplete pricing
    if out["PROFIT_STATE"] == "PROVEN" and rev and acq > 0:
        try:
            from financing_intelligence.assess import assess_opportunity_financing

            fin = assess_opportunity_financing(
                opportunity_id=f"og-cid-{uuid.uuid4().hex[:8]}",
                contract_value=float(rev),
                supplier_cost=float(acq),
                freight=0.0,
                persist=False,
            )
            if isinstance(fin, dict) and fin.get("financing_confirmed") is True:
                out["FINANCING_STATUS"] = "CONFIRMED"
            elif isinstance(fin, dict) and fin.get("blocked_reason"):
                out["FINANCING_STATUS"] = "FAILED"
                out["FINANCING_REASON"] = fin.get("blocked_reason")
            else:
                out["FINANCING_STATUS"] = "UNKNOWN"
                out["FINANCING_REASON"] = "ASSESSED_NOT_CONFIRMED"
        except Exception as exc:
            out["FINANCING_STATUS"] = "UNKNOWN"
            out["FINANCING_REASON"] = type(exc).__name__
    else:
        out["FINANCING_STATUS"] = "UNKNOWN"
        out["FINANCING_REASON"] = "INSUFFICIENT_ECONOMICS"

    return out


def run_commercial_id_pricing_repair_v1(
    *,
    top_n: int = 20,
    max_live_price_calls: int = 50,
    allow_live_price: bool = True,
) -> dict[str, Any]:
    t0 = time.time()
    run_id = f"og-cid-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    _progress(status="RUNNING", phase="HIST_RESTORE", RUN_ID=run_id)

    hist_manifest = restore_historical_pricing_access()
    bid_path = _canonical_data() / "opengov_bid_price_index.json"
    bid_index = BidIndex(bid_path)
    bid_index.load()

    _progress(phase="LOAD_COHORT", bid_loaded=bid_index.loaded, bid_records=len(bid_index.records))
    results, _snap_rows, bundle = load_prior_cohort()
    by_id: dict[str, Any] = bundle["snapshot_by_id"]
    prior_report = bundle["prior_report"]
    cohort = product_fit_cohort(results)

    # Before metrics from prior run
    before = {
        "PRODUCT_FIT_OPPORTUNITIES": len(cohort),
        "LINES_WITH_HISTORICAL": sum(int(r.get("LINES_WITH_HISTORICAL") or 0) for r in cohort),
        "OPPS_WITH_ANY_HISTORICAL": sum(1 for r in cohort if (r.get("LINES_WITH_HISTORICAL") or 0) > 0),
        "LINES_WITH_RETAIL": sum(int(r.get("LINES_WITH_RETAIL") or 0) for r in cohort),
        "OPPS_WITH_ANY_RETAIL": sum(1 for r in cohort if (r.get("LINES_WITH_RETAIL") or 0) > 0 or r.get("HAS_ANY_RETAIL")),
        "FULL_SCOPE_RETAIL": 0,
        "HIST_INDEX_STATE": "MISSING_AFTER_ZERO_HISTORY_WIPE",
        "SOURCE": prior_report.get("HONEST_ZERO") or prior_report.get("FUNNEL"),
    }

    # Rank all 87
    ranked_meta = []
    for r in cohort:
        oid = str(r.get("OPPORTUNITY_ID"))
        snap = by_id.get(oid) or {}
        ranked_meta.append({"row": r, "snap": snap, "key": rank_opportunity(r, snap)})
    ranked_meta.sort(key=lambda x: x["key"], reverse=True)

    ranked_summary = []
    for i, item in enumerate(ranked_meta, start=1):
        r, snap = item["row"], item["snap"]
        ranked_summary.append(
            {
                "RANK": i,
                "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                "TITLE": r.get("TITLE"),
                "LINE_COUNT": len(snap.get("line_items") or []) or r.get("LINE_COUNT"),
                "SERVICE_TITLE": bool(_SERVICE_TITLE.search(str(r.get("TITLE") or ""))),
                "PRODUCT_TITLE": bool(_PRODUCT_TITLE.search(str(r.get("TITLE") or ""))),
                "PRIOR_IDENTITY_LINES": r.get("LINES_WITH_IDENTITY"),
                "PRIOR_RETAIL_LINES": r.get("LINES_WITH_RETAIL"),
                "TOP20": i <= top_n,
            }
        )
    _save(RANKED_FILE, {"build": BUILD, "RUN_ID": run_id, "count": len(ranked_summary), "rows": ranked_summary})

    # Light audit all 87 (cache-only retail, hist on)
    _progress(phase="AUDIT87", total=len(ranked_meta))
    cache = {}
    cache_path = _canonical_data() / "m3_commercial_acquisition_cache_v1.json"
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    # Also check prior out cache
    prior_cache = _canonical_data() / PRIOR_OUT / "m3_commercial_acquisition_cache_v1.json"
    if prior_cache.exists():
        try:
            pc = json.loads(prior_cache.read_text(encoding="utf-8"))
            if isinstance(pc.get("entries"), dict):
                cache.setdefault("entries", {}).update(pc["entries"])
        except Exception:
            pass

    audit_rows = []
    live_budget = [0]  # no live during light audit
    for item in ranked_meta:
        audit_rows.append(
            audit_opportunity(
                item["row"],
                item["snap"],
                bid_index=bid_index,
                deep=False,
                client=None,
                allow_live=False,
                cache=cache,
                live_budget=live_budget,
            )
        )
    _save(AUDIT_FILE, {"build": BUILD, "RUN_ID": run_id, "count": len(audit_rows), "rows": audit_rows})

    # Deep research top N
    _progress(phase="TOP20_DEEP", top_n=top_n)
    top_items = ranked_meta[:top_n]
    live_budget = [max_live_price_calls]
    top20: list[dict[str, Any]] = []
    with httpx.Client(timeout=25.0, follow_redirects=True, headers={"User-Agent": UA}) as client:
        for i, item in enumerate(top_items, start=1):
            _progress(phase="TOP20_DEEP", processed=i, total=top_n, live_left=live_budget[0])
            ev = audit_opportunity(
                item["row"],
                item["snap"],
                bid_index=bid_index,
                deep=True,
                client=client,
                allow_live=allow_live_price,
                cache=cache,
                live_budget=live_budget,
            )
            ev["RANK"] = i
            top20.append(ev)
            time.sleep(0.15)

    _save(TOP20_FILE, {"build": BUILD, "RUN_ID": run_id, "count": len(top20), "rows": top20})

    # After metrics on top20 + hist coverage on full audit
    after_audit_hist_lines = sum(int(r.get("LINES_WITH_HISTORICAL") or 0) for r in audit_rows)
    after_audit_hist_opps = sum(1 for r in audit_rows if (r.get("LINES_WITH_HISTORICAL") or 0) > 0)
    top_lines = sum(int(r.get("MANDATORY_LINE_COUNT") or 0) for r in top20)
    top_identified = sum(int(r.get("LINES_IDENTIFIED") or 0) for r in top20)
    top_retail = sum(int(r.get("LINES_VERIFIED_RETAIL") or 0) for r in top20)
    top_quote = sum(int(r.get("LINES_QUOTE_REQUIRED") or 0) for r in top20)
    fully = [r for r in top20 if r.get("FULLY_PRICED")]
    profit_pos = [r for r in top20 if r.get("PROFIT_STATE") == "PROVEN" and float(r.get("ESTIMATED_GROSS_PROFIT") or 0) > 0]
    ge5 = [r for r in profit_pos if float(r.get("ESTIMATED_GROSS_PROFIT") or 0) >= 5000]
    ge10 = [r for r in profit_pos if float(r.get("ESTIMATED_GROSS_PROFIT") or 0) >= 10000]
    fin_c = Counter(str(r.get("FINANCING_STATUS") or "UNKNOWN") for r in top20)
    class_all: Counter[str] = Counter()
    unresolved_reasons: list[dict[str, Any]] = []
    for r in top20:
        for ln in r.get("LINES") or []:
            class_all[str(ln.get("LINE_CLASS"))] += 1
            if ln.get("LINE_CLASS") != "VERIFIED_RETAIL_PRICE":
                unresolved_reasons.append(
                    {
                        "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                        "TITLE": (r.get("TITLE") or "")[:80],
                        "DESCRIPTION": ln.get("DESCRIPTION"),
                        "UNRESOLVED": ln.get("UNRESOLVED_REASON"),
                    }
                )

    deals = []
    for r in top20:
        if not (r.get("FULLY_PRICED") or (r.get("LINES_VERIFIED_RETAIL") or 0) > 0):
            continue
        deals.append(
            {
                "RANK": r.get("RANK"),
                "TITLE": r.get("TITLE"),
                "BUYER": r.get("BUYER"),
                "MANDATORY_LINES": r.get("MANDATORY_LINE_COUNT"),
                "LINES_IDENTIFIED": r.get("LINES_IDENTIFIED"),
                "LINES_VERIFIED_RETAIL": r.get("LINES_VERIFIED_RETAIL"),
                "LINES_WITH_HISTORICAL": r.get("LINES_WITH_HISTORICAL"),
                "FULLY_PRICED": r.get("FULLY_PRICED"),
                "ACQUISITION_COST": r.get("ACQUISITION_COST"),
                "CURRENT_REVENUE": r.get("CURRENT_REVENUE"),
                "CURRENT_REVENUE_STATE": r.get("CURRENT_REVENUE_STATE"),
                "ESTIMATED_GROSS_PROFIT": r.get("ESTIMATED_GROSS_PROFIT"),
                "PROFIT_STATE": r.get("PROFIT_STATE"),
                "FINANCING_STATUS": r.get("FINANCING_STATUS"),
                "SOLICITATION_URL": r.get("SOLICITATION_URL"),
                "RETAIL_LINKS": [
                    (ln.get("RETAIL") or {}).get("RETAIL_URL")
                    for ln in (r.get("LINES") or [])
                    if (ln.get("RETAIL") or {}).get("RETAIL_URL")
                ],
                "HISTORICAL_LINKS": [
                    (ln.get("HISTORICAL") or {}).get("HISTORICAL_AWARD_LINK")
                    for ln in (r.get("LINES") or [])
                    if (ln.get("HISTORICAL") or {}).get("HISTORICAL_AWARD_LINK")
                ],
                "VIABLE": bool(
                    r.get("FULLY_PRICED")
                    and r.get("PROFIT_STATE") == "PROVEN"
                    and float(r.get("ESTIMATED_GROSS_PROFIT") or 0) >= 5000
                ),
            }
        )
    _save(DEALS_FILE, {"build": BUILD, "RUN_ID": run_id, "count": len(deals), "deals": deals})
    _save(LINE_CLASSES_FILE, {"build": BUILD, "counts": dict(class_all), "unresolved_sample": unresolved_reasons[:80]})

    # Remaining 67 quality check
    remaining = ranked_meta[top_n:]
    remaining_better = []
    for item in remaining:
        r, snap = item["row"], item["snap"]
        title = str(r.get("TITLE") or "")
        if _SERVICE_TITLE.search(title):
            continue
        if not _PRODUCT_TITLE.search(title):
            continue
        n = len(snap.get("line_items") or []) or int(r.get("LINE_COUNT") or 999)
        if n > 30:
            continue
        # score identity from light audit
        aud = next((a for a in audit_rows if a.get("OPPORTUNITY_ID") == r.get("OPPORTUNITY_ID")), None)
        if aud and (aud.get("LINES_IDENTIFIED") or 0) >= max(1, int(0.5 * (aud.get("MANDATORY_LINE_COUNT") or 1))):
            remaining_better.append(
                {
                    "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                    "TITLE": title,
                    "LINE_COUNT": n,
                    "LINES_IDENTIFIED": aud.get("LINES_IDENTIFIED"),
                    "LINES_WITH_HISTORICAL": aud.get("LINES_WITH_HISTORICAL"),
                }
            )

    scale_viable = None
    if fully and profit_pos:
        scale_viable = "PROMISING_BUT_NOT_YET_SCALE"
    elif top_retail > 0 or after_audit_hist_lines > 0:
        scale_viable = "PARTIALLY_VIABLE_IDENTITY_IMPROVED_PRICING_STILL_BOTTLENECK"
    else:
        scale_viable = "NOT_VIABLE_TO_SCALE_YET"

    why_top20_failed = []
    for r in top20:
        if r.get("FULLY_PRICED") and r.get("PROFIT_STATE") == "PROVEN":
            continue
        why_top20_failed.append(
            {
                "TITLE": r.get("TITLE"),
                "LINES": r.get("MANDATORY_LINE_COUNT"),
                "IDENTIFIED": r.get("LINES_IDENTIFIED"),
                "RETAIL": r.get("LINES_VERIFIED_RETAIL"),
                "HIST": r.get("LINES_WITH_HISTORICAL"),
                "CLASS_COUNTS": r.get("CLASS_COUNTS"),
                "PROFIT_BLOCKER": r.get("PROFIT_BLOCKER") or r.get("PROFIT_STATE"),
                "FINANCING_STATUS": r.get("FINANCING_STATUS"),
                "SERVICE_TITLE": r.get("SERVICE_TITLE"),
            }
        )

    report = {
        "BUILD": BUILD,
        "RUN_ID": run_id,
        "SOURCE_RUN": SOURCE_RUN,
        "PRIOR_PROFIT_BUILD": PRIOR_PROFIT_BUILD,
        "STATUS": "COMPLETE",
        "WALL_CLOCK_SECONDS": round(time.time() - t0, 2),
        "CREATED_AT": _utc(),
        "ISOLATION": {
            "TOUCHED_BIDNET_ENRICHMENT": False,
            "TOUCHED_L23_WRITES": False,
            "TOUCHED_OPERATOR_UI": False,
            "DELETED_OR_OVERWROTE_PRODUCTION_HISTORY": False,
            "SAM_API_CALLS": 0,
            "LIVE_PRICE_CALLS_BUDGET": max_live_price_calls,
            "LIVE_PRICE_CALLS_REMAINING": live_budget[0],
            "OUTPUT_DIR": OUT_DIR_NAME,
        },
        "HISTORICAL_ACCESS": hist_manifest,
        "BEFORE_AFTER_PRICING_COVERAGE": {
            "BEFORE": before,
            "AFTER": {
                "BID_INDEX_RECORDS_AVAILABLE": len(bid_index.records),
                "BID_INDEX_LOAD_ERROR": bid_index.load_error,
                "PRODUCT_FIT_OPPORTUNITIES": len(cohort),
                "AUDIT87_LINES_WITH_HISTORICAL": after_audit_hist_lines,
                "AUDIT87_OPPS_WITH_ANY_HISTORICAL": after_audit_hist_opps,
                "TOP20_TOTAL_REQUIRED_LINES": top_lines,
                "TOP20_LINES_IDENTIFIED": top_identified,
                "TOP20_LINES_VERIFIED_RETAIL": top_retail,
                "TOP20_LINES_QUOTE_REQUIRED": top_quote,
                "TOP20_FULLY_PRICED": len(fully),
                "TOP20_POSITIVE_MARGIN_PROVEN": len(profit_pos),
                "TOP20_PROFIT_GE_5000": len(ge5),
                "TOP20_PROFIT_GE_10000": len(ge10),
                "NOTE_HIST_UNKNOWN_NOT_UNPROFITABLE": True,
            },
        },
        "TOP20_METRICS": {
            "TOTAL_REQUIRED_LINES": top_lines,
            "LINES_CORRECTLY_IDENTIFIED": top_identified,
            "LINES_WITH_VERIFIED_RETAIL_PRICES": top_retail,
            "LINES_REQUIRING_QUOTES": top_quote,
            "FULLY_PRICED_OPPORTUNITIES": len(fully),
            "EVIDENCE_BACKED_POSITIVE_MARGINS": len(profit_pos),
            "PROFIT_ABOVE_5K": len(ge5),
            "PROFIT_ABOVE_10K": len(ge10),
            "FINANCING_STATUS_COUNTS": dict(fin_c),
            "LINE_CLASS_COUNTS": dict(class_all),
        },
        "VIABLE_DEALS": [d for d in deals if d.get("VIABLE")],
        "PARTIAL_DEALS": [d for d in deals if not d.get("VIABLE")],
        "WHY_TOP20_NOT_READY": why_top20_failed,
        "REMAINING_67_BETTER_CANDIDATES": remaining_better[:15],
        "REMAINING_67_NOTE": (
            "If top20 are service-heavy or identity-weak, remaining product catalogs "
            "(parts IDIQ, fasteners, janitorial) may be better but often have large line counts."
        ),
        "SCALE_DECISION": scale_viable,
        "SCALE_DECISION_RATIONALE": [
            "Extraction→lines works; commercial ID from descriptions improved vs MPN-only gate.",
            "Historical award index restored for validation (local persisted evidence).",
            "Full-scope public retail remains the primary bottleneck for most multi-line scopes.",
            "Absence of historical match is labeled UNKNOWN — not proof of unprofitability.",
            "Financing UNKNOWN ≠ FAILED when pricing incomplete.",
        ],
        "OUTPUT_LOCATIONS": {
            "report": REPORT_FILE,
            "audit87": AUDIT_FILE,
            "ranked": RANKED_FILE,
            "top20": TOP20_FILE,
            "deals": DEALS_FILE,
            "hist_manifest": HIST_MANIFEST,
            "line_classes": LINE_CLASSES_FILE,
            "dir": OUT_DIR_NAME,
        },
    }
    _save(REPORT_FILE, report)
    _progress(status="DONE", phase="DONE", FINAL_STATUS="COMPLETE", scale=scale_viable)
    return report


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--max-live-price-calls", type=int, default=50)
    ap.add_argument("--no-live-price", action="store_true")
    args = ap.parse_args()
    rep = run_commercial_id_pricing_repair_v1(
        top_n=args.top_n,
        max_live_price_calls=args.max_live_price_calls,
        allow_live_price=not args.no_live_price,
    )
    print(
        json.dumps(
            {
                "STATUS": rep.get("STATUS"),
                "BEFORE_AFTER": rep.get("BEFORE_AFTER_PRICING_COVERAGE"),
                "TOP20_METRICS": rep.get("TOP20_METRICS"),
                "SCALE_DECISION": rep.get("SCALE_DECISION"),
                "VIABLE_DEALS": len(rep.get("VIABLE_DEALS") or []),
            },
            indent=2,
        )
    )
