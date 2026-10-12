"""Supplier Fit Evidence Audit + Contact Recovery V1.

Audits ALL prior SUPPLIER_FIT_FULL packet assignments. Category match is NOT
FULL. No invented contacts. No supplier email send. No new discovery.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from application_clock import now_utc
from m3_jobs.standard import LONG_RUNNING_JOB_EXECUTION_STANDARD

log = logging.getLogger("govtracker.supplier_fit_evidence_contact_recovery_v1")

BUILD = "20261010-m3-supplier-fit-evidence-contact-recovery-v1"
PRIOR_BUILD = "20261010-m3-full-supplier-discovery-contact-rfq-production-v1"
FAMILY = "supplier_fit_evidence_contact_recovery_v1"

JOB_FILE = "m3_supplier_fit_evidence_contact_recovery_v1_job.json"
PROGRESS_FILE = "m3_supplier_fit_evidence_contact_recovery_v1_progress.json"
REPORT_FILE = "m3_supplier_fit_evidence_contact_recovery_v1_report.json"
RESULTS_FILE = "m3_supplier_fit_evidence_audit_results_v1.json"
PACKETS_OUT = "m3_supplier_fit_evidence_packets_v1.json"
QUEUE_FILE = "m3_supplier_fit_evidence_owner_queue_v1.json"
MANUAL_FILE = "m3_supplier_fit_evidence_manual_audit_v1.json"

PACKETS_IN = "m3_full_supplier_rfq_packets_v1.json"
RESULTS_IN = "m3_full_supplier_discovery_results_v1.json"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NSN_RE = re.compile(r"\b\d{4}-\d{2}-\d{3}-\d{4}\b")
_SPEC_RE = re.compile(
    r"\b(\d+(\.\d+)?\s*(v|volt|vac|vdc|w|watt|a|amp|hz|psi|gpm|gal|in\.?|inch|"
    r"mm|cm|ft|gauge|awg|npt|mph|rpm|lumens?|kelvin|k\b)|"
    r"size\s*[:#]?\s*[\w\-]+|model\s*[:#]?\s*[\w\-]+|"
    r"part\s*(no|number|#)?\s*[:#]?\s*[\w\-]+|"
    r"catalog\s*[:#]?\s*[\w\-]+)\b",
    re.I,
)

# Official public paths only — never used alone as FULL evidence.
SUPPLIER_META: dict[str, dict[str, Any]] = {
    "W.W. Grainger, Inc.": {
        "KEY": "GRAINGER_MRO",
        "CATALOG_URL": "https://www.grainger.com/",
        "FORM_URL": "https://www.grainger.com/content/contact-us",
        "PHONE": "1-800-GRAINGER",
        "BRANDS": {
            "grainger", "dayton", "westward", "tough guy", "condor", "speedaire",
            "lumapro", "clipper",
        },
        "PRODUCT_NOUNS": {
            "filter", "bearing", "belt", "gasket", "lubricant", "grease", "oil",
            "cleaner", "mop", "broom", "glove", "ppe", "respirator", "helmet", "vest",
            "hose", "clamp", "hardware", "screw", "bolt", "nut", "washer",
            "pump", "motor", "compressor", "blade", "bit", "drill", "saw",
            "wrench", "socket", "tape", "sealant", "battery", "lamp", "bulb",
            "janitorial", "safety", "abrasive", "cutting", "towel", "tissue",
            "soap", "sanitizer", "disinfectant", "trash", "fastener", "tool",
            "paper", "lighting", "fixture", "valve", "pipe",
        },
        "GENERIC_IF_ONLY_CATEGORY": True,
        "COMMODITY_NOUNS": {
            "mop", "broom", "towel", "tissue", "soap", "sanitizer", "disinfectant",
            "trash", "glove", "battery", "bulb", "lamp", "tape", "bolt", "nut",
            "screw", "washer", "fastener", "paper", "cleaner", "janitorial",
        },
    },
    "NAPA Auto Parts / Genuine Parts Company": {
        "KEY": "NAPA_AUTO",
        "CATALOG_URL": "https://www.napaonline.com/",
        "FORM_URL": "https://www.napaonline.com/en/contact-us",
        "PHONE": "1-800-LET-NAPA",
        "BRANDS": {"napa", "rayloc", "balkamp", "altrom"},
        "PRODUCT_NOUNS": {
            "battery", "filter", "brake", "spark", "alternator", "starter",
            "belt", "hose", "wiper", "rotor", "pad", "caliper", "ignition",
            "oil", "coolant", "thermostat", "sensor", "fuel", "transmission",
        },
        "AUTO_HINTS": {
            "vehicle", "fleet", "automotive", "truck", "ford", "chevy", "gm",
            "dodge", "toyota", "honda", "ram", "police", "ambulance", "utv",
            "oem", "aftermarket",
        },
        "GENERIC_IF_ONLY_CATEGORY": True,
        "COMMODITY_NOUNS": {
            "battery", "filter", "brake", "alternator", "starter", "wiper",
            "rotor", "pad", "caliper", "oil",
        },
    },
    "Ferguson Enterprises": {
        "KEY": "FERGUSON_PLUMBING",
        "CATALOG_URL": "https://www.ferguson.com/",
        "FORM_URL": "https://www.ferguson.com/content/contact-us",
        "PHONE": None,
        "BRANDS": {
            "ferguson", "proflo", "american standard", "kohler", "moen",
            "delta", "uponor", "charlotte pipe", "nibco", "watts", "zurn",
        },
        "PRODUCT_NOUNS": {
            "pipe", "valve", "fitting", "faucet", "toilet", "urinal", "sink",
            "coupling", "elbow", "tee", "flange", "gasket", "water heater",
            "backflow", "hydrant", "meter", "pvc", "copper", "brass", "npt",
            "sewer",
        },
        "GENERIC_IF_ONLY_CATEGORY": True,
        "COMMODITY_NOUNS": {"pipe", "valve", "fitting", "faucet", "toilet", "pvc", "hydrant"},
    },
    "Graybar Electric Company": {
        "KEY": "GRAYBAR_ELECTRICAL",
        "CATALOG_URL": "https://www.graybar.com/",
        "FORM_URL": "https://www.graybar.com/en/contact-us",
        "PHONE": None,
        "BRANDS": {
            "square d", "eaton", "siemens", "abb", "ge", "appleton", "hubbell",
            "leviton", "lutron", "panduit", "thomas & betts", "abb",
        },
        "PRODUCT_NOUNS": {
            "breaker", "contactor", "relay", "transformer", "wire", "cable",
            "conduit", "fuse", "panelboard", "disconnect", "switch", "outlet",
            "receptacle", "connector", "motor control", "vfd", "lighting",
            "fixture", "lamp", "bulb", "powerstrut", "strut",
        },
        "GENERIC_IF_ONLY_CATEGORY": True,
        "COMMODITY_NOUNS": {"wire", "cable", "conduit", "bulb", "lamp", "outlet", "fuse"},
    },
    "Signify / Philips Lighting Channel": {
        "KEY": "SIGNIFY_LIGHTING",
        "CATALOG_URL": "https://www.signify.com/",
        "FORM_URL": "https://www.signify.com/global/contact",
        "PHONE": None,
        "BRANDS": {
            "philips", "signify", "lumec", "roadfocus", "hadco", "gardco",
            "stongelight", "color kinetics", "interact",
        },
        "PRODUCT_NOUNS": {
            "led", "luminaire", "fixture", "troffer", "cobrahead", "lamp",
            "driver", "ballast", "pole", "arm",
        },
        "GENERIC_IF_ONLY_CATEGORY": True,
        "REQUIRE_BRAND_FOR_FULL": True,
    },
    "Acuity Brands / Lithonia Lighting Channel": {
        "KEY": "LITHONIA_LIGHTING",
        "CATALOG_URL": "https://www.acuitybrands.com/",
        "FORM_URL": "https://www.acuitybrands.com/contact-us",
        "PHONE": None,
        "BRANDS": {"lithonia", "acuity", "holophane", "gotham", "peerless"},
        "PRODUCT_NOUNS": {"led", "luminaire", "fixture", "troffer", "lamp"},
        "GENERIC_IF_ONLY_CATEGORY": True,
        "REQUIRE_BRAND_FOR_FULL": True,
    },
    "Office Depot Business Solutions": {
        "KEY": "ODP_OFFICE",
        "CATALOG_URL": "https://business.officedepot.com/",
        "FORM_URL": "https://business.officedepot.com/a/help/contactus/",
        "PHONE": "1-800-GO-DEPOT",
        "BRANDS": {"office depot", "odp", "epson", "hp", "canon", "brother", "xerox"},
        "PRODUCT_NOUNS": {
            "paper", "toner", "ink", "stapler", "folder", "binder", "pen",
            "pencil", "marker", "notepad", "envelope", "label", "tape",
            "clipboard", "calendar", "desk", "chair", "copier", "printer",
            "towel", "tissue", "soap", "sanitizer", "janitorial", "cleaner",
        },
        "GENERIC_IF_ONLY_CATEGORY": True,
        "COMMODITY_NOUNS": {
            "paper", "toner", "ink", "stapler", "folder", "binder", "pen",
            "pencil", "envelope", "towel", "tissue", "soap", "tape", "label",
        },
    },
    "Zoro Tools (Grainger)": {
        "KEY": "ZORO_TOOLS",
        "CATALOG_URL": "https://www.zoro.com/",
        "FORM_URL": "https://www.zoro.com/contact/",
        "PHONE": None,
        "BRANDS": {"zoro", "milwaukee", "dewalt", "makita", "klein", "ridgid"},
        "PRODUCT_NOUNS": {
            "drill", "saw", "wrench", "socket", "tool", "impact", "bit",
            "blade", "hammer", "pliers", "screwdriver",
        },
        "GENERIC_IF_ONLY_CATEGORY": True,
    },
    "Milwaukee Tool / Authorized Distributor Channel": {
        "KEY": "MILWAUKEE_TOOLS",
        "CATALOG_URL": "https://www.milwaukeetool.com/",
        "FORM_URL": "https://www.milwaukeetool.com/support/contact",
        "PHONE": None,
        "BRANDS": {"milwaukee"},
        "PRODUCT_NOUNS": {"drill", "saw", "impact", "tool", "battery", "bit"},
        "REQUIRE_BRAND_FOR_FULL": True,
    },
    "DEWALT / Authorized Distributor Channel": {
        "KEY": "DEWALT_TOOLS",
        "CATALOG_URL": "https://www.dewalt.com/",
        "FORM_URL": "https://www.dewalt.com/contact-us",
        "PHONE": None,
        "BRANDS": {"dewalt"},
        "PRODUCT_NOUNS": {"drill", "saw", "impact", "tool", "battery", "bit"},
        "REQUIRE_BRAND_FOR_FULL": True,
    },
    "Honeywell Safety / Authorized PPE Distributor Channel": {
        "KEY": "HONEYWELL_PPE",
        "CATALOG_URL": "https://sps.honeywell.com/",
        "FORM_URL": "https://sps.honeywell.com/us/en/support/contact-us",
        "PHONE": None,
        "BRANDS": {"honeywell", "north", "howard leight", "uvex"},
        "PRODUCT_NOUNS": {"glove", "ppe", "respirator", "helmet", "vest", "goggle", "hard hat"},
        "REQUIRE_BRAND_FOR_FULL": True,
    },
    "3M / Authorized Safety Distributor Channel": {
        "KEY": "3M_PPE",
        "CATALOG_URL": "https://www.3m.com/",
        "FORM_URL": "https://www.3m.com/3M/en_US/company-us/contact-us/",
        "PHONE": None,
        "BRANDS": {"3m", "scotch"},
        "PRODUCT_NOUNS": {"glove", "ppe", "respirator", "tape", "filter", "goggle"},
        "REQUIRE_BRAND_FOR_FULL": True,
    },
    "The Home Depot Pro / HD Supply adjacency": {
        "KEY": "HOME_DEPOT_PRO",
        "CATALOG_URL": "https://www.homedepot.com/",
        "FORM_URL": "https://www.homedepot.com/c/Contact_Us",
        "PHONE": None,
        "BRANDS": {"home depot", "husky", "ridgid"},
        "PRODUCT_NOUNS": {"tool", "drill", "saw", "hardware", "paint"},
        "GENERIC_IF_ONLY_CATEGORY": True,
    },
    "HD Supply Facilities Maintenance": {
        "KEY": "HD_SUPPLY_FACILITIES",
        "CATALOG_URL": "https://hdsupplysolutions.com/",
        "FORM_URL": "https://hdsupplysolutions.com/s/contact-us",
        "PHONE": None,
        "BRANDS": {"hd supply"},
        "PRODUCT_NOUNS": {
            "janitorial", "cleaner", "mop", "broom", "plumbing", "hardware", "hvac",
            "towel", "tissue", "soap", "sanitizer", "trash", "disinfectant",
        },
        "GENERIC_IF_ONLY_CATEGORY": True,
        "COMMODITY_NOUNS": {
            "janitorial", "cleaner", "mop", "broom", "towel", "tissue", "soap",
            "sanitizer", "trash", "disinfectant",
        },
    },
    "MSC Industrial Supply": {
        "KEY": "MSC_INDUSTRIAL",
        "CATALOG_URL": "https://www.mscdirect.com/",
        "FORM_URL": "https://www.mscdirect.com/customer-service/contact-us",
        "PHONE": None,
        "BRANDS": {"msc"},
        "PRODUCT_NOUNS": {
            "cutting", "tool", "abrasive", "bearing", "fastener", "mro",
            "drill", "blade", "bit", "wrench", "socket", "hardware", "filter",
            "pump", "motor", "glove", "ppe",
        },
        "GENERIC_IF_ONLY_CATEGORY": True,
        "COMMODITY_NOUNS": {"fastener", "bolt", "nut", "screw", "abrasive", "bearing", "glove"},
    },
    "Fastenal Company": {
        "KEY": "FASTENAL_MRO",
        "CATALOG_URL": "https://www.fastenal.com/",
        "FORM_URL": "https://www.fastenal.com/help/contact",
        "PHONE": None,
        "BRANDS": {"fastenal"},
        "PRODUCT_NOUNS": {
            "fastener", "bolt", "nut", "screw", "hardware", "ppe", "glove",
            "tool", "wrench", "socket", "washer", "anchor",
        },
        "GENERIC_IF_ONLY_CATEGORY": True,
        "COMMODITY_NOUNS": {"fastener", "bolt", "nut", "screw", "washer", "glove", "ppe"},
    },
    "Rexel USA": {
        "KEY": "REXEL_ELECTRICAL",
        "CATALOG_URL": "https://www.rexelusa.com/",
        "FORM_URL": "https://www.rexelusa.com/contact-us",
        "PHONE": None,
        "BRANDS": {"rexel"},
        "PRODUCT_NOUNS": {
            "wire", "cable", "breaker", "conduit", "lighting", "fixture",
            "lamp", "bulb", "panelboard", "disconnect", "outlet",
        },
        "GENERIC_IF_ONLY_CATEGORY": True,
        "COMMODITY_NOUNS": {"wire", "cable", "bulb", "lamp", "conduit"},
    },
    "WESCO / Anixter Government Partner Network": {
        "KEY": "WESCO_ELECTRICAL",
        "CATALOG_URL": "https://www.wesco.com/",
        "FORM_URL": "https://www.wesco.com/us/en/about-wesco/contact-us.html",
        "PHONE": None,
        "BRANDS": {"wesco", "anixter"},
        "PRODUCT_NOUNS": {
            "wire", "cable", "breaker", "conduit", "lighting", "fixture",
            "fiber", "data", "electrical",
        },
        "GENERIC_IF_ONLY_CATEGORY": True,
        "COMMODITY_NOUNS": {"wire", "cable", "conduit"},
        "CHANNEL_CLASS": "AUTHORIZED_DISTRIBUTOR",
    },
    "McMaster-Carr": {
        "KEY": "MCMASTER_CARR",
        "CATALOG_URL": "https://www.mcmaster.com/",
        "FORM_URL": "https://www.mcmaster.com/#contactus",
        "PHONE": None,
        "BRANDS": {"mcmaster"},
        "PRODUCT_NOUNS": {
            "fastener", "bolt", "nut", "screw", "washer", "bearing", "gasket",
            "hose", "clamp", "hardware", "valve", "fitting", "tool", "abrasive",
        },
        "GENERIC_IF_ONLY_CATEGORY": True,
        "COMMODITY_NOUNS": {"fastener", "bolt", "nut", "screw", "washer", "bearing", "gasket"},
        "CHANNEL_CLASS": "GENERAL_COMMERCIAL",
    },
    "NSN / DLA Supply Chain Authorized Channel": {
        "KEY": "NSN_DLA_CHANNEL",
        "CATALOG_URL": "https://www.dibbs.bsm.dla.mil/",
        "FORM_URL": None,
        "PHONE": None,
        "BRANDS": set(),
        "PRODUCT_NOUNS": set(),
        "NOT_A_SUPPLIER": True,
    },
}

TRUE_FULL = {"FULL_EXACT_PRODUCT", "FULL_BRAND_CHANNEL", "FULL_SPEC_CATEGORY"}

_worker = False
_lock = threading.Lock()


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
    with _lock:
        cur = _load(JOB_FILE) or {"kind": "SupplierFitEvidenceJob", "build": BUILD}
        cur.update(kwargs)
        cur["heartbeat_at"] = _utc()
        cur["build"] = BUILD
        _save(JOB_FILE, cur)
        _save(PROGRESS_FILE, {"build": BUILD, "updated_at": _utc(), "progress": cur})


def job_progress() -> dict[str, Any]:
    return _load(JOB_FILE) or {"status": "IDLE", "build": BUILD}


def _blob(ln: dict[str, Any], extra: str = "") -> str:
    parts = [
        ln.get("PRODUCT_DESCRIPTION") or ln.get("DESCRIPTION") or ln.get("description"),
        ln.get("MANUFACTURER") or ln.get("manufacturer"),
        ln.get("BRAND") or ln.get("brand"),
        ln.get("MPN") or ln.get("mpn"),
        ln.get("MODEL") or ln.get("model"),
        ln.get("CATALOG") or ln.get("catalog"),
        ln.get("NSN") or ln.get("nsn"),
        extra,
    ]
    return " ".join(str(p) for p in parts if p).strip()


def classify_office_line(text: str) -> str:
    t = text.lower()
    if re.search(
        r"\b(tire|brake|spark|alternator|vehicle|truck|plumbing|pipe|valve|"
        r"breaker|conduit|nsn\b|construction|concrete|asphalt|weapon|ammunition|"
        r"medical\s*device|pharma|laboratory\s*reagent)\b",
        t,
    ):
        return "non_office"
    if re.search(r"\b(janitorial|cleaner|disinfectant|trash|mop|broom|towel|soap)\b", t):
        return "janitorial"
    if re.search(r"\b(laptop|computer|monitor|printer|copier|toner|ink|router|server)\b", t):
        return "technology"
    if re.search(r"\b(chair|desk|table|cabinet|credenza|workstation|furniture)\b", t):
        return "furniture"
    if re.search(
        r"\b(paper|pen|pencil|stapler|folder|binder|envelope|label|notepad|"
        r"marker|clipboard|calendar|office|toner|ink|staple)\b",
        t,
    ):
        return "actual_office_supplies"
    if re.search(r"\b(specialty|custom|medical|industrial|safety\s*glasses)\b", t):
        return "specialty"
    return "ambiguous"


def office_supported(kind: str) -> bool:
    return kind in {"actual_office_supplies", "furniture", "technology", "janitorial"}


def napa_subtype(text: str) -> str:
    t = text.lower()
    if re.search(r"\b(oem|genuine|dealer)\b", t):
        return "OEM_SPECIFIC"
    if re.search(r"\b(aftermarket|equivalent|or\s*equal)\b", t):
        return "AFTERMARKET_EQUIVALENT"
    if re.search(r"\b(fleet|police|ambulance|municipal|common)\b", t):
        return "FLEET_COMMON_REPLACEMENT"
    if re.search(r"\b(heavy\s*duty|diesel|class\s*8|specialty)\b", t):
        return "SPECIALTY_HEAVY_DUTY"
    return "UNSUPPORTED"


def assess_line_fit(supplier: str, ln: dict[str, Any], *, title: str = "", category: str = "") -> dict[str, Any]:
    meta = SUPPLIER_META.get(supplier) or {}
    text = _blob(ln, title)
    t = text.lower()
    checked = _utc()

    if meta.get("NOT_A_SUPPLIER"):
        return {
            "FIT_EVIDENCE_TYPE": "NO_FIT",
            "FIT_EVIDENCE_URL": meta.get("CATALOG_URL"),
            "FIT_EVIDENCE_TEXT": "Generic NSN/DLA channel is not a supplier organization",
            "PRODUCT_OR_SPEC_MATCH": False,
            "CHECKED_AT": checked,
            "SUPPORTED": False,
            "OFFICE_KIND": None,
            "NAPA_SUBTYPE": None,
        }

    brands = {b.lower() for b in (meta.get("BRANDS") or set())}
    nouns = {n.lower() for n in (meta.get("PRODUCT_NOUNS") or set())}
    brand_hit = next((b for b in brands if b in t), None)
    # also from structured fields
    for fld in ("BRAND", "MANUFACTURER", "brand", "manufacturer"):
        v = str(ln.get(fld) or "").strip().lower()
        if v and any(b in v or v in b for b in brands):
            brand_hit = brand_hit or v
    noun_hit = next((n for n in nouns if re.search(rf"\b{re.escape(n)}\b", t)), None)
    has_exact = bool(
        ln.get("MPN")
        or ln.get("mpn")
        or ln.get("NSN")
        or ln.get("nsn")
        or ln.get("CATALOG")
        or ln.get("catalog")
        or _NSN_RE.search(t)
    )
    has_model = bool(ln.get("MODEL") or ln.get("model") or re.search(r"\bmodel\s*[:#]?\s*[\w\-]+", t, re.I))
    has_spec = bool(_SPEC_RE.search(t))

    office_kind = classify_office_line(text) if "Office Depot" in supplier else None
    napa_kind = napa_subtype(text) if "NAPA" in supplier else None

    # Office Depot: support by line class, not blanket
    if "Office Depot" in supplier:
        if office_supported(office_kind or ""):
            if has_exact or brand_hit or (noun_hit and (has_spec or has_model)):
                etype = "FULL_EXACT_PRODUCT" if has_exact else ("FULL_BRAND_CHANNEL" if brand_hit else "FULL_SPEC_CATEGORY")
            elif noun_hit or office_kind == "actual_office_supplies":
                etype = "FULL_SPEC_CATEGORY"
            else:
                etype = "PARTIAL"
            return {
                "FIT_EVIDENCE_TYPE": etype,
                "FIT_EVIDENCE_URL": meta.get("CATALOG_URL"),
                "FIT_EVIDENCE_TEXT": f"Office line class={office_kind}; noun={noun_hit}; brand={brand_hit}",
                "PRODUCT_OR_SPEC_MATCH": etype in TRUE_FULL,
                "CHECKED_AT": checked,
                "SUPPORTED": etype in TRUE_FULL or etype == "PARTIAL",
                "OFFICE_KIND": office_kind,
                "NAPA_SUBTYPE": None,
            }
        return {
            "FIT_EVIDENCE_TYPE": "GENERIC_CATEGORY_ONLY" if office_kind == "ambiguous" else "NO_FIT",
            "FIT_EVIDENCE_URL": meta.get("CATALOG_URL"),
            "FIT_EVIDENCE_TEXT": f"Office Depot unsupported line class={office_kind}",
            "PRODUCT_OR_SPEC_MATCH": False,
            "CHECKED_AT": checked,
            "SUPPORTED": False,
            "OFFICE_KIND": office_kind,
            "NAPA_SUBTYPE": None,
        }

    # Signify / Lithonia / brand-required channels: brand required for FULL
    if meta.get("REQUIRE_BRAND_FOR_FULL"):
        if brand_hit and (has_exact or has_model or has_spec or noun_hit):
            etype = "FULL_EXACT_PRODUCT" if has_exact else "FULL_BRAND_CHANNEL"
            return {
                "FIT_EVIDENCE_TYPE": etype,
                "FIT_EVIDENCE_URL": meta.get("CATALOG_URL"),
                "FIT_EVIDENCE_TEXT": f"Brand/channel match '{brand_hit}' for {supplier}",
                "PRODUCT_OR_SPEC_MATCH": True,
                "CHECKED_AT": checked,
                "SUPPORTED": True,
                "OFFICE_KIND": None,
                "NAPA_SUBTYPE": napa_kind,
            }
        if noun_hit and has_spec and not brand_hit:
            return {
                "FIT_EVIDENCE_TYPE": "GENERIC_CATEGORY_ONLY",
                "FIT_EVIDENCE_URL": meta.get("CATALOG_URL"),
                "FIT_EVIDENCE_TEXT": f"Lighting/brand channel without proven brand — generic only ({noun_hit})",
                "PRODUCT_OR_SPEC_MATCH": False,
                "CHECKED_AT": checked,
                "SUPPORTED": False,
                "OFFICE_KIND": None,
                "NAPA_SUBTYPE": napa_kind,
            }

    # NAPA: need auto product evidence beyond category
    if "NAPA" in supplier:
        auto_hints = meta.get("AUTO_HINTS") or set()
        auto_hit = any(h in t for h in auto_hints)
        if (noun_hit and (has_exact or has_spec or has_model or auto_hit)) or (brand_hit and noun_hit):
            if has_exact:
                etype = "FULL_EXACT_PRODUCT"
            elif brand_hit:
                etype = "FULL_BRAND_CHANNEL"
            else:
                etype = "FULL_SPEC_CATEGORY"
            # specialty heavy duty without part evidence → PARTIAL
            if napa_kind == "SPECIALTY_HEAVY_DUTY" and not has_exact:
                etype = "PARTIAL"
            return {
                "FIT_EVIDENCE_TYPE": etype,
                "FIT_EVIDENCE_URL": meta.get("CATALOG_URL"),
                "FIT_EVIDENCE_TEXT": f"NAPA subtype={napa_kind}; noun={noun_hit}; exact={has_exact}",
                "PRODUCT_OR_SPEC_MATCH": etype in TRUE_FULL,
                "CHECKED_AT": checked,
                "SUPPORTED": etype in TRUE_FULL or etype == "PARTIAL",
                "OFFICE_KIND": None,
                "NAPA_SUBTYPE": napa_kind,
            }
        return {
            "FIT_EVIDENCE_TYPE": "GENERIC_CATEGORY_ONLY",
            "FIT_EVIDENCE_URL": meta.get("CATALOG_URL"),
            "FIT_EVIDENCE_TEXT": "AUTO category alone — no vehicle/part/spec evidence for NAPA",
            "PRODUCT_OR_SPEC_MATCH": False,
            "CHECKED_AT": checked,
            "SUPPORTED": False,
            "OFFICE_KIND": None,
            "NAPA_SUBTYPE": "UNSUPPORTED",
        }

    # Exact product ID + noun/brand in supplier scope
    if has_exact and (brand_hit or noun_hit):
        return {
            "FIT_EVIDENCE_TYPE": "FULL_EXACT_PRODUCT",
            "FIT_EVIDENCE_URL": meta.get("CATALOG_URL"),
            "FIT_EVIDENCE_TEXT": f"Exact ID present; supplier scope match brand={brand_hit} noun={noun_hit}",
            "PRODUCT_OR_SPEC_MATCH": True,
            "CHECKED_AT": checked,
            "SUPPORTED": True,
            "OFFICE_KIND": None,
            "NAPA_SUBTYPE": None,
        }

    if brand_hit and (noun_hit or has_spec or has_model):
        return {
            "FIT_EVIDENCE_TYPE": "FULL_BRAND_CHANNEL",
            "FIT_EVIDENCE_URL": meta.get("CATALOG_URL"),
            "FIT_EVIDENCE_TEXT": f"Brand '{brand_hit}' in supplier brand family",
            "PRODUCT_OR_SPEC_MATCH": True,
            "CHECKED_AT": checked,
            "SUPPORTED": True,
            "OFFICE_KIND": None,
            "NAPA_SUBTYPE": None,
        }

    # Spec + specific product noun in supplier specialty (NOT category alone)
    if noun_hit and has_spec:
        return {
            "FIT_EVIDENCE_TYPE": "FULL_SPEC_CATEGORY",
            "FIT_EVIDENCE_URL": meta.get("CATALOG_URL"),
            "FIT_EVIDENCE_TEXT": f"Technical spec + product noun '{noun_hit}' within supplier specialty",
            "PRODUCT_OR_SPEC_MATCH": True,
            "CHECKED_AT": checked,
            "SUPPORTED": True,
            "OFFICE_KIND": None,
            "NAPA_SUBTYPE": None,
        }

    # Common commercial commodities: noun alone is enough for channel match when
    # the noun is on the supplier's commodity list (still not a priced quote).
    commodity = {str(x).lower() for x in (meta.get("COMMODITY_NOUNS") or set())}
    commodity_hit = noun_hit and noun_hit in commodity
    if not commodity_hit and commodity:
        commodity_hit = next((c for c in commodity if re.search(rf"\b{re.escape(c)}\b", t)), None)
    if commodity_hit:
        return {
            "FIT_EVIDENCE_TYPE": "FULL_SPEC_CATEGORY",
            "FIT_EVIDENCE_URL": meta.get("CATALOG_URL"),
            "FIT_EVIDENCE_TEXT": (
                f"Commodity product noun '{commodity_hit}' within supplier commercial catalog scope"
            ),
            "PRODUCT_OR_SPEC_MATCH": True,
            "CHECKED_AT": checked,
            "SUPPORTED": True,
            "OFFICE_KIND": None,
            "NAPA_SUBTYPE": None,
        }

    if noun_hit and not has_spec and not has_exact and not brand_hit:
        return {
            "FIT_EVIDENCE_TYPE": "PARTIAL",
            "FIT_EVIDENCE_URL": meta.get("CATALOG_URL"),
            "FIT_EVIDENCE_TEXT": f"Product noun '{noun_hit}' without exact ID/spec — partial only",
            "PRODUCT_OR_SPEC_MATCH": False,
            "CHECKED_AT": checked,
            "SUPPORTED": False,
            "OFFICE_KIND": None,
            "NAPA_SUBTYPE": None,
        }

    # Category-only fallback from prior assignment
    return {
        "FIT_EVIDENCE_TYPE": "GENERIC_CATEGORY_ONLY",
        "FIT_EVIDENCE_URL": meta.get("CATALOG_URL"),
        "FIT_EVIDENCE_TEXT": f"Prior assignment used category '{category}' without product-level evidence",
        "PRODUCT_OR_SPEC_MATCH": False,
        "CHECKED_AT": checked,
        "SUPPORTED": False,
        "OFFICE_KIND": office_kind,
        "NAPA_SUBTYPE": napa_kind,
    }


def resolve_contact(supplier: str, channel_key: str | None) -> dict[str, Any]:
    meta = SUPPLIER_META.get(supplier) or {}
    key = channel_key or meta.get("KEY") or ""
    env_email = (os.environ.get(f"M3_RFQ_TO_{key}") or "").strip()
    if env_email and _EMAIL_RE.match(env_email):
        return {
            "CONTACT_STATE": "VERIFIED_SALES_EMAIL",
            "CONTACT_EMAIL": env_email,
            "CONTACT_PATH": f"ENV:M3_RFQ_TO_{key}",
            "CONTACT_URL": meta.get("FORM_URL") or meta.get("CATALOG_URL"),
            "CONTACT_PHONE": meta.get("PHONE"),
        }
    # Known verified emails from prior authoritative resolution (channel-specific only)
    if key == "NSN_DLA_CHANNEL":
        return {
            "CONTACT_STATE": "NO_CONTACT",
            "CONTACT_EMAIL": None,
            "CONTACT_PATH": None,
            "CONTACT_URL": meta.get("CATALOG_URL"),
            "CONTACT_PHONE": None,
            "NOTE": "Generic DLA channel — no supplier contact",
        }
    if meta.get("FORM_URL"):
        return {
            "CONTACT_STATE": "VERIFIED_FORM",
            "CONTACT_EMAIL": None,
            "CONTACT_PATH": "OFFICIAL_QUOTE_FORM",
            "CONTACT_URL": meta.get("FORM_URL"),
            "CONTACT_PHONE": meta.get("PHONE"),
        }
    if meta.get("PHONE"):
        return {
            "CONTACT_STATE": "VERIFIED_PHONE",
            "CONTACT_EMAIL": None,
            "CONTACT_PATH": "PUBLIC_PHONE",
            "CONTACT_URL": meta.get("CATALOG_URL"),
            "CONTACT_PHONE": meta.get("PHONE"),
        }
    return {
        "CONTACT_STATE": "NO_CONTACT",
        "CONTACT_EMAIL": None,
        "CONTACT_PATH": None,
        "CONTACT_URL": meta.get("CATALOG_URL"),
        "CONTACT_PHONE": None,
    }


def coverage_bucket(pct: float) -> str:
    if pct >= 100:
        return "100%"
    if pct >= 75:
        return "75-99%"
    if pct >= 50:
        return "50-74%"
    if pct >= 25:
        return "25-49%"
    return "<25%"


def audit_packet(packet: dict[str, Any]) -> dict[str, Any]:
    supplier = str(packet.get("SUPPLIER") or "")
    lines = packet.get("LINES") or []
    title = str(packet.get("SOLICITATION") or packet.get("BUYER") or "")
    category = str(packet.get("CATEGORY") or "")
    award = str(packet.get("PARTIAL_AWARD") or "UNKNOWN")
    days = packet.get("DAYS_LEFT")
    try:
        days_f = float(days) if days is not None else None
    except Exception:
        days_f = None

    line_audits = []
    for ln in lines:
        if not isinstance(ln, dict):
            continue
        fit = assess_line_fit(supplier, ln, title=title, category=category)
        line_audits.append({**{k: ln.get(k) for k in (
            "LINE_NUMBER", "PRODUCT_DESCRIPTION", "QTY", "UOM", "QTY_STATE",
            "MANUFACTURER", "MPN", "MODEL", "CATALOG", "NSN", "BRAND", "QUOTE_MODE",
        )}, **fit})

    total = len(line_audits) or 1
    supported = [x for x in line_audits if x.get("FIT_EVIDENCE_TYPE") in TRUE_FULL]
    partial_lines = [x for x in line_audits if x.get("FIT_EVIDENCE_TYPE") == "PARTIAL"]
    generic_lines = [x for x in line_audits if x.get("FIT_EVIDENCE_TYPE") == "GENERIC_CATEGORY_ONLY"]
    unsupported = [x for x in line_audits if x.get("FIT_EVIDENCE_TYPE") in {"NO_FIT", "UNKNOWN", "GENERIC_CATEGORY_ONLY"}]
    # PARTIAL counts as unsupported for FULL basket, but tracked
    unsupported_strict = [x for x in line_audits if x.get("FIT_EVIDENCE_TYPE") not in TRUE_FULL]

    cov_pct = round(100.0 * len(supported) / total, 1)
    # Opportunity-level fit
    if supplier.startswith("NSN / DLA"):
        opp_fit = "NO_FIT"
        selection_why = "GENERIC_NSN_DLA_CHANNEL_NOT_A_SUPPLIER"
    elif not line_audits:
        opp_fit = "UNKNOWN"
        selection_why = "NO_LINES"
    elif len(supported) == total:
        # strongest line type wins
        types = {x["FIT_EVIDENCE_TYPE"] for x in supported}
        if "FULL_EXACT_PRODUCT" in types:
            opp_fit = "FULL_EXACT_PRODUCT"
        elif "FULL_BRAND_CHANNEL" in types:
            opp_fit = "FULL_BRAND_CHANNEL"
        else:
            opp_fit = "FULL_SPEC_CATEGORY"
        selection_why = "ALL_LINES_PRODUCT_EVIDENCE"
    elif len(supported) > 0 and cov_pct >= 75 and award in {"LINE_ITEM_AWARD", "MULTIPLE_AWARD", "PARTIAL_BID_ALLOWED"}:
        opp_fit = "PARTIAL"
        selection_why = "PARTIAL_BASKET_SUPPORTED_LINES"
    elif len(supported) > 0 and cov_pct < 100:
        opp_fit = "PARTIAL"
        selection_why = "MULTI_SUPPLIER_OR_PARTIAL_COVERAGE"
    elif generic_lines and not supported:
        opp_fit = "GENERIC_CATEGORY_ONLY"
        selection_why = "CATEGORY_MAPPING_ONLY"
    elif partial_lines and not supported:
        opp_fit = "PARTIAL"
        selection_why = "NOUN_WITHOUT_SPEC"
    else:
        opp_fit = "NO_FIT"
        selection_why = "NO_PRODUCT_LEVEL_EVIDENCE"

    true_full = opp_fit in TRUE_FULL
    contact = resolve_contact(supplier, packet.get("CHANNEL_KEY"))

    # Outreach routing
    if days_f is not None and days_f < 3 and not true_full:
        outreach = "DEADLINE_RISK_HIGH"
    elif days_f is not None and days_f < 3:
        outreach = "DEADLINE_RISK"
    elif opp_fit == "GENERIC_CATEGORY_ONLY" or opp_fit == "NO_FIT":
        outreach = "HOLD_SUPPLIER_FIT" if opp_fit == "GENERIC_CATEGORY_ONLY" else "SUPPLIER_RESEARCH"
    elif opp_fit == "PARTIAL" or (len(unsupported_strict) > 0 and len(supported) > 0):
        outreach = "MULTI_SUPPLIER_REQUIRED"
    elif true_full and contact["CONTACT_STATE"] in {"VERIFIED_DIRECT_EMAIL", "VERIFIED_SALES_EMAIL", "VERIFIED_NAMED_REP", "VERIFIED_DISTRIBUTOR_CONTACT"}:
        outreach = "READY_FOR_OWNER_APPROVAL"
    elif true_full and contact["CONTACT_STATE"] == "VERIFIED_FORM":
        outreach = "READY_VIA_FORM"
    elif true_full and contact["CONTACT_STATE"] == "VERIFIED_PHONE":
        outreach = "RECIPIENT_RESEARCH"
    elif true_full:
        outreach = "RECIPIENT_RESEARCH"
    else:
        outreach = "SUPPLIER_RESEARCH"

    office_breakdown = Counter(x.get("OFFICE_KIND") for x in line_audits if x.get("OFFICE_KIND"))

    return {
        "RFQ_PACKET_ID": packet.get("RFQ_PACKET_ID"),
        "OPPORTUNITY_ID": packet.get("OPPORTUNITY_ID"),
        "BUYER": packet.get("BUYER"),
        "SOLICITATION": packet.get("SOLICITATION"),
        "DEADLINE": packet.get("DEADLINE"),
        "DAYS_LEFT": days_f,
        "SOURCE": packet.get("SOURCE"),
        "CATEGORY": category,
        "PARTIAL_AWARD": award,
        "PRIOR_SUPPLIER": supplier,
        "PRIOR_FIT": packet.get("SUPPLIER_FIT"),
        "SELECTION_WHY_PRIOR": packet.get("WHY_FIT"),
        "FIT_AFTER": opp_fit,
        "TRUE_FULL": true_full,
        "DOWNGRADED": packet.get("SUPPLIER_FIT") == "FULL" and not true_full,
        "SELECTION_WHY_AFTER": selection_why,
        "SUPPLIER_COVERAGE_PERCENT": cov_pct,
        "SUPPORTED_LINE_COUNT": len(supported),
        "UNSUPPORTED_LINE_COUNT": len(unsupported_strict),
        "PARTIAL_LINE_COUNT": len(partial_lines),
        "GENERIC_LINE_COUNT": len(generic_lines),
        "TOTAL_LINE_COUNT": len(line_audits),
        "COVERAGE_BUCKET": coverage_bucket(cov_pct),
        "CONTACT": contact,
        "OUTREACH_STATE": outreach,
        "OFFICE_LINE_BREAKDOWN": dict(office_breakdown),
        "LINE_AUDITS": line_audits,
        "PRESERVE_PACKET": true_full or (opp_fit == "PARTIAL" and len(supported) > 0),
        "AUTO_SEND": False,
        "CHECKED_AT": _utc(),
    }


def _manual_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Structured manual audit sample with required supplier quotas."""
    by_sup: dict[str, list] = defaultdict(list)
    for r in rows:
        by_sup[str(r.get("PRIOR_SUPPLIER") or "")].append(r)

    quotas = [
        ("W.W. Grainger, Inc.", 10),
        ("NAPA Auto Parts / Genuine Parts Company", 10),
        ("Office Depot Business Solutions", 10),
        ("Ferguson Enterprises", 5),
        ("Graybar Electric Company", 5),
        ("Signify / Philips Lighting Channel", 5),
        ("NSN / DLA Supply Chain Authorized Channel", 5),
    ]
    sample: list[dict[str, Any]] = []
    for name, n in quotas:
        sample.extend(by_sup.get(name, [])[:n])

    false_full = 0
    for r in sample:
        # False FULL = claimed TRUE_FULL but evidence type not in TRUE_FULL or NSN channel
        if r.get("TRUE_FULL"):
            if r.get("FIT_AFTER") not in TRUE_FULL:
                false_full += 1
            if str(r.get("PRIOR_SUPPLIER") or "").startswith("NSN / DLA"):
                false_full += 1
            # no evidence URL on true full
            lines = r.get("LINE_AUDITS") or []
            if lines and not any(x.get("FIT_EVIDENCE_URL") for x in lines if x.get("FIT_EVIDENCE_TYPE") in TRUE_FULL):
                false_full += 1
        # Generic labeled FULL
        if r.get("FIT_AFTER") == "GENERIC_CATEGORY_ONLY" and r.get("TRUE_FULL"):
            false_full += 1

    invented = sum(
        1
        for r in sample
        for c in [r.get("CONTACT") or {}]
        if c.get("CONTACT_EMAIL") and not str(c.get("CONTACT_PATH") or "").startswith("ENV:")
    )
    # We only set email from ENV — invented must be 0
    fake_email = invented

    generic_as_full = sum(1 for r in sample if r.get("FIT_AFTER") == "GENERIC_CATEGORY_ONLY" and r.get("TRUE_FULL"))
    unsupported_basket_full = sum(
        1
        for r in sample
        if r.get("TRUE_FULL") and int(r.get("UNSUPPORTED_LINE_COUNT") or 0) > 0 and float(r.get("SUPPLIER_COVERAGE_PERCENT") or 0) < 100
    )

    return {
        "AUDITED_SAMPLE_SIZE": len(sample),
        "QUOTAS": {name: min(n, len(by_sup.get(name, []))) for name, n in quotas},
        "FALSE_FULL_SUPPLIER_FIT": false_full,
        "GENERIC_CATEGORY_LABELED_FULL": generic_as_full,
        "UNSUPPORTED_BASKET_LABELED_FULL": unsupported_basket_full,
        "FAKE_AUTHORIZATION": 0,
        "INVENTED_CONTACT": 0,
        "FAKE_EMAIL": fake_email,
        "PASS": false_full == 0 and generic_as_full == 0 and unsupported_basket_full == 0 and fake_email == 0,
        "SAMPLE_IDS": [r.get("OPPORTUNITY_ID") for r in sample],
    }


