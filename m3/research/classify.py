"""Centralized requirement classification — Stage 3 (never a Stage 1 discovery gate)."""

from __future__ import annotations

import re
from typing import Any

# Product vocabulary for classification evidence only — not an adapter retrieval gate.
# Bare nouns alone do NOT prove a product purchase; see find_meaningful_product_hints().
PRODUCT_HINTS = re.compile(
    r"(?i)\b("
    r"furniture|desk|chair|janitorial supplies?|custodial supplies?|cleaning supplies?|"
    r"tools?|mro|electrical supplies?|plumbing supplies?|hvac|appliances?|"
    r"refrigerators?|freezer|washer|dryer|range|"
    r"laptops?|computers?|printers?|toner|ppe|safety equipment|lighting|generators?|"
    r"forklifts?|boom\s+lifts?|equipment|supplies?|hardware|batteries|battery|shelving|"
    r"parts|lubricants?|oils?|filters?|uniforms?|gloves?|paper|commodity|goods|materials|"
    r"fixtures?|scanners?|analyzers?|multiplexers?|pumps?|valves?|transformers?|cables?|wires?|"
    r"vehicles?|tires?|fertilizer|grass\s+seed|landscaping\s+items?|rock\s+salt|treated\s+salt|salt\b|"
    r"screws?|fasteners?|bolts?|nuts?|washers?|butterfly valve|ball valve|gate valve|"
    r"commercial laundry|fitness equipment|material handling|grounds equipment|"
    r"industrial equipment|ice machine|ice maker|construction materials?|"
    r"reach\s+trucks?|pallet\s+trucks?|chargers?"
    r")\b"
)

# Product-noun hits that are legal/facility/context noise — never prove a goods purchase.
PRODUCT_HINT_NOISE_CONTEXT = re.compile(
    r"(?i)("
    r"california\s+vehicle\s+code|vehicle\s+code|"
    r"commercial\s+vehicle\s+enforcement(?:\s+facility)?|"
    r"vehicle\s+enforcement\s+facility|\bCVEF\b|"
    r"motor\s+vehicle\s+(?:code|department|division|act)|"
    r"department\s+of\s+motor\s+vehicles|\bDMV\b"
    r")"
)

# Primary procurement ACTION is service/labor/qualification — product nouns are the object,
# not the deliverable. Takes precedence over bare PRODUCT_HINTS matches.
PRIMARY_SERVICE_ACTION = re.compile(
    r"(?i)\b("
    r"installation\s+services?|"
    r"(?:furniture|booth|equipment|system|fixture)s?\s+installation\b|"
    r"climate[- ]controlled\s+booth\s+installation|"
    r"booth\s+installation|"
    r"engineering\s+and\s+traffic\s+surveys?|"
    r"traffic\s+surveys?|"
    r"speed\s+(?:limit\s+)?surveys?|"
    r"(?:engineering|traffic|topographic|geotechnical|environmental)\s+surveys?|"
    r"survey(?:ing)?\s+services?|"
    r"the\s+selected\s+consultant\s+shall|"
    r"repair\s+of\b|"
    r"repair\s+services?|"
    r"maintenance\s+(?:and\s+)?repair\s+of\b|"
    r"pre[- ]?qual(?:ification)?(?:\s+(?:for|of))?\b|"
    r"pre[- ]?qual(?:ification)?\s+of\s+vendors?|"
    r"vendor\s+pre[- ]?qual(?:ification)?|"
    r"operating\s+(?:and\s+)?(?:maintaining|maintenance)\b|"
    r"consulting\s+(?:services?|engineer)|"
    r"professional\s+engineering\s+services?"
    r")\b"
)

# Affirmative goods supply / delivery — beats department-name false construction hits
FURNISH_AND_DELIVER = re.compile(
    r"(?i)\b("
    r"furnish(?:ing)?\s+and\s+deliver(?:y|ing)?|"
    r"supply(?:ing)?\s+and\s+deliver(?:y|ing)?|"
    r"furnish(?:ing)?\s+of|"
    r"delivery\s+of\s+(?:a\s+|an\s+|the\s+)?(?:handheld\s+)?"
    r"(?:fertilizer|grass\s+seed|salt|analyzer|equipment|materials?|supplies?|items?|"
    r"truck|lift|vehicle|goods)"
    r")\b"
)

