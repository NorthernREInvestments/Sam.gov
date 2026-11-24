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


# ---------------------------------------------------------------------------
# Evidence-accumulation classifier
#
# Decides the PRIMARY deliverable from independent evidence sources, each of
# which contributes a weighted vote backed by a verbatim span:
#   title     — the head phrase of the title (what the noun phrase is about)
#   summary   — purpose sentences ("requesting proposals ... to provide X"),
#               decisive phrases; negated phrases are ignored
#   codes     — agency/portal-assigned NIGP / UNSPSC / NAICS / PSC codes, read
#               through each taxonomy's published goods/services split
#   provider  — aggregator procurement_kind (weak: may itself be inferred)
# A category is decided only with a clear margin; conflicts stay unresolved.
# ---------------------------------------------------------------------------

CATEGORIES = ("product", "service", "construction", "non_purchase", "vendor_qualification")

DECISION_MIN_SCORE = 2.5
DECISION_MIN_MARGIN = 1.5

# Spans that contain cue words but name an organisation, facility, or legal code.
_NAME_MASK = re.compile(
    r"(?i)("
    r"\b(?:department|dept\.?|division|office|bureau|agency|board)\s+of\s+"
    r"(?:[\w&,'\-]+\s+){0,6}?services\b|"
    r"\b(?:student|social|human|health(?:\s+and\s+human)?|developmental|family|children(?:\s+and\s+family)?|"
    r"community|general|administrative|county|public|emergency|veterans?|youth|senior|aging|disability|"
    r"financial|technical|information|employment|protective|correctional|support|guest|visitor|"
    r"environmental|fleet|facilities|purchasing|procurement|business|court|library|parks?|"
    r"recreation|water|utility|utilities)\s+services"
    r"(?=\s+(?:agency|department|dept|division|office|building|bldg|center|centre|complex|facility|"
    r"renovations?|wing|hall|director|administration)\b)|"
    r"\b(?:department|dept\.?)\s+of\s+public\s+works\b|\bpublic\s+works\s+(?:department|dept\.?|division|director)\b|"
    r"california\s+vehicle\s+code|\bvehicle\s+code\b|commercial\s+vehicle\s+enforcement(?:\s+facility)?|"
    r"vehicle\s+enforcement\s+facility|\bCVEF\b|department\s+of\s+motor\s+vehicles|\bDMV\b|"
    r"\belectrical\s+service\b(?=\s+(?:upgrade|entrance|panel|replacement|improvements?))|"
    r"\b(?:pump|lift|booster|fire|police|transfer)\s+stations?\b|"
    r"\b(?:water|wastewater|sewage|treatment)\s+(?:reclamation\s+|treatment\s+|recycling\s+)?(?:plant|facility)\b|"
    r"\bsmall\s+business\b|\bservice[- ]disabled\b"
    r")"
)

_NEGATION_AFTER = re.compile(
    r"(?i)^[^.;]{0,60}?\b(?:(?:are|is|will|shall|may)\s+not\b|not\s+(?:permitted|allowed|included|part|"
    r"eligible|accepted|required|covered)\b|prohibited\b|excluded\b)"
)
_NEGATION_BEFORE = re.compile(
    r"(?i)\b(?:no|not|excluding|except|other\s+than|nor)\s+(?:[\w\-]+\s+){0,3}$"
)

