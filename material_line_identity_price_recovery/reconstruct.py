"""Source-first reconstruction from original solicitation packages."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from m3_data_root import data_path
from material_line_identity_price_recovery.models import PACKAGE_ROOT
from product_identity.classifier import classify_identity
from product_identity.normalizer import ProductIdentityNormalizer
from product_identity.package_process import process_package
from product_identity.spreadsheet import extract_spreadsheet

_KNOWN_MFR = re.compile(
    r"\b("
    r"Hunter|Netafim|Cummins|Ford|Caterpillar|John\s*Deere|3M|Honeywell|Grainger|"
    r"Milwaukee|DeWalt|Makita|Bosch|Siemens|Schneider|Eaton|Motorola|Cisco|Dell|"
    r"Mueller|Carson|Rain\s*Bird|Toro|Irritrol|Weathermatic|Baseline|"
    r"Philips|Lithonia|Acuity|Leviton|Hubbell|Square\s*D|ABB|Baldor|"
    r"Gates|Dayco|Donaldson|Fleetguard|Fram|Wix|Mann|Bosch|Delphi|"
    r"Global\s*Industrial|Hon|Steelcase|Herman\s*Miller"
    r")\b",
    re.I,
)
_MPN_IN_DESC = re.compile(
    r"\b([A-Z]{0,6}\d[\w\-./]{2,}|\d{2,}[A-Z][\w\-./]{1,}|[A-Z]{2,}[-/]\d[\w\-./]*)\b"
)
_NSN = re.compile(r"\b(\d{4})[- ]?(\d{2})[- ]?(\d{3})[- ]?(\d{4})\b")
_OR_EQUAL = re.compile(r"\b(brand\s+or\s+equal|or\s+approved\s+equal|or\s+equivalent|or\s+equal)\b", re.I)
_SECTION_FAKE_MFR = re.compile(
    r"^(SITE\s+WORK|CONDUIT\s+INSTALLATION|FENCE\s+INSTALLATION|TREES|VALVES|DRIPLINE|"
    r"IRRIGATION|ELECTRICAL|PLUMBING|NOTES?|GENERAL|LABOR|INSTALLATION)\b",
    re.I,
)


def package_dir_for(opportunity_id: str) -> Path | None:
    parts = opportunity_id.split(":")
    if len(parts) < 3:
        return None
    buyer, pid = parts[1], parts[2]
    d = data_path(PACKAGE_ROOT) / buyer / pid
    return d if d.exists() else None


def reconstruct_opportunity_source(opportunity_id: str) -> dict[str, Any]:
    """Re-extract identities from original package files."""
    pkg = package_dir_for(opportunity_id)
    if not pkg:
        return {
            "opportunity_id": opportunity_id,
            "package_found": False,
            "identities": [],
            "raw_rows": [],
            "spreadsheet_recoveries": 0,
            "stats": {},
        }

    parts = opportunity_id.split(":")
    buyer, pid = parts[1], parts[2]
    processed = process_package(buyer, pid, pkg)
    identities = list(processed.get("identities") or [])

    # Extra spreadsheet pass for multi-column stats
    spreadsheet_recoveries = 0
    continuation_recoveries = 0
    xlsx_rows: list[dict[str, Any]] = []
    for f in pkg.iterdir():
        if f.suffix.lower() in {".xlsx", ".xls", ".csv"}:
            try:
                rows = extract_spreadsheet(f)
                spreadsheet_recoveries += sum(
                    1
                    for r in rows
                    if r.get("part_number") or r.get("model") or r.get("catalog_number") or r.get("manufacturer")
                )
                # continuation: rows with description but missing qty filled from prior — tracked via inherited_mfr
                continuation_recoveries += sum(1 for r in rows if r.get("inherited_manufacturer"))
                xlsx_rows.extend(rows)
            except Exception:
                pass

    # Enrich identities: pull real manufacturer from description when section header polluted
    enriched = []
    for ident in identities:
        enriched.append(_enrich_identity(ident))

    return {
        "opportunity_id": opportunity_id,
        "package_found": True,
        "package_path": str(pkg),
        "identities": enriched,
        "raw_rows": processed.get("raw_rows") or [],
        "xlsx_rows": xlsx_rows,
        "spreadsheet_recoveries": spreadsheet_recoveries,
        "continuation_recoveries": continuation_recoveries,
        "stats": {
            "identity_counts": processed.get("identity_counts"),
            "usable_count": processed.get("usable_count"),
            "files": len(list(pkg.iterdir())),
        },
    }


def _enrich_identity(ident: dict[str, Any]) -> dict[str, Any]:
    out = dict(ident)
    desc = str(out.get("raw_description") or "")
    mfr = out.get("manufacturer")
    if mfr and _SECTION_FAKE_MFR.match(str(mfr).strip()):
        out["manufacturer_section_header"] = mfr
        out["manufacturer"] = None
        mfr = None

    if not mfr:
        m = _KNOWN_MFR.search(desc)
        if m:
            out["manufacturer"] = m.group(1).strip()
            out["manufacturer_from_description"] = True
            mfr = out["manufacturer"]

    # Extract MPN/model tokens from description if missing
    if not out.get("part_number") and not out.get("model"):
        # Prefer labeled patterns already in classifier; also Hunter-style model tokens
        for tok_m in _MPN_IN_DESC.finditer(desc):
            tok = tok_m.group(1)
            if len(tok) < 4 or not re.search(r"\d", tok):
                continue
            if tok.lower() in {"and", "with", "from", "this"}:
                continue
            # Prefer tokens near known mfr or with hyphen/digit mix
            if mfr or "-" in tok or re.search(r"[A-Za-z].*\d|\d.*[A-Za-z]", tok):
                if re.search(r"[A-Za-z]", tok) and re.search(r"\d", tok):
                    out["model"] = out.get("model") or tok.upper()
                    out["model_from_description"] = True
                    break

    nsn_m = _NSN.search(desc)
    if nsn_m and not out.get("nsn"):
        out["nsn"] = f"{nsn_m.group(1)}-{nsn_m.group(2)}-{nsn_m.group(3)}-{nsn_m.group(4)}"

    if _OR_EQUAL.search(desc):
        out["equal_allowed"] = True
        out["brand_or_equal_context"] = True

    # Reclassify after enrichment
    normalizer = ProductIdentityNormalizer()
    norm = normalizer.normalize_row(
        {
            "manufacturer": out.get("manufacturer"),
            "model": out.get("model"),
            "part_number": out.get("part_number"),
            "catalog_number": out.get("catalog_number"),
            "nsn": out.get("nsn"),
            "upc": out.get("upc"),
            "description": desc,
            "quantity": out.get("quantity"),
            "uom": out.get("uom"),
            "pack": out.get("pack_size"),
        }
    )
    reclass = classify_identity(
        norm,
        extra={
            "equal_allowed": out.get("equal_allowed"),
            "brand_or_equal_context": out.get("brand_or_equal_context"),
        },
    )
    for k in (
        "identity_type",
        "confidence_grade",
        "commercial_search_key",
        "part_number",
        "model",
        "manufacturer",
        "nsn",
        "equal_allowed",
        "confidence_reasons",
    ):
        if reclass.get(k) is not None:
            out[k] = reclass.get(k)
    out["RAW_SOURCE_TEXT"] = desc
    out["NORMALIZED_DESCRIPTION"] = desc
    out["MANUFACTURER"] = out.get("manufacturer")
    out["MPN"] = out.get("part_number")
    out["MODEL"] = out.get("model")
    out["NSN"] = out.get("nsn")
    out["BRAND_OR_EQUAL"] = bool(out.get("equal_allowed"))
    out["QTY"] = out.get("quantity")
    out["UOM"] = out.get("uom_normalized") or out.get("uom")
    out["PACK"] = out.get("pack_size")
    return out


def match_source_to_material_line(material: dict[str, Any], source_idents: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Best-effort join of frozen material line to reconstructed source identity."""
    desc = str(material.get("raw_solicitation_description") or "").strip().lower()
    mpn = str(material.get("prior_mpn") or material.get("part_model_text") or "").strip().upper()
    clin = str(material.get("clin") or "").strip().upper()

    best = None
    best_score = 0
    for ident in source_idents:
        score = 0
        idesc = str(ident.get("raw_description") or "").strip().lower()
        ipn = str(ident.get("part_number") or ident.get("model") or "").strip().upper()
        iitem = str(ident.get("attributes", {}).get("item") or ident.get("item") or "").strip().upper()

        if mpn and ipn and (mpn == ipn or mpn in ipn or ipn in mpn):
            score += 50
        if clin and iitem and clin == iitem:
            score += 40
        if desc and idesc:
            if desc == idesc:
                score += 40
            elif desc[:60] and desc[:60] in idesc:
                score += 25
            elif idesc[:60] and idesc[:60] in desc:
                score += 20
            else:
                # token overlap
                dt = set(re.findall(r"[a-z0-9]{4,}", desc))
                it = set(re.findall(r"[a-z0-9]{4,}", idesc))
                if dt and it:
                    overlap = len(dt & it) / max(1, len(dt))
                    if overlap >= 0.5:
                        score += int(overlap * 20)
        if score > best_score:
            best_score = score
            best = ident
    if best_score >= 20:
        return {**best, "match_score": best_score}
    return None