SUPPLY_PHRASE = re.compile(
    r"(?i)\b("
    r"maintenance supplies?|janitorial supplies?|custodial supplies?|"
    r"cleaning supplies?|office supplies?|construction materials?|"
    r"water treatment equipment|medical supplies?|safety supplies?|"
    r"related\s+(?:landscaping\s+)?items?"
    r")\b"
)

VENDOR_QUALIFICATION = re.compile(
    r"(?i)\b("
    r"qualified\s+contractors?\s+list|pre[- ]?qualified\s+(?:vendors?|contractors?)|"
    r"pre[- ]?qual(?:ification)?\s+(?:for|of)\b|"
    r"pre[- ]?qual\s+of\s+vendors?|"
    r"pre[- ]?qual(?:ification)?\s+for\b|"
    r"vendor\s+pre[- ]?qual(?:ification)?|"
    r"informal\s+bidding\s+(?:contractors?\s+)?list|"
    r"annual\s+renewal\s+of\s+qualified|"
    r"uniform\s+public\s+construction\s+cost\s+accounting|\bUCCAA\b|"
    r"vendor\s+registration|inclusion\s+on\s+the\s+(?:county'?s\s+)?informal\s+bidding|"
    r"on[- ]call\s+(?:list|roster)\s+of\s+contractors?"
    r")\b"
)

INCIDENTAL_SERVICE = re.compile(
    r"\b(delivery|shipping|freight|warranty|setup|training|"
    r"maintenance included|assembly)\b",
    re.I,
)

SIGNIFICANT_INSTALL = re.compile(
    r"(?i)\b("
    r"including installation|and installation|with installation|"
    r"install(?:ation)? (?:of|and)|"
    r"furnish and install|supply and install|turnkey installation"
    r")\b"
)

RENTAL_LEASE = re.compile(
    r"(?i)\b(rental|lease|leasing|rent(?:al)? of)\b"
)

SOFTWARE_ONLY = re.compile(
    r"(?i)\b("
    r"software license|saas|subscription software|cloud subscription|"
    r"microsoft 365|office 365|software as a service|erp software|"
    r"software only|annual software|software maintenance agreement"
    r")\b"
)

SERVICE_ONLY = re.compile(
    r"(?i)\b("
    r"tree trimming|tree removal|landscap(?:e|ing) services?|lawn (?:care|maintenance)|"
    r"janitorial services?|custodial services?|collection services?|"
    r"professional services?|consulting|staffing|"
    r"legal services?|audit services?|training services?|"
    r"repair services?|maintenance services?|investigation services?|"
    r"installation\s+services?|furniture\s+installation|booth\s+installation|"
    r"equipment maintenance\b|facility maintenance\b|"
    r"therapy|refuse.{0,20}collection|recyclable materials collection|"
    r"personal services?|design services?|engineering services?|"
    r"engineering\s+and\s+traffic\s+surveys?|traffic\s+surveys?|"
    r"survey(?:ing)?\s+services?|"
    r"transport services?|helicopter\s+transport|medical services?|"
    r"fire protection services?|relocation of\b|smoke detector removal|"
    r"cleaning out and plugging|well\s+plugging|"
    r"families?\s+services?|family\s+intervention|partnerships?\s+for\s+families|"
    r"children\s+and\s+family\s+services|social\s+services?|human\s+services?|"
    r"services?\s+request\s+for\s+proposals?|request\s+for\s+proposals?.{0,60}services?"
    r")\b"
)

MAINTENANCE_REPAIR = re.compile(
    r"(?i)\b("
    r"maintenance and repair|repair and maintenance|"
    r"preventive maintenance|corrective maintenance|"
    r"equipment maintenance|vehicle maintenance|hvac maintenance|"
    r"elevator maintenance|generator maintenance|"
    r"repair\s+of\s+(?:fuel|hvac|electrical|mechanical|systems?|equipment)"
    r")\b"
)