# (pattern, category, subtype, weight). Title head detection uses the LAST match
# in the main clause; "ACTION of ..." prefixes take precedence (see _title_signals).
_SERVICE_TERMS = (
    r"services?|maintenance|maintain(?:ing)?|repairs?|servicing|inspections?|testing|calibration|"
    r"cleaning|cleanout|clean[- ]?up|consult(?:ant|ants|ing)|advis(?:or|ors|ory)|audit(?:s|ing|or)?|"
    r"appraisals?|legal|attorneys?|counsel|staffing|nursing|catering|janitorial|custodial|mowing|"
    r"towing|hauling|removal|disposal|collection|recycling|shuttle|charter\s+bus(?:es)?|courier|"
    r"printing|mailing|translation|interpret(?:ation|ing|ers?)|training|coaching|administration|"
    r"administrator|management|support|implementation|integration|design(?![- ]build)|engineering|planning|"
    r"plan\s+(?:update|development|preparation)|studies|study|analysis|assessments?|evaluations?|"
    r"surveys?|surveying|monitoring|investigations?|security\s+guards?|guard\s+services|patrol|"
    r"pest\s+control|extermination|laundry|food\s+service|clinic|wellness|therapy|counseling|"
    r"defense|representation|hearing\s+officer|auctioneer|broker|marketing|campaign|advertising|"
    r"outreach|coordinator|writing|facilitation|mediation|operators?|operations|call\s+center|"
    r"hosting|subscription|licens(?:e|es|ing)|saas|software|programming|transcription|"
    r"court\s+reporting|landscap(?:e|ing)\s+maintenance|car\s+wash|transport(?:ation)?\s+services|"
    r"rental\s+with\s+operator|on[- ]call|as[- ]needed|program(?:s)?|scaffolding|labor|"
    r"installation|install"
)
_CONSTRUCTION_TERMS = (
    r"construction\s+services?|design[- ]build|construction|reconstruction|improvements?|rehabilitation|rehab|"
    r"renovations?|remodel(?:ing)?|painting|roofing|re-?roofing|flooring\s+replacement|"
    r"modernization|retrofit|expansion|demolition|paving|repaving|resurfacing|overlay|striping|"
    r"sidewalks?|curbs?|culverts?|bridges?|roadway|pipeline|water\s+mains?|sewer|reservoir|"
    r"job\s+order\s+contracts?|\bJOC\b|capital\s+improvement|\bCIP\b|projects?|upgrades?|"
    r"replacements?|phase\s+[\w\d]+|site\s+work|earthwork|grading|"
    r"erosion|sediment|seawall|levee|dam|trail|park\s+improvements?|restroom\s+building|abatement|remediation"
)
_GOODS_TERMS = (
    r"parts|supplies|supply|equipment|equip|materials?|commodit(?:y|ies)|vehicles?|trucks?|cars?|sedans?|suvs?|"
    r"(?:office|janitorial|custodial|cleaning|medical|safety|school|art|maintenance|industrial)\s+suppl(?:y|ies)|"
    r"purchase\s+(?:contract|agreement|order)s?|"
    r"vans?|buses|trailers?|furniture|furnishings|fixtures?|uniforms|clothing|apparel|footwear|boots|"
    r"gloves|ppe|food|foods|milk|dairy|bread|produce|meats?|groceries|beverages|fuels?|gasoline|"
    r"propane|oils?|lubricants?|chemicals?|chlorine|hypochlorite|bisulfite|salt|sand|gravel|aggregates?|"
    r"asphalt\s+mix|ready[- ]mix|lumber|pipes?|fittings?|conductors?|wires?|cables?|poles?|luminaires?|"
    r"lamps?|bulbs?|computers?|laptops?|tablets?|servers?|printers?|copiers?|toner|hardware|monitors?|"
    r"phones?|radios?|cameras?|batteries|tires?|generators?|pumps?|motors?|valves?|transformers?|"
    r"meters?|sensors?|analy[sz]ers?|spectrometers?|instruments?|devices?|machines?|machinery|"
    r"appliances?|mowers?|tractors?|forklifts?|lifts?|ambulances?|apparatus|engines?|books?|"
    r"textbooks?|drugs?|pharmaceuticals?|medications?|vaccines?|paper|envelopes|modules?|kits?|"
    r"components?|accessories|tools?|ammunition|firearms|body\s+armor|seed|fertilizer|mulch|"
    r"couplers?|isolators?|bags?|containers?|carts?|dumpsters?|bins?|mattresses?|linens?|"
    r"towels?|signs?|decals?|badges?|ice|steel|concrete|rock\s+salt|filters?|hoses?|helmets?|"
    r"bodies|plows?|spreaders?|sanders?|bleachers?|playground\s+equipment|lockers?|"
    r"shelving|carpet|flooring\s+materials|paint|products?|steel|systems?"
)
_NON_PURCHASE_TERMS = (
    r"grant\s+(?:program|opportunity|award|application|round)s?|funding\s+(?:opportunity|availability)|"
    r"incentive\s+(?:program|project)s?|rebate\s+program|grants?|funding|nofa|nofo|incentive|rebate|scholarships?|sale|auction|surplus|for\s+sale|"
    r"meeting|agenda|minutes|public\s+hearing|reminder|newsletter|award\s+notice|notice\s+of\s+award|"
    r"intent\s+to\s+award|lease\s+of\s+(?:facility\s+)?space|lease\s+of\s+(?:city|county|state|town)?\s*"
    r"(?:property|land|space)|agricultural\s+lease|property\s+lease|ground\s+lease|"
    r"purchase\s+(?:city|county|town|state|district)\s+property|real\s+estate|affordable\s+housing|"
    r"supportive\s+housing|housing\s+development"
)

TITLE_CUE = re.compile(
    rf"(?i)\b(?:(?P<vq>{VENDOR_QUALIFICATION.pattern[7:-3]}|small\s+works\s+roster|bidders?'?s?\s+list|"
    rf"registration\s+notice|qualified\s+vendors?\s+list|pre[- ]?qualified)|"
    rf"(?P<np>{_NON_PURCHASE_TERMS})|"
    rf"(?P<svc>{_SERVICE_TERMS})|"
    rf"(?P<con>{_CONSTRUCTION_TERMS})|"
    rf"(?P<goods>{_GOODS_TERMS}))\b"
)

_VQ_ANYWHERE = re.compile(
    rf"(?i)\b(?:{VENDOR_QUALIFICATION.pattern[7:-3]}|pre[- ]?qual(?:ification|ified)?\b|"
    r"small\s+works\s+roster|bidders?'?s?\s+list|qualified\s+vendors?\s+list|"
    r"(?:vendor|contractor)\s+registration\s+notice|annual\s+[\w\s]{0,30}registration\s+notice)"
)