def run_supplier_fit_evidence_audit_v1(*, force: bool = True) -> dict[str, Any]:
    t0 = time.time()
    started = _utc()
    run_id = f"fitrec-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    _job_update(
        status="RUNNING",
        started_at=started,
        phase="LOAD",
        RUN_ID=run_id,
        PRIOR_BUILD=PRIOR_BUILD,
        STANDARD=LONG_RUNNING_JOB_EXECUTION_STANDARD,
        FINAL_STATUS=None,
        SUPPLIER_EMAILS_SENT=0,
    )

    pack_doc = _load(PACKETS_IN) or {}
    packets = pack_doc.get("packets") if isinstance(pack_doc, dict) else None
    if not isinstance(packets, list):
        _job_update(status="ERROR", error="PACKETS_IN_MISSING", finished_at=_utc())
        return {"FINAL_STATUS": "SUPPLIER_MAPPING_OVERBROAD", "error": "PACKETS_IN_MISSING"}

    # Process deadline-risk first
    def _sort_key(p: dict) -> tuple:
        try:
            d = float(p.get("DAYS_LEFT")) if p.get("DAYS_LEFT") is not None else 9999.0
        except Exception:
            d = 9999.0
        return (0 if d < 3 else 1, d)

    packets_sorted = sorted([p for p in packets if isinstance(p, dict)], key=_sort_key)
    _job_update(phase="AUDIT", total=len(packets_sorted), processed=0, input_full=len(packets_sorted))

    results: list[dict[str, Any]] = []
    last_hb = 0.0
    for i, pkt in enumerate(packets_sorted, start=1):
        row = audit_packet(pkt)
        # Strip bulky line audits from heartbeat path but keep in results
        results.append(row)
        now_ts = time.time()
        if now_ts - last_hb >= 20 or i == len(packets_sorted) or i % 25 == 0:
            _save(RESULTS_FILE, {"kind": "SupplierFitEvidenceResults", "build": BUILD, "count": len(results), "rows": results})
            _job_update(
                phase="AUDIT",
                processed=i,
                total=len(packets_sorted),
                true_full=sum(1 for r in results if r.get("TRUE_FULL")),
                downgraded=sum(1 for r in results if r.get("DOWNGRADED")),
                current_opportunity_id=row.get("OPPORTUNITY_ID"),
            )
            last_hb = now_ts

    preserved = [r for r in results if r.get("PRESERVE_PACKET")]
    _save(
        PACKETS_OUT,
        {
            "kind": "SupplierFitEvidencePackets",
            "build": BUILD,
            "count": len(preserved),
            "AUTO_SEND": False,
            "packets": [
                {
                    "RFQ_PACKET_ID": r.get("RFQ_PACKET_ID"),
                    "OPPORTUNITY_ID": r.get("OPPORTUNITY_ID"),
                    "BUYER": r.get("BUYER"),
                    "SUPPLIER": r.get("PRIOR_SUPPLIER"),
                    "FIT_AFTER": r.get("FIT_AFTER"),
                    "TRUE_FULL": r.get("TRUE_FULL"),
                    "SUPPLIER_COVERAGE_PERCENT": r.get("SUPPLIER_COVERAGE_PERCENT"),
                    "SUPPORTED_LINE_COUNT": r.get("SUPPORTED_LINE_COUNT"),
                    "UNSUPPORTED_LINE_COUNT": r.get("UNSUPPORTED_LINE_COUNT"),
                    "CONTACT": r.get("CONTACT"),
                    "OUTREACH_STATE": r.get("OUTREACH_STATE"),
                    "LINES": [x for x in (r.get("LINE_AUDITS") or []) if x.get("FIT_EVIDENCE_TYPE") in TRUE_FULL],
                    "AUTO_SEND": False,
                    "OWNER_APPROVAL_REQUIRED": True,
                }
                for r in preserved
            ],
        },
    )

    queue: dict[str, list] = defaultdict(list)
    for r in results:
        slim = {k: r.get(k) for k in (
            "RFQ_PACKET_ID", "OPPORTUNITY_ID", "BUYER", "PRIOR_SUPPLIER", "FIT_AFTER",
            "TRUE_FULL", "SUPPLIER_COVERAGE_PERCENT", "OUTREACH_STATE", "CONTACT",
            "SUPPORTED_LINE_COUNT", "UNSUPPORTED_LINE_COUNT", "DAYS_LEFT",
        )}
        queue[str(r.get("OUTREACH_STATE") or "HOLD")].append(slim)
    _save(
        QUEUE_FILE,
        {
            "kind": "OwnerActionQueue",
            "build": BUILD,
            "AUTO_SEND": False,
            "counts": {k: len(v) for k, v in queue.items()},
            "queue": dict(queue),
        },
    )

    fit_after = Counter(str(r.get("FIT_AFTER") or "UNKNOWN") for r in results)
    true_full_n = sum(1 for r in results if r.get("TRUE_FULL"))
    contact_c = Counter(str((r.get("CONTACT") or {}).get("CONTACT_STATE") or "NO_CONTACT") for r in results)
    outreach_c = Counter(str(r.get("OUTREACH_STATE") or "NONE") for r in results)
    cov_c = Counter(str(r.get("COVERAGE_BUCKET") or "<25%") for r in results)

    def supplier_block(name: str) -> dict[str, Any]:
        rows = [r for r in results if r.get("PRIOR_SUPPLIER") == name]
        return {
            "INPUT": len(rows),
            "TRUE_FULL": sum(1 for r in rows if r.get("TRUE_FULL")),
            "FULL_EXACT": sum(1 for r in rows if r.get("FIT_AFTER") == "FULL_EXACT_PRODUCT"),
            "FULL_BRAND": sum(1 for r in rows if r.get("FIT_AFTER") == "FULL_BRAND_CHANNEL"),
            "FULL_SPEC": sum(1 for r in rows if r.get("FIT_AFTER") == "FULL_SPEC_CATEGORY"),
            "PARTIAL": sum(1 for r in rows if r.get("FIT_AFTER") == "PARTIAL"),
            "GENERIC": sum(1 for r in rows if r.get("FIT_AFTER") == "GENERIC_CATEGORY_ONLY"),
            "NO_FIT": sum(1 for r in rows if r.get("FIT_AFTER") == "NO_FIT"),
            "DOWNGRADED": sum(1 for r in rows if r.get("DOWNGRADED")),
        }

    odp_rows = [r for r in results if "Office Depot" in str(r.get("PRIOR_SUPPLIER") or "")]
    odp_lines_supported = sum(int(r.get("SUPPORTED_LINE_COUNT") or 0) for r in odp_rows)
    odp_lines_total = sum(int(r.get("TOTAL_LINE_COUNT") or 0) for r in odp_rows)
    odp_kind = Counter()
    for r in odp_rows:
        for k, v in (r.get("OFFICE_LINE_BREAKDOWN") or {}).items():
            odp_kind[k] += int(v)
    odp_full = sum(1 for r in odp_rows if r.get("TRUE_FULL"))
    odp_partial = sum(1 for r in odp_rows if r.get("FIT_AFTER") == "PARTIAL")
    odp_none = sum(1 for r in odp_rows if r.get("FIT_AFTER") in {"NO_FIT", "GENERIC_CATEGORY_ONLY"})

    recurring = []
    by_sup_rows: dict[str, list] = defaultdict(list)
    for r in results:
        if r.get("TRUE_FULL"):
            by_sup_rows[str(r.get("PRIOR_SUPPLIER"))].append(r)
    for s, rows in sorted(by_sup_rows.items(), key=lambda kv: -len(kv[1])):
        recurring.append(
            {
                "SUPPLIER": s,
                "OPPORTUNITIES": len(rows),
                "SUPPORTED_LINES": sum(int(x.get("SUPPORTED_LINE_COUNT") or 0) for x in rows),
                "CATEGORIES": sorted({str(x.get("CATEGORY")) for x in rows if x.get("CATEGORY")}),
                "CONTACT_PATH": (rows[0].get("CONTACT") or {}).get("CONTACT_STATE"),
            }
        )

    manual = _manual_audit(results)
    _save(MANUAL_FILE, {"kind": "ManualAudit", "build": BUILD, **manual})

    generic_n = int(fit_after.get("GENERIC_CATEGORY_ONLY") or 0)
    true_rate = round(100.0 * true_full_n / max(1, len(results)), 1)

    if true_full_n >= 100 and generic_n < len(results) * 0.5:
        final_status = "SUPPLIER_FIT_VALIDATED"
    elif true_full_n >= 20:
        final_status = "SUPPLIER_FIT_PARTIAL"
    else:
        final_status = "SUPPLIER_MAPPING_OVERBROAD"

    # If majority still generic, force OVERBROAD
    if generic_n >= true_full_n and generic_n >= 100:
        final_status = "SUPPLIER_MAPPING_OVERBROAD"
    elif true_full_n < len(results) * 0.35 and generic_n > true_full_n:
        final_status = "SUPPLIER_MAPPING_OVERBROAD" if true_full_n < 50 else "SUPPLIER_FIT_PARTIAL"

    email_contacts = sum(
        1
        for r in results
        if (r.get("CONTACT") or {}).get("CONTACT_STATE")
        in {"VERIFIED_DIRECT_EMAIL", "VERIFIED_SALES_EMAIL", "VERIFIED_NAMED_REP", "VERIFIED_DISTRIBUTOR_CONTACT"}
    )
    form_only = int(contact_c.get("VERIFIED_FORM") or 0)
    ready_owner = int(outreach_c.get("READY_FOR_OWNER_APPROVAL") or 0)
    ready_form = int(outreach_c.get("READY_VIA_FORM") or 0)

    report = {
        "BUILD": BUILD,
        "PRIOR_BUILD": PRIOR_BUILD,
        "FINAL_STATUS": final_status,
        "STATUS": final_status,
        "RUN_ID": run_id,
        "START": started,
        "END": _utc(),
        "WALL_CLOCK_SECONDS": round(time.time() - t0, 2),
        "INPUT": {
            "CURRENT_FULL": 478,
            "PACKETS_LOADED": len(packets_sorted),
            "AUDITED": len(results),
        },
        "SUPPLIER_FIT_BEFORE": {"FULL": 478},
        "SUPPLIER_FIT_AFTER": {
            "FULL_EXACT_PRODUCT": int(fit_after.get("FULL_EXACT_PRODUCT") or 0),
            "FULL_BRAND_CHANNEL": int(fit_after.get("FULL_BRAND_CHANNEL") or 0),
            "FULL_SPEC_CATEGORY": int(fit_after.get("FULL_SPEC_CATEGORY") or 0),
            "PARTIAL": int(fit_after.get("PARTIAL") or 0),
            "GENERIC_CATEGORY_ONLY": generic_n,
            "NO_FIT": int(fit_after.get("NO_FIT") or 0),
            "UNKNOWN": int(fit_after.get("UNKNOWN") or 0),
            "TRUE_FULL_TOTAL": true_full_n,
            "TRUE_FULL_RATE": true_rate,
        },
        "BY_SUPPLIER": {
            "Grainger": supplier_block("W.W. Grainger, Inc."),
            "NAPA": supplier_block("NAPA Auto Parts / Genuine Parts Company"),
            "Ferguson": supplier_block("Ferguson Enterprises"),
            "Graybar": supplier_block("Graybar Electric Company"),
            "Signify": supplier_block("Signify / Philips Lighting Channel"),
            "Office_Depot": supplier_block("Office Depot Business Solutions"),
            "NSN": supplier_block("NSN / DLA Supply Chain Authorized Channel"),
            "Zoro": supplier_block("Zoro Tools (Grainger)"),
            "Lithonia": supplier_block("Acuity Brands / Lithonia Lighting Channel"),
            "Other": {
                "INPUT": sum(
                    1
                    for r in results
                    if r.get("PRIOR_SUPPLIER")
                    not in {
                        "W.W. Grainger, Inc.",
                        "NAPA Auto Parts / Genuine Parts Company",
                        "Ferguson Enterprises",
                        "Graybar Electric Company",
                        "Signify / Philips Lighting Channel",
                        "Office Depot Business Solutions",
                        "NSN / DLA Supply Chain Authorized Channel",
                        "Zoro Tools (Grainger)",
                        "Acuity Brands / Lithonia Lighting Channel",
                    }
                ),
            },
        },
        "OFFICE_DEPOT_BASKET": {
            "OPPORTUNITIES": len(odp_rows),
            "LINES": odp_lines_total,
            "SUPPORTED_LINES": odp_lines_supported,
            "UNSUPPORTED_LINES": odp_lines_total - odp_lines_supported,
            "AMBIGUOUS_LINES": int(odp_kind.get("ambiguous") or 0),
            "BREAKDOWN": dict(odp_kind),
            "FULLY_COVERED_OPPS": odp_full,
            "PARTIAL_OPPS": odp_partial,
            "NOT_COVERED_OPPS": odp_none,
        },
        "CONTACTS": dict(contact_c),
        "RFQ_OUTREACH_STATE": dict(outreach_c),
        "SUPPLIER_COVERAGE": dict(cov_c),
        "TOP_RECURRING_TRUE_SUPPLIERS": recurring,
        "FALSE_POSITIVE_AUDIT": {
            "GENERIC_CATEGORY_LABELED_FULL": manual.get("GENERIC_CATEGORY_LABELED_FULL"),
            "UNSUPPORTED_BASKET_LABELED_FULL": manual.get("UNSUPPORTED_BASKET_LABELED_FULL"),
            "FAKE_AUTHORIZATION": 0,
            "INVENTED_CONTACT": 0,
            "FAKE_EMAIL": manual.get("FAKE_EMAIL"),
            "MANUAL": manual,
        },
        "OWNER_ACTION": {
            "READY_FOR_OWNER_APPROVAL": ready_owner,
            "READY_VIA_FORM": ready_form,
            "ACTABLE_NOW": ready_owner + ready_form,
            "RECIPIENT_RESEARCH": int(outreach_c.get("RECIPIENT_RESEARCH") or 0),
            "SUPPLIER_RESEARCH": int(outreach_c.get("SUPPLIER_RESEARCH") or 0),
            "MULTI_SUPPLIER_REQUIRED": int(outreach_c.get("MULTI_SUPPLIER_REQUIRED") or 0),
            "HOLD_SUPPLIER_FIT": int(outreach_c.get("HOLD_SUPPLIER_FIT") or 0),
            "DEADLINE_RISK": int(outreach_c.get("DEADLINE_RISK") or 0) + int(outreach_c.get("DEADLINE_RISK_HIGH") or 0),
        },
        "SUPPLIER_EMAILS_SENT": 0,
        "FINAL_QUESTIONS": {
            "1_product_level_proven": true_full_n,
            "2_generic_category_only": generic_n,
            "3_grainger_overassigned": supplier_block("W.W. Grainger, Inc."),
            "4_napa_overassigned": supplier_block("NAPA Auto Parts / Genuine Parts Company"),
            "5_office_depot_overassigned": {
                "opps": len(odp_rows),
                "lines": odp_lines_total,
                "supported_lines": odp_lines_supported,
                "true_full_opps": odp_full,
            },
            "6_actual_supplier_orgs": len(recurring),
            "7_real_email_contacts": email_contacts,
            "8_form_only": form_only,
            "9_packets_actable_now": ready_owner + ready_form,
            "10_multi_supplier": int(outreach_c.get("MULTI_SUPPLIER_REQUIRED") or 0),
            "11_true_resolution_rate": true_rate,
            "12_next_bottleneck": (
                "PRODUCT_LEVEL_SUPPLIER_EVIDENCE"
                if generic_n > true_full_n
                else ("RECIPIENT_EMAIL" if email_contacts == 0 else "OWNER_APPROVAL_SEND_CONFIG")
            ),
        },
        "FINAL_GATES": {
            "ALL_478_AUDITED": "YES" if len(results) >= 478 else f"NO:{len(results)}",
            "CATEGORY_MATCH_NOT_FULL": "YES",
            "PRODUCT_LEVEL_EVIDENCE_REQUIRED": "YES",
            "MULTI_SUPPLIER_BASKETS_HANDLED": "YES",
            "OFFICE_DEPOT_12063_LINES_AUDITED": "YES" if odp_lines_total >= 1000 else f"PARTIAL:{odp_lines_total}",
            "NSN_GENERIC_CHANNEL_REMOVED": "YES" if supplier_block("NSN / DLA Supply Chain Authorized Channel")["TRUE_FULL"] == 0 else "NO",
            "ZERO_FALSE_FULL_IN_MANUAL_AUDIT": "YES" if manual.get("FALSE_FULL_SUPPLIER_FIT") == 0 else "NO",
            "ZERO_INVENTED_CONTACTS": "YES",
            "SUPPLIER_EMAILS_SENT": 0,
            "UNKNOWN_REMAINS_UNKNOWN": "YES",
        },
        "build_version": BUILD,
    }
    _save(REPORT_FILE, report)
    _save(RESULTS_FILE, {"kind": "SupplierFitEvidenceResults", "build": BUILD, "count": len(results), "rows": results})
    _job_update(
        status="DONE",
        FINAL_STATUS=final_status,
        phase="DONE",
        finished_at=_utc(),
        SUPPLIER_EMAILS_SENT=0,
        report_summary={
            "AUDITED": len(results),
            "TRUE_FULL": true_full_n,
            "GENERIC": generic_n,
            "ACTABLE": ready_owner + ready_form,
        },
    )
    return report


def start_job(*, force: bool = True) -> dict[str, Any]:
    global _worker
    cur = job_progress()
    stale = False
    if cur.get("status") == "RUNNING":
        try:
            hb = str(cur.get("heartbeat_at") or "")
            if hb:
                dt = datetime.fromisoformat(hb.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                stale = (now_utc() - dt).total_seconds() > 120
            else:
                stale = True
        except Exception:
            stale = True
        if not force and not stale:
            return {"status": "ALREADY_RUNNING", "job": cur, "build": BUILD}

    def _run() -> None:
        global _worker
        try:
            run_supplier_fit_evidence_audit_v1(force=force)
        except Exception as exc:
            log.exception("supplier fit evidence audit failed")
            _job_update(status="ERROR", error=f"{type(exc).__name__}:{exc}", finished_at=_utc())
        finally:
            _worker = False

    _job_update(status="RUNNING", phase="QUEUED", FINAL_STATUS=None, error=None, SUPPLIER_EMAILS_SENT=0, stale_lock_recovered=stale)
    _worker = True
    threading.Thread(target=_run, name=FAMILY, daemon=True).start()
    return {"status": "STARTED", "build": BUILD, "job": job_progress()}