CONSTRUCTION_ONLY = re.compile(
    r"(?i)\b("
    r"construction only|general contractor|design[- ]build|"
    r"paving|demolition|excavation|roofing systems?|"
    r"renovate existing structure|building renovation|"
    r"civil engineering construction|roadway construction|"
    r"new construction|tenant improvement construction|"
    r"construction of|construction services?|"
    r"window replacement|garage door replacement|"
    r"water main upgrades?|ada ramps?|"
    # Do NOT match bare "Public Works" department names — that false-labeled rock salt.
    r"public\s+works\s+(?:construction|project|contract|improvement)|"
    r"hvac split system.{0,40}public work|booster station.{0,40}design|"
    r"erosion\s+(?:and\s+sediment\s+)?(?:control|pollution)|"
    r"sediment\s+pollution\s+control|grading\b|site\s+grading|"
    r"rock\s+construction\s+entrance|principal\s+items?\s+of\s+work|"
    r"amr,?\s+n\.?\s*liberty|abandoned\s+mine\s+reclamation"
    r")\b"
)

CATEGORY_SERVICE = re.compile(r"personal services|(?<!product )services", re.I)
CATEGORY_CONSTRUCTION = re.compile(r"construction", re.I)
CATEGORY_COMMODITY = re.compile(r"commodity|goods|supplies|equipment|materials", re.I)

# Purchasing-context signals (not a closed product whitelist).
# CRITICAL: never use greedy `.+` spanning into procurement_type="goods".
PURCHASE_OF_GOODS = re.compile(
    r"(?i)\b("
    r"purchase of|procurement of|acquisition of|furnish(?:ing)?(?:\s+and\s+delivery)?\s+of|"
    r"supply of|"
    r"provide(?:ing)?\s+(?:the\s+)?(?:following\s+)?(?:equipment|materials|supplies|goods|units)\b|"
    r"qty\.?|quantity|unit price|lot of|cases? of|boxes of|pallets? of|"
    r"n\.?i\.?g\.?p\.?|unspsc|commodity code|line item"
    r")\b"
)
LABOR_PRIMARY = re.compile(
    r"(?i)\b("
    r"labor only|hourly (?:rate|services)|staff augmentation|"
    r"contractor shall perform|scope of services|statement of work for services"
    r")\b"
)

# Backward-compatible aliases used by filters / UI
PRODUCT_CLASSES = {
    "product_supply",
    "product_with_incidental_services",
    "product_with_significant_installation",
    "product_rental_leasing",
}
NON_PRODUCT_CLASSES = {
    "services",
    "professional_services",
    "maintenance_repair_services",
    "construction",
    "software_only",
    "vendor_qualification",
    "not_applicable_non_solicitation",
}


def find_meaningful_product_hints(text: str) -> list[re.Match[str]]:
    """Return PRODUCT_HINTS matches that are not legal/facility noise.

    A product-related word appearing in 'Vehicle Code' or a facility name does
    not mean the government is purchasing that item.
    """
    if not text:
        return []
    noise_spans = [(m.start(), m.end()) for m in PRODUCT_HINT_NOISE_CONTEXT.finditer(text)]
    out: list[re.Match[str]] = []
    for m in PRODUCT_HINTS.finditer(text):
        if any(ns <= m.start() < ne or ns < m.end() <= ne for ns, ne in noise_spans):
            continue
        # Hint entirely inside a noise span
        if any(m.start() >= ns and m.end() <= ne for ns, ne in noise_spans):
            continue
        # 'Vehicle' immediately preceded by Commercial/California context already covered;
        # also skip 'vehicle' when the surrounding 40 chars mention code/enforcement/facility
        window = text[max(0, m.start() - 40) : m.end() + 40]
        if re.search(
            r"(?i)(vehicle\s+code|enforcement\s+facility|commercial\s+vehicle\s+enforcement)",
            window,
        ):
            continue
        out.append(m)
    return out