_ACTION_OF = re.compile(
    r"(?i)\b(?P<act>purchase|procurement|acquisition|supply|furnish(?:ing)?|delivery|"
    r"furnish(?:ing)?\s+and\s+deliver(?:y|ing)?|supply(?:ing)?\s+and\s+deliver(?:y|ing)?|"
    r"purchase\s+and\s+install(?:ation)?|furnish(?:ing)?\s+and\s+install(?:ation|ing)?|"
    r"supply\s+and\s+install(?:ation)?|replacement|repairs?|rehabilitation|renovation|installation|"
    r"construction|reconstruction|maintenance|inspection|cleaning|removal|relocation|demolition|"
    r"design|abatement|restoration|resurfacing|painting|paving|upgrades?|improvements?|"
    r"modernization|retrofit|expansion|operation|management|administration|disposal|hauling|"
    r"collection|testing|calibration|servicing|monitoring|assessment|evaluation|analysis|study|"
    r"preparation|development|implementation|rental|lease|leasing|sale)\s+(?:and\s+\w+\s+)?of\b"
)
_IMPERATIVE_START = re.compile(
    r"(?i)^(?P<verb>replace|repair|install|construct|remove|maintain|clean|inspect|paint|pave|"
    r"rehabilitate|renovate|upgrade|demolish|furnish|supply|purchase|provide|deliver|procure)\b"
)
_RELATED_SERVICES = re.compile(
    r"(?i)\b(?:and|&|with|plus)\s+(?:related\s+|associated\s+|incidental\s+)?(?:products\s+and\s+)?"
    r"(?:services?|service|support|maintenance|installation|repairs?)\b"
)
_GOODS_OBJECT_NONPURCHASE = re.compile(
    r"(?i)\b(?:property|properties|land|real\s+estate|housing|building\s+site|parcels?)\b"
)
_TRAILING_CLAUSE = re.compile(
    r"(?i)\s+(?:for\s+(?:the\s+)?|at\s+|in\s+|throughout\s+|along\s+|located\s+|within\s+|on\s+behalf\s+of\s+)"
)

_SUBTYPE_MAINT = re.compile(r"(?i)\b(?:maint\w*|repairs?|servicing|calibrat\w*)\b")
_SUBTYPE_PROF = re.compile(
    r"(?i)\b(?:consult\w*|advis\w*|audit\w*|apprais\w*|legal|attorneys?|counsel|design|engineering|"
    r"planning|plan|stud(?:y|ies)|analysis|assessments?|evaluations?|surveys?|management|"
    r"administrat\w*|architect\w*|inspect\w*|estimating)\b"
)
_SUBTYPE_SOFTWARE = re.compile(r"(?i)software|subscription|licens|saas|hosting")
_RENTAL_GOODS = re.compile(r"(?i)\b(?:rental|rented|rent|lease|leased|leasing)\b")


def _clean_text(text: str | None) -> str:
    import html

    t = html.unescape(str(text or ""))
    t = t.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", t).strip()


# Capitalised organisation names ("Department of Corrections and Rehabilitation")
_PROPER_ORG_MASK = re.compile(
    r"\b(?:Department|Dept\.?|Division|Bureau|Agency|Board|Office|Commission|Authority|Council)\s+of\s+"
    r"(?:the\s+)?(?:[A-Z][\w'\-]*(?:\s+|$)(?:(?:and|&|of|for)\s+)?){1,6}"
)
_PRICING_MASK = re.compile(r"(?i)\btime[- ]and[- ]materials?\b|\bT\s*&\s*M\b")


def _mask_names(text: str) -> str:
    blank = lambda m: " " * len(m.group(0))  # noqa: E731
    return _PRICING_MASK.sub(blank, _PROPER_ORG_MASK.sub(blank, _NAME_MASK.sub(blank, text)))


def _negated(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 60) : start]
    before = re.split(r"[.;!?]\s", before)[-1]
    after = text[end : end + 80]
    return bool(_NEGATION_BEFORE.search(before) or _NEGATION_AFTER.search(after))


def _cue_category(m: re.Match[str]) -> str:
    if m.group("vq"):
        return "vendor_qualification"
    if m.group("np"):
        return "non_purchase"
    if m.group("svc"):
        return "service"
    if m.group("con"):
        return "construction"
    return "product"


# Cues that are decisive as a title head vs weaker contextual heads
_WEAK_HEAD = re.compile(
    r"(?i)^(?:projects?|phase\s+[\w\d]+|program(?:s)?|on[- ]call|as[- ]needed|support|operations|"
    r"supply|concrete|ice|paint|signs?|trail|dam|sewer|reservoir|pipeline|products?|"
    r"steel|bags?|components?|devices?|systems?)$"
)