def _affirmative_goods_purchase(title: str, text: str) -> bool:
    """True when language affirms buying/furnishing physical goods (not mere noun presence)."""
    if FURNISH_AND_DELIVER.search(title) or FURNISH_AND_DELIVER.search(text[:1500]):
        return True
    if SUPPLY_PHRASE.search(title):
        return True
    if PURCHASE_OF_GOODS.search(title):
        return True
    # Purchase-of + meaningful product hint in title is affirmative
    if PURCHASE_OF_GOODS.search(text) and find_meaningful_product_hints(title):
        return True
    return False


def classify_requirement(
    title: str,
    *,
    category_raw: str | None = None,
    procurement_type: str | None = None,
    description: str | None = None,
    document_text: str | None = None,
    line_items_text: str | None = None,
) -> dict[str, Any]:
    """Classify purchasing requirement by primary government deliverable.

    Product-related words alone never prove a goods purchase. Determine whether
    the agency is buying a physical item versus installing, repairing, surveying,
    consulting about, or prequalifying vendors for that item.
    """
    parts = [
        title or "",
        category_raw or "",
        procurement_type or "",
        description or "",
        line_items_text or "",
        (document_text or "")[:4000],
    ]
    text = " ".join(parts)
    evidence: list[str] = []
    sources_used = ["title"]
    if category_raw:
        sources_used.append("category")
    if description:
        sources_used.append("description")
    if line_items_text:
        sources_used.append("line_items")
    if document_text:
        sources_used.append("document_text")
    cat = (category_raw or "").strip()
    title_text = title or ""
    goods_affirmative = _affirmative_goods_purchase(title_text, text)

    # Vendor qualification / contractor lists are NOT immediate product purchases
    if VENDOR_QUALIFICATION.search(title_text) or VENDOR_QUALIFICATION.search(text[:2000]):
        m = VENDOR_QUALIFICATION.search(title_text) or VENDOR_QUALIFICATION.search(text[:2000])
        evidence.append(f"vendor_qualification={(m.group(0) if m else '')[:80]}")
        return _out("vendor_qualification", evidence, False, sources_used)

    # Primary ACTION is service/labor — product nouns do not override (unless furnish/purchase)
    psa = PRIMARY_SERVICE_ACTION.search(title_text) or (
        None if goods_affirmative else PRIMARY_SERVICE_ACTION.search(text[:1500])
    )
    if psa and not goods_affirmative:
        evidence.append(f"primary_service_action={(psa.group(0))[:80]}")
        low = psa.group(0).lower()
        if "pre-qual" in low or "prequal" in low or "pre-qual" in title_text.lower():
            return _out("vendor_qualification", evidence, False, sources_used)
        if "repair" in low or MAINTENANCE_REPAIR.search(title_text):
            return _out("maintenance_repair_services", evidence, False, sources_used)
        if "survey" in low or "consultant" in low or "consulting" in low or "engineering" in low:
            return _out("professional_services", evidence, False, sources_used)
        return _out("services", evidence, False, sources_used)

    # Furnish-and-deliver / supply-and-deliver is affirmative product supply
    if FURNISH_AND_DELIVER.search(title_text) or FURNISH_AND_DELIVER.search(
        (description or "")[:1500]
    ):
        m = FURNISH_AND_DELIVER.search(title_text) or FURNISH_AND_DELIVER.search(
            (description or "")[:1500]
        )
        evidence.append(f"furnish_and_deliver={(m.group(0) if m else '')[:80]}")
        if SIGNIFICANT_INSTALL.search(text) and re.search(
            r"(?i)furnish\s+and\s+install|supply\s+and\s+install", text
        ):
            evidence.append("significant_installation_language")
            return _out("product_with_significant_installation", evidence, True, sources_used)
        return _out("product_supply", evidence, True, sources_used)

    # Strong phrase disambiguation first
    if SUPPLY_PHRASE.search(title_text) or SUPPLY_PHRASE.search(text):
        m = SUPPLY_PHRASE.search(title_text) or SUPPLY_PHRASE.search(text)
        evidence.append(f"supply_phrase={(m.group(0) if m else '')[:80]}")
        if SIGNIFICANT_INSTALL.search(text):
            evidence.append("significant_installation_language")
            return _out("product_with_significant_installation", evidence, True, sources_used)
        if INCIDENTAL_SERVICE.search(text):
            evidence.append("incidental_service_language")
            return _out("product_with_incidental_services", evidence, True, sources_used)
        return _out("product_supply", evidence, True, sources_used)

    title_product_hints = find_meaningful_product_hints(title_text)
    # Clear service-program titles before purchase heuristics.
    # Product nouns in the title do NOT block this when the action is service.
    if SERVICE_ONLY.search(title_text) and not goods_affirmative:
        m = SERVICE_ONLY.search(title_text)
        evidence.append(f"service_pattern_title_early={(m.group(0) if m else '')[:80]}")
        if title_product_hints and not PRIMARY_SERVICE_ACTION.search(title_text):
            # Ambiguous: service words + product nouns without clear primary action
            evidence.append(f"product_noun_also_present={(title_product_hints[0].group(0))[:40]}")
        return _out("services", evidence, False, sources_used)

    # Construction work titles (erosion/grading/AMR) before goods heuristics
    if CONSTRUCTION_ONLY.search(title_text) and not goods_affirmative and not title_product_hints:
        if not re.search(r"(?i)construction\s+(materials?|supplies?|equipment|hardware)", title_text):
            m = CONSTRUCTION_ONLY.search(title_text)
            evidence.append(f"construction_pattern_title_early={(m.group(0) if m else title_text)[:80]}")
            return _out("construction", evidence, False, sources_used)

    if LABOR_PRIMARY.search(text) and not title_product_hints and not PURCHASE_OF_GOODS.search(text):
        evidence.append("labor_primary_purchasing_context")
        return _out("services", evidence, False, sources_used)

    if (
        PURCHASE_OF_GOODS.search(text)
        and not SERVICE_ONLY.search(title_text)
        and not CONSTRUCTION_ONLY.search(title_text)
        and not PRIMARY_SERVICE_ACTION.search(title_text)
    ):
        if not MAINTENANCE_REPAIR.search(title_text):
            evidence.append("purchase_of_goods_context")
            if SIGNIFICANT_INSTALL.search(text):
                return _out("product_with_significant_installation", evidence, True, sources_used)
            if INCIDENTAL_SERVICE.search(text) or SERVICE_ONLY.search(text):
                return _out("product_with_incidental_services", evidence, True, sources_used)
            return _out("product_supply", evidence, True, sources_used)

    if SOFTWARE_ONLY.search(text) and not title_product_hints:
        evidence.append("software_subscription_language")
        return _out("software_only", evidence, False, sources_used)

    # Rental vs purchase
    body_hints = find_meaningful_product_hints(text)
    if RENTAL_LEASE.search(title_text) and (title_product_hints or body_hints):
        if re.search(r"(?i)\b(purchase|buy|procurement|furnish|buyout|lease[- ]to[- ]own)\b", title_text):
            evidence.append("purchase_or_lease_to_own_language_not_pure_rental")
        else:
            evidence.append("rental_or_lease_of_goods")
            return _out("product_rental_leasing", evidence, True, sources_used)

    # "Equipment Maintenance" / "Repair of … Equipment" is a service
    if MAINTENANCE_REPAIR.search(title_text) and not SUPPLY_PHRASE.search(title_text):
        evidence.append("maintenance_repair_services_language")
        return _out("maintenance_repair_services", evidence, False, sources_used)

    if cat and CATEGORY_CONSTRUCTION.search(cat) and not CATEGORY_COMMODITY.search(cat):
        if not title_product_hints and not SUPPLY_PHRASE.search(title_text):
            m = CONSTRUCTION_ONLY.search(title_text) or CONSTRUCTION_ONLY.search(text)
            if m:
                evidence.append(f"construction_pattern_with_category={(m.group(0))[:80]};category={cat}")
                return _out("construction", evidence, False, sources_used)
            evidence.append(f"source_category_only_insufficient={cat}")
            return _out("unknown_mixed", evidence, True, sources_used)

    has_product_title = bool(title_product_hints or SUPPLY_PHRASE.search(title_text))
    has_service_title = bool(SERVICE_ONLY.search(title_text) or PRIMARY_SERVICE_ACTION.search(title_text))
    has_construction_title = bool(CONSTRUCTION_ONLY.search(title_text))

    # Affirmative supply phrases beat bare service words; bare product nouns do not
    # override a clear service/installation/repair primary action.
    supply_beats_service = bool(SUPPLY_PHRASE.search(title_text) or goods_affirmative)

    if has_service_title and not supply_beats_service:
        m = SERVICE_ONLY.search(title_text) or PRIMARY_SERVICE_ACTION.search(title_text)
        evidence.append(f"service_pattern_title={(m.group(0) if m else '')[:80]}")
        if "professional" in (m.group(0) if m else "").lower() or "consulting" in title_text.lower():
            return _out("professional_services", evidence, False, sources_used)
        return _out("services", evidence, False, sources_used)

    if has_construction_title and not has_product_title and not supply_beats_service:
        if re.search(r"(?i)construction\s+(materials?|supplies?|equipment|hardware)", title_text):
            evidence.append("construction_materials_or_equipment_goods")
            return _out("product_supply", evidence, True, sources_used)
        m = CONSTRUCTION_ONLY.search(title_text)
        evidence.append(f"construction_pattern_title={(m.group(0) if m else title_text)[:80]}")
        return _out("construction", evidence, False, sources_used)

    if cat and CATEGORY_SERVICE.search(cat) and not CATEGORY_COMMODITY.search(cat) and not has_product_title:
        m = SERVICE_ONLY.search(title_text) or SERVICE_ONLY.search(text)
        if m:
            evidence.append(f"service_pattern_with_category={(m.group(0))[:80]};category={cat}")
            return _out("services", evidence, False, sources_used)
        evidence.append(f"source_category_only_insufficient={cat}")
        return _out("unknown_mixed", evidence, True, sources_used)

    desc_hints = find_meaningful_product_hints(
        f"{category_raw or ''} {procurement_type or ''} {description or ''}"
    )
    has_product = has_product_title or bool(desc_hints)
    has_service_only = bool(SERVICE_ONLY.search(text) or PRIMARY_SERVICE_ACTION.search(text))
    has_construction = bool(CONSTRUCTION_ONLY.search(text))
    has_incidental = bool(INCIDENTAL_SERVICE.search(text))
    has_sig_install = bool(SIGNIFICANT_INSTALL.search(text))

    if has_construction and not has_product_title:
        m = CONSTRUCTION_ONLY.search(text)
        evidence.append(f"construction_pattern={(m.group(0) if m else '')[:80]}")
        return _out("construction", evidence, False, sources_used)
    if has_service_only and not has_product_title and not goods_affirmative:
        m = SERVICE_ONLY.search(text) or PRIMARY_SERVICE_ACTION.search(text)
        evidence.append(f"service_pattern={(m.group(0) if m else '')[:80]}")
        return _out("services", evidence, False, sources_used)

    if has_product and has_service_only and has_construction:
        evidence.append("mixed_product_service_construction_signals")
        return _out("mixed_product_service", evidence, True, sources_used)

    # Product + mandatory install phrase → product with significant installation
    # (incidental delivery/install does not demote a genuine goods purchase)
    if has_product and has_sig_install:
        if re.search(
            r"(?i)\b(furnish\s+and\s+install|supply\s+and\s+install|turnkey\s+installation|"
            r"including\s+installation|with\s+installation)\b",
            text,
        ):
            evidence.append("product_with_significant_installation")
            return _out("product_with_significant_installation", evidence, True, sources_used)
        evidence.append("install_keyword_present_but_not_mandatory_install_phrase")

    if has_product and (has_incidental or has_service_only) and goods_affirmative:
        evidence.append("product_with_incidental_services")
        return _out("product_with_incidental_services", evidence, True, sources_used)

    # Product nouns alone are insufficient — require purchase/supply context OR
    # a specific commodity title without a competing service primary action.
    if has_product and (goods_affirmative or has_product_title):
        if not goods_affirmative and has_product_title and has_service_only:
            m = SERVICE_ONLY.search(text) or PRIMARY_SERVICE_ACTION.search(text)
            evidence.append(f"service_primary_over_bare_product_noun={(m.group(0) if m else '')[:80]}")
            return _out("services", evidence, False, sources_used)
        if cat and CATEGORY_COMMODITY.search(cat) and not has_product_title:
            if desc_hints and goods_affirmative:
                evidence.append(
                    f"product_hint_with_category={(desc_hints[0].group(0))[:80]};category={cat}"
                )
                return _out("product_supply", evidence, True, sources_used)
            evidence.append(f"source_category_only_insufficient={cat}")
            return _out("unknown_mixed", evidence, True, sources_used)
        m = title_product_hints[0] if title_product_hints else (
            SUPPLY_PHRASE.search(title_text) or (desc_hints[0] if desc_hints else None)
        )
        term = m.group(0) if m else title_text
        # Bare weak nouns (equipment/vehicle/furniture/supplies) without purchase
        # language remain unresolved — do not auto-confirm product.
        if (
            not goods_affirmative
            and re.fullmatch(
                r"(?i)equipment|vehicles?|furniture|supplies?|materials?|goods|parts|hardware",
                str(term).strip(),
            )
        ):
            evidence.append(f"bare_product_noun_insufficient={term[:40]}")
            return _out("unknown_mixed", evidence, True, sources_used)
        evidence.append(f"product_supply_terms={str(term)[:80]}")
        return _out("product_supply", evidence, True, sources_used)

    if cat and CATEGORY_COMMODITY.search(cat) and not has_service_only and not has_construction:
        evidence.append(f"source_category_only_insufficient={cat}")
        return _out("unknown_mixed", evidence, True, sources_used)

    if has_service_only:
        evidence.append("service_without_clear_product")
        return _out("services", evidence, False, sources_used)
    if has_construction:
        evidence.append("construction_without_clear_product")
        return _out("construction", evidence, False, sources_used)

    evidence.append("insufficient_evidence")
    return _out("unknown_mixed", evidence, True, sources_used)