def _analyze_phrase(phrase: str, *, source: str) -> dict[str, Any] | None:
    """Find the deliverable head of a title or captured object phrase."""
    raw = _clean_text(phrase)
    if not raw:
        return None
    text = _mask_names(raw)

    # Prequalification / bidder lists: not an immediate purchase, wherever the phrase sits
    m_vq = _VQ_ANYWHERE.search(text)
    if m_vq and not _negated(text, m_vq.start(), m_vq.end()):
        return {
            "category": "vendor_qualification",
            "subtype": "vendor_qualification",
            "strength": "strong",
            "quote": raw[max(0, m_vq.start() - 20) : m_vq.end() + 80].strip(),
            "rule": f"{source}.vendor_qualification",
        }

    # Purchase-and-install / furnish-and-install → product with significant installation
    m_inst = re.search(
        r"(?i)\b(?:furnish(?:ing)?|supply|supplying|purchase|procure)\s+(?:and|&)\s+install(?:ation|ing)?\b",
        text,
    )
    if m_inst and not _negated(text, m_inst.start(), m_inst.end()):
        return {
            "category": "product",
            "subtype": "significant_installation",
            "strength": "strong",
            "quote": raw[max(0, m_inst.start() - 20) : m_inst.end() + 80].strip(),
            "rule": f"{source}.furnish_and_install",
        }

    # "ACTION of OBJECT" — the action is the deliverable
    m_act = _ACTION_OF.search(text)
    if m_act and m_act.start() <= 40 and not _negated(text, m_act.start(), m_act.end()):
        act = m_act.group("act").lower()
        obj = text[m_act.end() : m_act.end() + 120]
        if act.startswith(("purchase", "procurement", "acquisition", "supply", "furnish", "delivery")):
            if _GOODS_OBJECT_NONPURCHASE.search(obj[:60]):
                cat, sub = "non_purchase", "real_property"
            else:
                inner = _analyze_phrase(obj, source=source + ".object") if obj.strip() else None
                if inner and inner["category"] in {"service", "construction"} and inner["strength"] == "strong":
                    return {**inner, "rule": f"{source}.{act}_of_{inner['category']}"}
                cat, sub = "product", "supply"
        elif act == "sale":
            cat, sub = "non_purchase", "government_sale"
        elif act in {"rental", "lease", "leasing"}:
            if _GOODS_OBJECT_NONPURCHASE.search(obj[:60]) or re.search(r"(?i)\bspace\b", obj[:40]):
                cat, sub = "non_purchase", "real_property"
            else:
                cat, sub = "product", "rental"
        elif act in {
            "construction", "reconstruction", "rehabilitation", "renovation", "demolition",
            "resurfacing", "paving", "restoration", "expansion", "modernization", "retrofit",
            "improvements", "improvement", "upgrade", "upgrades", "replacement",
        }:
            cat, sub = "construction", act
        else:
            cat, sub = "service", act
        return {
            "category": cat,
            "subtype": sub,
            "strength": "strong",
            "quote": raw[m_act.start() : m_act.end() + 80].strip(),
            "rule": f"{source}.action_of:{act}",
        }

    m_imp = _IMPERATIVE_START.search(text)
    if m_imp:
        verb = m_imp.group("verb").lower()
        if verb in {"furnish", "supply", "purchase", "deliver", "procure"}:
            cat = "product"
        elif verb in {"construct", "pave", "rehabilitate", "renovate", "upgrade", "demolish", "replace"}:
            cat = "construction"
        elif verb == "provide":
            inner = _analyze_phrase(text[m_imp.end() :], source=source + ".object")
            return inner
        else:
            cat = "service"
        return {
            "category": cat,
            "subtype": verb,
            "strength": "strong",
            "quote": raw[:120],
            "rule": f"{source}.imperative:{verb}",
        }

    # Main clause: cut trailing "for/at/in ..." only if the head clause has a cue
    segments = [s for s in re.split(r"\s+[-–—|:]\s+|\s*\(\s*|\s*\)\s*", text) if s.strip()]
    best: dict[str, Any] | None = None
    for seg in segments:
        cut = _TRAILING_CLAUSE.split(seg, maxsplit=1)
        main = cut[0] if TITLE_CUE.search(cut[0]) else seg
        cues = [m for m in TITLE_CUE.finditer(main) if not _negated(main, m.start(), m.end())]
        if not cues:
            continue
        # A cue directly followed by another cue only modifies it ("Equipment Maintenance",
        # "Printing Materials"): the following word is the head of that compound.
        cues = [
            m for i, m in enumerate(cues)
            if i + 1 >= len(cues)
            or _WEAK_HEAD.match(cues[i + 1].group(0).strip())
            or not re.fullmatch(r"[\s\-]*", main[m.end() : cues[i + 1].start()])
        ]
        strong_cues = [m for m in cues if not _WEAK_HEAD.match(m.group(0).strip())]
        head = strong_cues[-1] if strong_cues else cues[-1]
        cat = _cue_category(head)
        head_text = head.group(0)
        strength = "strong" if strong_cues else "weak"
        # "Parts and Service" / "Equipment with Related Services" names both deliverables;
        # "Pump and Well Inspection" (other words between) is a service of a goods object.
        joint = False
        goods_cues = [m for m in strong_cues if _cue_category(m) == "product"]
        for a, b in zip(strong_cues, strong_cues[1:]):
            pair = {_cue_category(a), _cue_category(b)}
            if pair == {"product", "service"} and re.fullmatch(
                r"(?i)(?:\s|,|&|/|\+|\band\b|\bwith\b|\bplus\b|\brelated\b|\bassociated\b)+",
                main[a.end() : b.start()],
            ):
                joint = True
        if goods_cues and _RELATED_SERVICES.search(main[goods_cues[-1].end() :]):
            joint = True
        if joint:
            cat_out, sub = "mixed", "goods_and_services"
        else:
            cat_out, sub = cat, head_text.lower()
        if cat_out == "product" and _RENTAL_GOODS.search(main[: head.start()]):
            sub = "rental"
        cand = {
            "category": cat_out,
            "subtype": sub,
            "strength": strength,
            "quote": seg.strip()[:200] if len(seg) < 200 else main[:200],
            "rule": f"{source}.head:{head_text.lower()[:40]}",
        }
        if best is None or (best["strength"] == "weak" and strength == "strong"):
            best = cand
        if strength == "strong":
            break
    return best