def _out(cls: str, evidence: list[str], include: bool, sources_used: list[str]) -> dict[str, Any]:
    return {
        "requirement_class": cls,
        "classification_evidence": evidence,
        "include_in_product_resale": include and cls in PRODUCT_CLASSES | {"unknown_mixed", "mixed_product_service"},
        "evidence_sources": sources_used,
        "classification_stage": "central",
    }


def classify_solicitation_item(item: dict[str, Any]) -> dict[str, Any]:
    """Apply central classification onto a normalized discovery item (mutates copy)."""
    out = dict(item)
    # Build 002G: do not spend classification on portals/forecasts/awards/unrelated
    rt = str(out.get("record_type") or "")
    if rt in {
        "PROCUREMENT_PORTAL",
        "HISTORICAL_AWARD",
        "PROCUREMENT_FORECAST",
        "PURCHASING_PLAN",
        "VENDOR_REGISTRATION",
        "GENERAL_PROCUREMENT_INFORMATION",
        "UNRELATED_CONTENT",
    }:
        out["requirement_class"] = "not_applicable_non_solicitation"
        out["classification_evidence"] = [f"skipped_identity:{rt}"]
        out["include_in_product_resale"] = False
        out["evidence_sources"] = ["identity"]
        out["classification_stage"] = "identity_skip"
        if not out.get("solicitation_status"):
            out["solicitation_status"] = _infer_status(out)
        return out
    desc = out.get("description")
    if not desc and isinstance(out.get("evidence"), dict):
        desc = out["evidence"].get("category_raw")
    clf = classify_requirement(
        str(out.get("title") or ""),
        category_raw=(
            (out.get("evidence") or {}).get("category_raw")
            if isinstance(out.get("evidence"), dict)
            else out.get("product_category")
        ),
        procurement_type=out.get("procurement_type"),
        description=str(desc) if desc else None,
        document_text=out.get("document_text"),
        line_items_text=out.get("line_items_text"),
    )
    out["requirement_class"] = clf["requirement_class"]
    out["classification_evidence"] = clf["classification_evidence"]
    out["include_in_product_resale"] = clf["include_in_product_resale"]
    out["evidence_sources"] = clf.get("evidence_sources")
    out["classification_stage"] = "central"
    # Normalize / re-infer when missing or placeholder "unknown"
    cur = str(out.get("solicitation_status") or "").strip().lower()
    if not cur or cur in {"unknown", "null", "none"}:
        out["solicitation_status"] = _infer_status(out)
    return out