_PURPOSE_PATTERNS = (
    re.compile(
        r"(?i)\b(?:seek(?:s|ing)?|request(?:s|ing)?|solicit(?:s|ing)?|invit(?:es?|ing)|accept(?:s|ing)|"
        r"issu(?:es|ing)|releas(?:es|ed|ing)|is\s+requesting)\b[^.;:]{0,140}?\b(?:bids?|proposals?|quotes?|"
        r"quotations?|qualifications|statements?\s+of\s+qualifications|soqs?|responses?|offers?)\b"
        r"[^.;:]{0,80}?\b(?P<verb>for(?:\s+the)?|to\s+(?:provide|furnish|supply|perform|deliver|purchase|"
        r"procure|conduct|complete|design|construct|install|repair|replace|remove|assist|staff|"
        r"prepare|develop|establish|operate))\s+(?P<obj>[^.;]{4,220})"
    ),
    re.compile(r"(?i)\bpurpose\s+of\s+(?:this|the)\s+[^.]{0,80}?\bis\s+to\s+(?P<obj>[^.;]{4,240})"),
    re.compile(
        r"(?i)\b(?:the\s+)?(?:project|work|scope(?:\s+of\s+work)?)\s+(?:consists?\s+of|includes?|involves?|"
        r"will\s+include|generally\s+consists\s+of)\b[^.;:]{0,20}?\s+(?P<obj>[^.;]{4,220})"
    ),
    re.compile(
        r"(?i)\b(?:contractor|vendor|consultant|firm)\s+(?:shall|will)\s+(?P<verb>provide|furnish|supply|"
        r"perform|deliver|install|construct|repair)\s+(?P<obj>[^.;]{4,200})"
    ),
)

_SUMMARY_DECISIVE = (
    (FURNISH_AND_DELIVER, "product", 2.0, "furnish_and_deliver"),
    (re.compile(r"(?i)\b(?:principal\s+items?\s+of\s+work|construction\s+of|general\s+contractor|"
                r"contractor'?s\s+license|class\s+[A-C]\d*\s+license|"
                r"erosion\s+(?:and\s+sediment\s+)?(?:control|pollution))\b"),
     "construction", 1.5, "construction_contract_language"),
    (re.compile(r"(?i)\b(?:scope\s+of\s+services|professional\s+services|consulting\s+(?:firms?|services)|"
                r"qualified\s+(?:firms?|consultants?)|statements?\s+of\s+qualifications|"
                r"hourly\s+rates?|staff(?:ing)?\s+augmentation)\b"),
     "service", 1.5, "service_contract_language"),
    (re.compile(r"(?i)\b(?:price\s+per\s+(?:unit|each|case|ton|gallon|pound)|"
                r"(?:brand|make)\s+and\s+model|or\s+(?:approved\s+)?equal|"
                r"f\.?o\.?b\.?\s+destination|nsn\s+\d|qty\s+\d+\s+(?:ui\s+)?(?:ea|each)\b)"),
     "product", 1.5, "goods_pricing_or_delivery_language"),
    (re.compile(r"(?i)\b(?:grant\s+(?:program|funding|opportunity|application)|funding\s+(?:availability|"
                r"opportunity)|eligible\s+(?:applicants|entities)|notice\s+of\s+funding|"
                r"purchase\s+(?:city|county|town)\s+property|(?:will|to)\s+be\s+sold|public\s+auction|"
                r"notice\s+is\s+hereby\s+given\s+that\s+the\s+[\w\s]{0,60}(?:will\s+(?:hold|meet)|meetings?))\b"),
     "non_purchase", 2.0, "non_purchase_language"),
)

# "furnish all labor, tools, equipment ... required to provide X": contract boilerplate, not goods
_LABOR_BOILERPLATE = re.compile(
    r"(?i)\b(?:furnish|provide|supply)(?:ing)?\s+all\s+(?:necessary\s+|required\s+)?labor\b[^.;]{0,140}?"
    r"(?:\b(?:required|necessary|needed)\s+(?=to\b|for\b)|(?=\bfor\b)|"
    r"(?=\bto\s+(?:install|perform|complete|construct|provide|repair|replace|remove)\b))"
)
# Purpose objects end at relative/qualifying clauses
_OBJECT_TAIL = re.compile(
    r"(?i)\s+(?:(?:necessary|required|needed)\s+(?:to|for)|in\s+order\s+to|"
    r"(?:associated|in\s+connection|in\s+accordance)\s+with|during|through(?:out)?|which|that|who|"
    r"as\s+(?:specified|described|outlined|needed|required)|for\s+(?:use|the\s+period|a\s+period|the\s+term)|"
    r"on\s+an?\s+as[- ]needed)\b"
)


_WORK_VERBS = {
    "perform": "service", "conduct": "service", "install": "service", "repair": "service",
    "remove": "service", "assist": "service", "staff": "service", "operate": "service",
    "prepare": "service", "develop": "service", "design": "service",
    "construct": "construction", "replace": "construction", "complete": "construction",
}


def _lead_phrase(body: str, title_text: str) -> str | None:
    """Opening clause of the summary when it is not just an echo of the title."""
    if not body:
        return None
    first = re.split(r"(?<=[.;:!?])\s+", body, maxsplit=1)[0][:200]
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()  # noqa: E731
    nf, nt = norm(first), norm(title_text)
    if not nf or (nt and (nf.startswith(nt[:40]) or nt.startswith(nf[:40]))):
        return None
    # Only noun-phrase style leads; sentences about the agency are handled by purpose patterns
    if re.search(r"(?i)\b(?:is|are|was|were|has|have|will|shall)\b", first):
        return None
    return first


def _code_side(system: str, code: str, label: str) -> str | None:
    s = (system or "").lower()
    c = re.sub(r"\D", "", str(code or ""))
    lab = str(label or "")
    if s == "psc":
        raw = str(code or "").strip().upper()
        if not raw:
            return None
        if raw[0].isdigit():
            return "product"
        return "construction" if raw[0] in {"Y", "Z"} else "service"
    if s == "nigp" and len(c) >= 3:
        cls = int(c[:3])
        if cls in {909, 912, 913, 914}:
            return "construction"
        if cls >= 905:
            return "service"
        return "product" if cls >= 5 else None
    if s == "unspsc" and len(c) >= 2:
        seg = int(c[:2])
        if seg == 72:
            return "construction"
        if seg == 95:
            return "non_purchase"
        if 70 <= seg <= 94:
            return "service"
        if 10 <= seg <= 60:
            return "product"
        return None
    if s == "naics" and len(c) >= 2:
        sec = int(c[:2])
        if sec == 23:
            return "construction"
        if sec in {31, 32, 33, 42, 44, 45}:
            return "product"
        if sec in {22, 48, 49, 51, 52, 53, 54, 55, 56, 61, 62, 71, 72, 81}:
            return "service"
        return None
    # Portal categories: read the label
    if re.search(r"(?i)\b(construction|contractors?|paving|roofing|demolition)\b", lab):
        return "construction"
    if re.search(r"(?i)\b(services?|consult|repair|maintenance|rental|leasing|testing|training)\b", lab):
        return "service"
    if re.search(r"(?i)\b(manufactur|wholesal|merchant|supplies|equipment|parts|products|materials|stores)\b", lab):
        return "product"
    return None