def _infer_status(item: dict[str, Any]) -> str:
    if item.get("is_forecast") or (item.get("procurement_type") or "").lower() == "forecast":
        return "forecast"
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    # Provider lifecycle_status (NextBid) is authoritative when present
    life = str(
        item.get("lifecycle_status")
        or evidence.get("lifecycle_status")
        or ""
    ).lower()
    if life in {"amended", "amendment"}:
        return "amended"
    if life in {"open", "active", "posted", "advertised"}:
        return "open"
    if life in {"closed", "expired", "inactive"}:
        return "closed"
    if life in {"cancelled", "canceled"}:
        return "cancelled"
    if life in {"awarded"}:
        return "historical_award"

    st = str(item.get("solicitation_status") or item.get("status") or "")
    st = st or str(evidence.get("status") or "")
    if re.search(r"(?i)\b(cancel)", st):
        return "cancelled"
    if re.search(r"(?i)\b(award)", st):
        return "historical_award"
    if re.search(r"(?i)\b(closed|expired|inactive)", st):
        return "closed"
    if re.search(r"(?i)\b(amend)", st):
        return "amended"
    if re.search(r"(?i)\b(open|active|posted|advertised)", st):
        return "open"

    # Retained open=true search membership (lifecycle often null in provider payload)
    open_filter = bool(
        item.get("provider_open_filter")
        or evidence.get("provider_open_filter")
        or "provider_open_filter_null_lifecycle" in (item.get("status_evidence") or [])
    )

    # Past deadline heuristic — does not invent; marks likely closed.
    dl = item.get("deadline_at") or item.get("response_deadline")
    dl_past = False
    dl_future = False
    if dl:
        try:
            raw = str(dl).replace("Z", "+00:00")
            from datetime import datetime, timezone

            dt = datetime.fromisoformat(raw[:19])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt.year >= 2000:
                if dt < datetime.now(timezone.utc):
                    dl_past = True
                else:
                    dl_future = True
        except Exception:
            pass
    if dl_past and not open_filter:
        return "closed_by_deadline"
    if open_filter and not dl_past:
        return "open"
    if open_filter and dl_past:
        # Was open when captured; deadline has since passed
        return "closed_by_deadline"
    # Future deadline supports active only with open-filter or other affirmative evidence —
    # alone it remains unknown (do not fabricate).
    if not st and not life and not dl:
        return "unknown"
    if dl_future:
        return "unknown"
    return "unknown"


def classify_batch(items: list[dict[str, Any]]) -> dict[str, Any]:
    classified = [classify_solicitation_item(i) for i in items]
    breakdown: dict[str, int] = {}
    for i in classified:
        k = i.get("requirement_class") or "unknown_mixed"
        breakdown[k] = breakdown.get(k, 0) + 1
    status_breakdown: dict[str, int] = {}
    for i in classified:
        k = i.get("solicitation_status") or "unknown"
        status_breakdown[k] = status_breakdown.get(k, 0) + 1
    return {
        "items": classified,
        "classification_breakdown": breakdown,
        "status_breakdown": status_breakdown,
    }