def _codes_signal(agency_codes: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not agency_codes:
        return None
    sides: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for sc in agency_codes:
        if not isinstance(sc, dict):
            continue
        key = (str(sc.get("system") or ""), str(sc.get("code") or ""))
        if key in seen:
            continue
        seen.add(key)
        side = _code_side(key[0], key[1], str(sc.get("label") or ""))
        if side:
            sides.setdefault(side, []).append(f"{key[0]}:{key[1]} {sc.get('label') or ''}".strip())
    if not sides:
        return None
    total = sum(len(v) for v in sides.values())
    top = max(sides, key=lambda k: len(sides[k]))
    share = len(sides[top]) / total
    if share < 0.7:
        return {"category": None, "split": {k: len(v) for k, v in sides.items()}, "quote": "; ".join(
            x for v in sides.values() for x in v[:2])[:240]}
    weight = 1.5 if len(sides[top]) >= 2 else 1.0
    return {"category": top, "weight": weight, "quote": "; ".join(sides[top][:3])[:240],
            "split": {k: len(v) for k, v in sides.items()}}


def classify_requirement(
    title: str,
    *,
    category_raw: str | None = None,
    procurement_type: str | None = None,
    description: str | None = None,
    document_text: str | None = None,
    line_items_text: str | None = None,
    agency_codes: list[dict[str, Any]] | None = None,
    procurement_kind: str | None = None,
) -> dict[str, Any]:
    """Classify the government's primary deliverable from weighed, quoted evidence.

    Product words alone never prove a goods purchase; the head of the title, the
    stated purpose, agency commodity codes and provider kind are weighed together.
    Without a clear margin the record stays ``unknown_mixed`` (unresolved).
    """
    title_text = _clean_text(title)
    desc = _clean_text(description)
    doc = _clean_text((document_text or "")[:6000])
    items = _clean_text(line_items_text)
    sources_used = ["title"]
    for name, val in (("category", category_raw), ("description", desc), ("line_items", items),
                      ("document_text", doc), ("agency_codes", agency_codes), ("procurement_kind", procurement_kind)):
        if val:
            sources_used.append(name)

    signals: list[dict[str, Any]] = []

    def add(source: str, category: str, weight: float, rule: str, quote: str, subtype: str | None = None) -> None:
        signals.append({"source": source, "category": category, "weight": round(weight, 2),
                        "rule": rule, "quote": (quote or "").strip()[:240], "subtype": subtype})

    # --- Title head ---
    th = _analyze_phrase(title_text, source="title")
    if th:
        w = 3.0 if th["strength"] == "strong" else 1.5
        if th["category"] == "mixed":
            add("title", "product", 2.5, th["rule"], th["quote"], "goods_and_services")
            add("title", "service", 2.5, th["rule"], th["quote"], "goods_and_services")
        else:
            add("title", th["category"], w, th["rule"], th["quote"], th["subtype"])
    # Category label on the record (portal "category" text) — weak
    if category_raw and not re.fullmatch(r"(?i)\s*(goods|services?|mixed|unknown|none)\s*", category_raw):
        ch = _analyze_phrase(category_raw, source="category")
        if ch and ch["category"] in CATEGORIES:
            add("category", ch["category"], 0.75, ch["rule"], ch["quote"], ch["subtype"])

    # --- Summary / document: purpose sentences then decisive phrases ---
    body = " ".join(x for x in (desc, items, doc) if x)
    # Same-length substitution keeps offsets aligned with `body` for quoting
    body_masked = _LABOR_BOILERPLATE.sub(lambda m: " " * len(m.group(0)), _mask_names(body))

    lead = _lead_phrase(body_masked, title_text)
    if lead:
        la = _analyze_phrase(lead, source="summary.lead")
        if la and la["category"] in CATEGORIES:
            add("summary", la["category"], 1.5 if la["strength"] == "strong" else 0.75,
                la["rule"], body[: len(lead)], la["subtype"])

    purpose_hits = 0
    for pat in _PURPOSE_PATTERNS:
        for m in pat.finditer(body_masked):
            if purpose_hits >= 2:
                break
            if _negated(body_masked, m.start(), m.end()):
                continue
            obj = _OBJECT_TAIL.split(m.group("obj"), maxsplit=1)[0]
            truncated = m.end() >= len(body_masked.rstrip())
            if truncated:
                # Retained summaries are often cut mid-word; never read a fragment as a noun
                obj = obj.rsplit(" ", 1)[0] if " " in obj.strip() else ""
                if not obj.strip():
                    continue
            verb = (m.groupdict().get("verb") or "").lower()
            verb_word = verb.split()[-1] if verb else ""
            if verb_word in _WORK_VERBS:
                # "shall install all roofing components": the work is the deliverable
                an = {"category": _WORK_VERBS[verb_word], "subtype": verb_word, "strength": "strong",
                      "rule": f"summary.purpose_work_verb:{verb_word}", "quote": obj[:160]}
            else:
                an = _analyze_phrase(obj, source="summary.purpose")
            if an is None and verb:
                if re.search(r"furnish|supply|purchase|procure|deliver", verb):
                    an = {"category": "product", "subtype": verb, "strength": "weak",
                          "rule": f"summary.purpose_verb:{verb}", "quote": obj[:160]}
                elif re.search(r"construct|install|replace|repair|remove|design|conduct|perform|staff", verb):
                    an = {"category": "construction" if "construct" in verb else "service", "subtype": verb,
                          "strength": "weak", "rule": f"summary.purpose_verb:{verb}", "quote": obj[:160]}
            if not an:
                continue
            quote = body[m.start() : min(len(body), m.end())][:240]
            w = 2.5 if an["strength"] == "strong" else 1.25
            if truncated:
                w /= 2
            if an["category"] == "mixed":
                add("summary", "product", 2.0, an["rule"], quote, "goods_and_services")
                add("summary", "service", 2.0, an["rule"], quote, "goods_and_services")
            else:
                add("summary", an["category"], w, an["rule"], quote, an["subtype"])
            purpose_hits += 1
    # Distinct decisive phrases accumulate (capped per category); repeats of one phrase do not.
    per_cat_summary: dict[str, float] = {}
    for pat, cat, w, rule in _SUMMARY_DECISIVE:
        seen_phrases: set[str] = set()
        for m in pat.finditer(body_masked):
            phrase = re.sub(r"\s+", " ", m.group(0).lower())
            if phrase in seen_phrases or _negated(body_masked, m.start(), m.end()):
                continue
            if per_cat_summary.get(cat, 0.0) >= 3.0:
                break
            seen_phrases.add(phrase)
            per_cat_summary[cat] = per_cat_summary.get(cat, 0.0) + w
            add("summary", cat, w, f"summary.{rule}", body[max(0, m.start() - 30) : m.end() + 60])
    vq = _VQ_ANYWHERE.search(body_masked[:3000])
    if vq and not _negated(body_masked, vq.start(), vq.end()):
        add("summary", "vendor_qualification", 1.5, "summary.vendor_qualification",
            body[max(0, vq.start() - 30) : vq.end() + 60])

    # --- Agency codes ---
    cs = _codes_signal(agency_codes)
    if cs and cs.get("category"):
        add("codes", cs["category"], cs["weight"], "codes.taxonomy_side", cs["quote"])

    # --- Provider kind (weak; may be inferred upstream) ---
    pk = str(procurement_kind or "").strip().lower()
    if pk in {"services", "service"}:
        add("provider_kind", "service", 1.0, "provider.procurement_kind", f"procurement_kind={pk}")
    elif pk in {"goods", "commodity", "commodities", "products"}:
        add("provider_kind", "product", 1.0, "provider.procurement_kind", f"procurement_kind={pk}")
    elif pk == "construction":
        add("provider_kind", "construction", 1.0, "provider.procurement_kind", f"procurement_kind={pk}")

    return _decide(signals, title_text, body, sources_used, codes_split=(cs or {}).get("split"))


def _service_subclass(title_text: str, signals: list[dict[str, Any]], top: str) -> str:
    # Subtype from what the evidence says (title + quoted spans), never from rule names
    head = " ".join([title_text] + [s["quote"] for s in signals if s["source"] == "title" and s["category"] == top])
    if _SUBTYPE_SOFTWARE.search(head) and not re.search(r"(?i)implementation|integration|support", head):
        return "software_only"
    if _SUBTYPE_MAINT.search(head):
        return "maintenance_repair_services"
    if _SUBTYPE_PROF.search(head):
        return "professional_services"
    return "services"


def _decide(
    signals: list[dict[str, Any]],
    title_text: str,
    body: str,
    sources_used: list[str],
    *,
    codes_split: dict[str, int] | None,
) -> dict[str, Any]:
    scores = {c: 0.0 for c in CATEGORIES}
    by_source: dict[str, set[str]] = {}
    for s in signals:
        scores[s["category"]] += s["weight"]
        by_source.setdefault(s["category"], set()).add(s["source"])
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (top, top_score), (second, second_score) = ranked[0], ranked[1]

    title_sig = next((s for s in signals if s["source"] == "title"), None)
    joint = any(s.get("subtype") == "goods_and_services" for s in signals if s["source"] in {"title", "summary"})
    evidence = [f"{s['rule']}={s['quote']}" for s in sorted(signals, key=lambda x: -x["weight"])]
    conflicts = [
        f"{s['source']}:{s['category']}({s['weight']})"
        for s in signals
        if s["category"] != top and s["weight"] >= 1.5
    ]
    meta = {
        "category_scores": {k: round(v, 2) for k, v in scores.items() if v},
        "evidence_signals": signals,
        "conflicting_signals": conflicts,
        "codes_split": codes_split,
    }

    if not signals or top_score == 0:
        evidence.append("insufficient_evidence")
        return _out("unknown_mixed", evidence, True, sources_used, confidence="none", **meta)

    # Vendor-qualification and non-purchase heads in the title are decisive
    if title_sig and title_sig["category"] in {"vendor_qualification", "non_purchase"} and title_sig["weight"] >= 3:
        cls = "vendor_qualification" if title_sig["category"] == "vendor_qualification" else "not_applicable_non_solicitation"
        return _out(cls, evidence, False, sources_used, confidence="high", **meta)

    # Joint goods + services explicitly named together
    if joint and scores["product"] >= DECISION_MIN_SCORE and scores["service"] >= DECISION_MIN_SCORE:
        cls = "mixed_product_service"
        if any(s.get("subtype") == "significant_installation" for s in signals):
            cls = "product_with_significant_installation"
        conf = "high" if len(by_source.get("product", set()) | by_source.get("service", set())) >= 2 else "medium"
        return _out(cls, evidence, True, sources_used, confidence=conf, **meta)

    # Service vs construction disagreement still establishes "work, not goods"
    work_pair = {top, second} == {"service", "construction"}
    if work_pair and top_score >= DECISION_MIN_SCORE:
        work = scores["service"] + scores["construction"]
        others = max(scores["product"], scores["non_purchase"], scores["vendor_qualification"])
        if work - others >= 2 * DECISION_MIN_MARGIN and (top_score - second_score) < DECISION_MIN_MARGIN:
            evidence.append(
                f"work_not_goods:service={scores['service']:.1f}_construction={scores['construction']:.1f}"
            )
            cls = "construction" if top == "construction" else _service_subclass(title_text, signals, top)
            return _out(cls, evidence, False, sources_used, confidence="medium", **meta)

    if top_score < DECISION_MIN_SCORE or (top_score - second_score) < DECISION_MIN_MARGIN:
        if top_score > 0 and second_score > 0 and top_score - second_score < DECISION_MIN_MARGIN:
            evidence.append(f"conflicting_evidence:{top}={top_score:.1f}_vs_{second}={second_score:.1f}")
        else:
            evidence.append(f"weak_evidence:{top}={top_score:.1f}")
        return _out("unknown_mixed", evidence, True, sources_used, confidence="low", leaning=top, **meta)

    agreeing_sources = by_source.get(top, set())
    strong_title = bool(title_sig and title_sig["category"] == top and title_sig["weight"] >= 3)
    opposing_strong = any(s["weight"] >= 2.0 for s in signals if s["category"] != top)
    if not opposing_strong and (strong_title or (top_score >= 4.5 and len(agreeing_sources) >= 2)):
        conf = "high"
    else:
        conf = "medium"

    top_signals = [s for s in signals if s["category"] == top]
    if top == "product":
        if any(s.get("subtype") == "significant_installation" for s in top_signals):
            cls = "product_with_significant_installation"
        elif any(s.get("subtype") == "rental" for s in top_signals):
            cls = "product_rental_leasing"
        else:
            cls = "product_supply"
        return _out(cls, evidence, True, sources_used, confidence=conf, **meta)
    if top == "service":
        cls = _service_subclass(title_text, signals, top)
        return _out(cls, evidence, False, sources_used, confidence=conf, **meta)
    if top == "construction":
        return _out("construction", evidence, False, sources_used, confidence=conf, **meta)
    if top == "vendor_qualification":
        return _out("vendor_qualification", evidence, False, sources_used, confidence=conf, **meta)
    return _out("not_applicable_non_solicitation", evidence, False, sources_used, confidence=conf, **meta)


def _out(
    cls: str,
    evidence: list[str],
    include: bool,
    sources_used: list[str],
    *,
    confidence: str = "low",
    leaning: str | None = None,
    category_scores: dict[str, float] | None = None,
    evidence_signals: list[dict[str, Any]] | None = None,
    conflicting_signals: list[str] | None = None,
    codes_split: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "requirement_class": cls,
        "classification_evidence": evidence,
        "include_in_product_resale": include and cls in PRODUCT_CLASSES | {"unknown_mixed", "mixed_product_service"},
        "evidence_sources": sources_used,
        "classification_stage": "central",
        "decision_confidence": confidence,
        "leaning": leaning,
        "category_scores": category_scores or {},
        "evidence_signals": evidence_signals or [],
        "conflicting_signals": conflicting_signals or [],
        "codes_split": codes_split,
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
    desc = out.get("description") or out.get("summary")
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
        agency_codes=agency_codes_from_record(out),
        procurement_kind=out.get("procurement_kind"),
    )
    out["requirement_class"] = clf["requirement_class"]
    out["classification_evidence"] = clf["classification_evidence"]
    out["include_in_product_resale"] = clf["include_in_product_resale"]
    out["evidence_sources"] = clf.get("evidence_sources")
    out["classification_stage"] = "central"
    out["classification_confidence"] = clf.get("decision_confidence")
    # Normalize / re-infer when missing or placeholder "unknown"
    cur = str(out.get("solicitation_status") or "").strip().lower()
    if not cur or cur in {"unknown", "null", "none"}:
        out["solicitation_status"] = _infer_status(out)
    return out


def agency_codes_from_record(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Agency/portal-assigned commodity codes retained on a provider record."""
    cl = rec.get("classification")
    if not isinstance(cl, dict) and isinstance(rec.get("evidence"), dict):
        cl = rec["evidence"].get("classification")
    if not isinstance(cl, dict):
        return []
    return [sc for sc in (cl.get("source_codes") or []) if isinstance(sc, dict)]


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
