"""Owner opportunity checklist triage — full get_opportunity body, strict JSON, title mandatory.

Rules (non-negotiable):
- Read EVERY field, including title. procurement_kind=unknown never alone → UNCLEAR.
- Model output must be valid JSON. Invalid JSON is a hard failure after one retry —
  never silently rebadge as UNCLEAR.
- SERVICE or MIX → SKIP. PRODUCT only → FETCH_DOCUMENTS (investigate further).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from m3.config import get_settings
from m3.research.budget import estimate_response_cost
from m3.research.openai_client import _extract_usage, _output_text, require_json_object

MODEL_DEFAULT = "gpt-4o-mini"

SYSTEM = (
    "You are M3 Supply's solicitation triage analyst. "
    "You MUST read the COMPLETE opportunity JSON — every field, especially title + description. "
    "Never invent facts. "
    "Decide from title/description/line items — NOT from procurement_kind, NAICS, NIGP, "
    "tradelines, or classification labels alone. Those labels are often wrong "
    "(e.g. procurement_kind=mixed on a pure concrete supply bid). "
    "M3 is a goods supplier: we want agencies BUYING tangible products/materials. "
    "PRODUCT (FETCH_DOCUMENTS) when the award is for supplying goods, including "
    "furnish-and-deliver, purchase-and-delivery, bulk fuel/propane/gasoline supply, "
    "requirements contracts / 'service contracts' that only furnish products "
    "(e.g. vehicle filters), and on-call material supply. "
    "Delivery of goods is STILL PRODUCT — delivery alone is not a service MIX. "
    "MIX / SERVICE_OR_MIX / SKIP only when the vendor must also perform install, "
    "construction, removal+install, maintenance, repair, labor, training/"
    "implementation as a turnkey obligation, or other non-supply work to win. "
    "agency_is_buying=true ONLY when the government/agency itself is purchasing "
    "goods from vendors. SKIP grant/incentive/rebate/voucher programs where the "
    "agency funds third parties (e.g. carriers) to buy equipment — that is not "
    "the agency buying from M3. "
    "SKIP agency surplus sales / equipment for sale. "
    "SKIP sole source, exemption, intent to award, demo-only, informational-only. "
    "SKIP pharmacy benefits management (PBM), benefits administration, health/vision "
    "insurance plan services, managed care, consulting, and other pure professional "
    "services — those are SERVICE even if the packet includes spreadsheets. "
    "SKIP perishable food / grocery buys: food items, milk, bread, produce, meat, "
    "dairy, frozen foods, cafeteria food commodities, and other perishables. "
    "M3 does not supply perishable food. Food-service equipment or non-food supplies "
    "can still be PRODUCT; edible/perishable commodities must be SKIP. "
    "Do NOT set FETCH_DOCUMENTS unless want_type=PRODUCT, agency_is_buying=true, "
    "is_product_only=true. "
    "If what_they_want is services/management/administration with no tangible goods, "
    "want_type must be SERVICE and next_step SKIP — never PRODUCT. "
    "Return a single valid JSON object only. No markdown. No commentary."
)

SCHEMA = {
    "nb_id": "string",
    "posted_date": "string|null",
    "agency": "string|null",
    "want_type": "PRODUCT|SERVICE|MIX|UNCLEAR",
    "decision": "PRODUCT|SERVICE_OR_MIX|UNCLEAR",
    "is_product_only": "boolean|null",
    "agency_is_buying": "boolean — true only if the agency is purchasing goods from vendors",
    "what_they_want": "string|null",
    "quantity": "string|null",
    "close_date": "string|null",
    "why": "string",
    "title_used": "boolean — must be true; confirm you used the title",
    "source_snippets": ["string — short verbatim quotes"],
    "documents_indicated": "boolean",
    "document_count_if_stated": "number|null",
    "next_step": "SKIP|FETCH_DOCUMENTS|OWNER_REVIEW_RAW",
}

# Agency selling assets — never a doc-fetch candidate for M3.
_SURPLUS_SALE_RE = re.compile(
    r"(?i)("
    r"notice\s+of\s+sale|"
    r"sale\s+of\s+municipal|"
    r"equipment\s+for\s+sale|"
    r"surplus\s+(?:sale|auction|equipment|property|vehicles?)|"
    r"auction\s+of\s+(?:surplus|municipal|city|county)|"
    r"vehicles?\s+for\s+sale|"
    r"description\s+of\s+equipment\s+for\s+sale"
    r")"
)

# Agency is NOT the buyer — grant/incentive programs funding someone else to purchase.
_AGENCY_NOT_BUYING_RE = re.compile(
    r"(?i)("
    r"purchase\s+incentive\s+(?:project|program)|"
    r"incentive\s+(?:project|program)|"
    r"grant\s+agreement|"
    r"rebate\s+program|"
    r"voucher\s+program|"
    r"invites?\s+the\s+submittal\s+of\s+proposals\s+from.{0,120}to\s+purchase|"
    r"licensed\s+motor\s+carriers|"
    r"selected\s+lmcs\s+shall"
    r")"
)

# Clear labor/install in body → not pure goods (even if title looks like furniture/equipment).
# Keep narrow: bare "install" alone is too common in pure-supply notes.
# Do NOT treat delivery, furnish-and-deliver, or "service contract for furnishing goods" as labor.
_BODY_INSTALL_LABOR_RE = re.compile(
    r"(?i)("
    r"disassembl(?:e|ing)|"
    r"remov(?:e|ing)\s+the\s+existing|"
    r"build(?:ing)?\s+and\s+install|"
    r"furnish(?:ing)?\s+and\s+install|"
    r"including\s+installation|"
    r"installation\s+(?:services|labor|of\s+the)|"
    r"labor\s+and\s+materials|"
    r"\bconstruction\s+of\b|"
    r"turnkey\s+solution|"
    r"implementation\s+and\s+training"
    r")"
)

# Strong service-procurement signals — fail closed even if the model says PRODUCT.
# Wrong PRODUCT is as bad as no info: never fetch docs / keep these.
_SERVICE_CONTRACT_BUY_RE = re.compile(
    r"(?i)("
    r"pharmacy\s+benefits?\s+management|"
    r"benefits?\s+management\s+services?|"
    r"\bPBM\b|"
    r"managed\s+care\s+(?:services?|plan|organization)|"
    r"vision\s+(?:insurance|benefits?|plan)\s+services?|"
    r"health\s+(?:insurance|plan)\s+administration|"
    r"professional\s+services?\s+(?:contract|agreement|rfp)|"
    r"request\s+for\s+proposals?\s+for\s+.{0,80}"
    r"(?:management|consulting|administration)\s+services?|"
    r"procure(?:ment)?\s+of\s+.{0,60}"
    r"(?:management|consulting|administration)\s+services?|"
    r"seeks?\s+to\s+procure\s+.{0,60}(?:management|consulting)\s+services?"
    r")"
)

# Narrow: require service-as-the-buy, not "Animal Services Department" / "Incident Management".
_WHAT_THEY_WANT_SERVICE_ASK_RE = re.compile(
    r"(?i)("
    r"\b(?:management|administration|consulting)\s+services?\b|"
    r"\b(?:professional|janitorial|staffing|maintenance)\s+services?\b|"
    r"\bservices?\s+(?:contract|agreement)\b|"
    r"\bto\s+procure\s+.{0,60}services?\b|"
    r"\bseeks?\s+to\s+procure\s+.{0,60}services?\b|"
    r"\binsurance\s+plan\b|"
    r"\bpharmacy\s+benefits?|"
    r"\bbenefits?\s+management\b|"
    r"\bfishing\s+services?\b|"
    r"\bcontrol\s+services?\b|"
    r"\bsupport\s+services?\b"
    r")"
)

_WHAT_THEY_WANT_GOODS_CUE_RE = re.compile(
    r"(?i)\b("
    r"goods|products?|supplies|materials|equipment|commodit|"
    r"furnish|purchase\s+of|supply\s+of|delivery\s+of|"
    r"fuel|propane|gasoline|diesel|filters?|concrete|sandbags?"
    r")\b"
)

# Goods supply patterns that first-pass models wrongly call MIX (delivery / labels).
_GOODS_SUPPLY_RE = re.compile(
    r"(?i)("
    r"furnish(?:ing)?\s+and\s+deliver(?:y|ies)?"
    r"|purchase\s+and\s+deliver(?:y|ies)?"
    r"|supply\s+and\s+deliver(?:y|ies)?"
    r"|on[\s-]?demand\s+delivery\s+of"
    r"|bulk\s+(?:liquid\s+)?(?:propane|gasoline|diesel|fuel)"
    r"|(?:propane|gasoline|diesel)\s+fuel\s+supply"
    r"|bulk\s+propane\s+fuel\s+supply"
    r"|fuel\s+supply"
    r"|service\s+contracts?\s*(?:\([^)]*\))?\s*for\s+the\s+furnishing"
    r"|furnishing\s+of\s+all .{0,80}requirements\s+for"
    r"|on[\s-]?call,\s+as\s+needed"
    r")"
)

_PRODUCT_TITLE = re.compile(
    r"(?i)\b("
    r"purchase|furnish|supply|supplies|materials|equipment|vehicles?|truck|suv|"
    r"pipe|fittings?|lubricants?|concrete|commodit|office\s+supply|"
    r"instructional\s+materials|goods|delivery\s+of"
    r")\b"
)
_SERVICE_TITLE = re.compile(
    r"(?i)\b("
    r"rebuild|repair|renovation|improvements?|construction|plowing|staffing|"
    r"janitorial|maintenance|install(?:ation)?|professional\s+services|"
    r"labor\b|pump\s+track|skate\s+park"
    r")\b"
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def title_signal_decision(title: str) -> dict[str, Any] | None:
    """Deterministic title safety net when model wrongly returns UNCLEAR."""
    from m3.research.nextbid_owner_filters import listing_skip_before_detail_reason

    t = (title or "").strip()
    if not t:
        return None
    pre_skip = listing_skip_before_detail_reason(t)
    if pre_skip:
        return {
            "want_type": "MIX",
            "decision": "SERVICE_OR_MIX",
            "next_step": "SKIP",
            "why": f"Pre-detail skip ({pre_skip}): {t}",
            "title_override": True,
        }
    food = perishable_food_reason(t)
    if food:
        return {
            "want_type": "MIX",
            "decision": "SERVICE_OR_MIX",
            "next_step": "SKIP",
            "why": f"Perishable food out of scope ({food}): {t}",
            "title_override": True,
        }
    has_p = bool(_PRODUCT_TITLE.search(t))
    has_s = bool(_SERVICE_TITLE.search(t))
    if has_s and not has_p:
        return {
            "want_type": "SERVICE",
            "decision": "SERVICE_OR_MIX",
            "next_step": "SKIP",
            "why": f"Title alone is service/construction: {t}",
            "title_override": True,
        }
    if has_s and has_p:
        # e.g. "Purchase & Delivery" is product; "Furnish labor material" is mix
        if re.search(r"(?i)\b(labor|rebuild|repair|improvements?|renovation|construction)\b", t):
            return {
                "want_type": "MIX",
                "decision": "SERVICE_OR_MIX",
                "next_step": "SKIP",
                "why": f"Title indicates mix/service execution: {t}",
                "title_override": True,
            }
    if has_p and not has_s:
        return {
            "want_type": "PRODUCT",
            "decision": "PRODUCT",
            "next_step": "FETCH_DOCUMENTS",
            "why": f"Title alone is product purchase: {t}",
            "title_override": True,
        }
    return None


def normalize_decision(parsed: dict[str, Any]) -> tuple[str, str]:
    """want_type is authoritative for product vs service/mix (owner checklist Q3)."""
    want = str(parsed.get("want_type") or "").upper()
    decision = str(parsed.get("decision") or "").upper()
    next_step = str(parsed.get("next_step") or "").upper()
    agency_buying = parsed.get("agency_is_buying")
    is_product_only = parsed.get("is_product_only")
    # Prefer want_type when present — model sometimes sets decision=SERVICE_OR_MIX
    # while want_type=PRODUCT on a clear goods buy.
    if want in {"SERVICE", "MIX"}:
        return "SERVICE_OR_MIX", "SKIP"
    if agency_buying is False or is_product_only is False:
        return "SERVICE_OR_MIX", "SKIP"
    if want == "PRODUCT":
        if next_step == "OWNER_REVIEW_RAW":
            return "PRODUCT", next_step
        return "PRODUCT", "FETCH_DOCUMENTS"
    if decision in {"SERVICE_OR_MIX", "SERVICE_OR_CONSTRUCTION"}:
        return "SERVICE_OR_MIX", "SKIP"
    if decision == "PRODUCT":
        if next_step == "OWNER_REVIEW_RAW":
            return "PRODUCT", next_step
        return "PRODUCT", "FETCH_DOCUMENTS"
    if not decision:
        decision = "UNCLEAR"
    if not next_step:
        next_step = "OWNER_REVIEW_RAW" if decision == "UNCLEAR" else "SKIP"
    return decision, next_step


def opportunity_text_blob(opportunity: dict[str, Any]) -> str:
    parts = [
        str(opportunity.get("title") or ""),
        str(opportunity.get("description") or ""),
        str(opportunity.get("summary") or ""),
        str(opportunity.get("ai_summary") or ""),
        str(opportunity.get("notice_type") or ""),
    ]
    return "\n".join(parts)


# Portal chrome only (e.g. Nebraska DAS sidebar link) — not this solicitation selling assets.
_SURPLUS_NAV_CHROME_RE = re.compile(
    r"(?i)\bsurplus\s+property\s+auction\b|"
    r"\blinks?\s+of\s+interest\b|"
    r"\bsearch\s+state\s+contract\s+database\b"
)


def body_surplus_sale_reason(opportunity: dict[str, Any]) -> str | None:
    """Agency selling assets — not an M3 buy. Deterministic pre-docs skip."""
    blob = opportunity_text_blob(opportunity)
    # Strip public-site nav phrases so "Surplus Property Auction" links don't kill ITBs.
    cleaned = _SURPLUS_NAV_CHROME_RE.sub(" ", blob)
    m = _SURPLUS_SALE_RE.search(cleaned)
    if m:
        return f"agency_surplus_sale:{m.group(0).lower()}"
    return None


def body_agency_not_buying_reason(opportunity: dict[str, Any]) -> str | None:
    """Grant/incentive programs — agency funds others to buy; agency is not M3's customer."""
    blob = opportunity_text_blob(opportunity)
    m = _AGENCY_NOT_BUYING_RE.search(blob)
    if m:
        return f"agency_not_buying:{m.group(0).lower()}"
    return None


def body_install_labor_reason(opportunity: dict[str, Any]) -> str | None:
    """Install/labor language in body → not pure goods purchase."""
    blob = opportunity_text_blob(opportunity)
    # Surplus sales already handled separately.
    if body_surplus_sale_reason(opportunity):
        return None
    m = _BODY_INSTALL_LABOR_RE.search(blob)
    if m:
        return f"install_or_labor_in_body:{m.group(0).lower()}"
    return None


def body_service_contract_reason(opportunity: dict[str, Any]) -> str | None:
    """Deterministic fail-closed: body is a service buy (PBM, benefits admin, etc.)."""
    blob = opportunity_text_blob(opportunity)
    m = _SERVICE_CONTRACT_BUY_RE.search(blob)
    if m:
        return f"service_contract_buy:{m.group(0).lower()}"
    return None


# Perishable / edible food commodities — hard NO for M3 (not food-service equipment).
_PERISHABLE_FOOD_RE = re.compile(
    r"(?i)\b("
    r"food\s+items|"
    r"perishable(?:s)?|"
    r"grocer(?:y|ies)|"
    r"fresh\s+(?:fruit|vegetable|produce)|"
    r"(?:fs\s+)?(?:milk|bread)\s+supply|"
    r"milk\s+supply|"
    r"bread\s+supply|"
    r"food\s+service,\s+food|"
    r"canteen\s+(?:food|resale)|"
    r"frozen\s+(?:foods?|meats?|turkey|vegetables?)|"
    r"meat(?:s)?\s+(?:and|&)\s+(?:poultry|seafood)|"
    r"dairy\s+products?|"
    r"cafeteria\s+food|"
    r"edible\s+food"
    r")\b"
)


def perishable_food_reason(*texts: Any) -> str | None:
    """Fail-closed: perishable food / grocery commodity buys are out of scope."""
    blob = "\n".join(str(t or "") for t in texts)
    m = _PERISHABLE_FOOD_RE.search(blob)
    if m:
        return f"perishable_food:{m.group(0).lower()}"
    return None


def body_perishable_food_reason(opportunity: dict[str, Any]) -> str | None:
    return perishable_food_reason(opportunity_text_blob(opportunity))


def triage_product_service_contradiction_reason(
    triage: dict[str, Any],
    *,
    opportunity: dict[str, Any] | None = None,
) -> str | None:
    """Fail closed when labeled PRODUCT (or free-form service want) contradicts goods.

    Wrong PRODUCT is as bad as no info — never authorize docs or keep.
    """
    raw_want = str(triage.get("want_type") or "")
    want = raw_want.upper()
    decision = str(triage.get("decision") or "").upper()
    parsed = triage.get("parsed") if isinstance(triage.get("parsed"), dict) else {}
    what = str(triage.get("what_they_want") or parsed.get("what_they_want") or "")

    # Free-form want_type from doc-read finals (e.g. "Pharmacy Benefits Management").
    if want and want not in {"PRODUCT", "UNCLEAR", "SERVICE", "MIX", "SERVICE_OR_MIX"}:
        if _SERVICE_CONTRACT_BUY_RE.search(raw_want) or re.search(
            r"(?i)pharmacy|benefits?\s+management|\bPBM\b|managed\s+care|services?",
            raw_want,
        ):
            return f"want_type_is_service_not_product:{raw_want}"

    claiming_product = want == "PRODUCT" or decision == "PRODUCT"
    if not claiming_product:
        return None

    blob_parts = [raw_want, what]
    if opportunity is not None:
        blob_parts.insert(0, opportunity_text_blob(opportunity))
    blob = "\n".join(blob_parts)

    m = _SERVICE_CONTRACT_BUY_RE.search(blob)
    if m:
        return f"product_label_contradicts_service:{m.group(0).lower()}"

    food = perishable_food_reason(blob)
    if food:
        return f"product_label_contradicts_{food}"

    if what and _WHAT_THEY_WANT_SERVICE_ASK_RE.search(what):
        if not _WHAT_THEY_WANT_GOODS_CUE_RE.search(what):
            return f"what_they_want_is_services_not_goods:{what[:160]}"
    return None


def goods_supply_salvage(opportunity: dict[str, Any]) -> dict[str, Any] | None:
    """Salvage false MIX/SERVICE when body is clearly goods supply (+ delivery OK).

    Owner lesson: mislabeling furnish/deliver concrete, filter requirements contracts,
    sandbag purchase/delivery, and bulk propane as MIX skips real revenue.
    """
    if body_surplus_sale_reason(opportunity):
        return None
    if body_agency_not_buying_reason(opportunity):
        return None
    if body_install_labor_reason(opportunity):
        return None
    if body_service_contract_reason(opportunity):
        return None
    if body_perishable_food_reason(opportunity):
        return None
    blob = opportunity_text_blob(opportunity)
    if not blob.strip():
        return None
    m = _GOODS_SUPPLY_RE.search(blob)
    if not m:
        return None
    return {
        "want_type": "PRODUCT",
        "decision": "PRODUCT",
        "next_step": "FETCH_DOCUMENTS",
        "agency_is_buying": True,
        "is_product_only": True,
        "why": (
            "Goods supply salvage: product purchase or furnish/deliver "
            f"(matched {m.group(0)!r}); delivery/requirements-contract wording is not MIX"
        ),
        "title_override": True,
        "goods_supply_salvage": True,
    }


def opportunity_shows_no_documents(opportunity: dict[str, Any]) -> bool:
    """True when get_opportunity already indicates there is nothing to fetch."""
    docs = opportunity.get("documents")
    empty_docs = docs in (None, [], {}) or (isinstance(docs, list) and len(docs) == 0)
    dc = opportunity.get("document_count")
    ac = opportunity.get("attachment_count")
    dc_empty = dc in (None, 0, "0")
    ac_empty = ac in (None, 0, "0")
    return bool(empty_docs and dc_empty and ac_empty)


def authorize_document_fetch(
    triage: dict[str, Any],
    *,
    opportunity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Hard gate before any list_documents / list_files / download credits.

    Requires a completed get_opportunity triage that confirms a strict agency
    PRODUCT purchase. Fail closed on anything else — including surplus sales
    and opportunities that already show zero documents.
    """
    want = str(triage.get("want_type") or "").upper()
    decision = str(triage.get("decision") or "").upper()
    next_step = str(triage.get("next_step") or "").upper()
    parsed = triage.get("parsed") if isinstance(triage.get("parsed"), dict) else {}
    agency_buying = parsed.get("agency_is_buying", triage.get("agency_is_buying"))
    is_product_only = parsed.get("is_product_only", triage.get("is_product_only"))

    if not triage.get("ok"):
        return {
            "authorized": False,
            "reason": f"triage_not_ok:{triage.get('status') or triage.get('error') or 'unknown'}",
        }
    if opportunity is not None:
        surplus = body_surplus_sale_reason(opportunity)
        if surplus:
            return {"authorized": False, "reason": surplus}
        not_buying = body_agency_not_buying_reason(opportunity)
        if not_buying:
            return {"authorized": False, "reason": not_buying}
        labor = body_install_labor_reason(opportunity)
        if labor:
            return {"authorized": False, "reason": labor}
        service_buy = body_service_contract_reason(opportunity)
        if service_buy:
            return {"authorized": False, "reason": service_buy}
        food = body_perishable_food_reason(opportunity)
        if food:
            return {"authorized": False, "reason": food}
        if opportunity_shows_no_documents(opportunity):
            return {"authorized": False, "reason": "opportunity_shows_no_documents"}
    contradiction = triage_product_service_contradiction_reason(
        triage, opportunity=opportunity
    )
    if contradiction:
        return {"authorized": False, "reason": contradiction}
    if want != "PRODUCT" or decision != "PRODUCT":
        return {
            "authorized": False,
            "reason": f"not_strict_product:want={want}:decision={decision}",
        }
    if next_step != "FETCH_DOCUMENTS":
        return {"authorized": False, "reason": f"next_step_blocks_fetch:{next_step}"}
    if agency_buying is False:
        return {"authorized": False, "reason": "agency_is_buying_false"}
    if is_product_only is False:
        return {"authorized": False, "reason": "is_product_only_false"}
    return {
        "authorized": True,
        "reason": "strict_product_purchase_confirmed",
        "want_type": want,
        "decision": decision,
        "next_step": next_step,
    }


TITLE_TRIAGE_SYSTEM = (
    "You are M3 Supply's listing-title screener. "
    "M3 only wants open competitive purchases of tangible goods/products. "
    "SKIP true services, construction, labor, install/maintenance, sole source, "
    "exemption, intent-to-award, demo-only, and informational notices. "
    "PURSUE furnish-and-deliver, purchase-and-delivery, fuel/propane supply, "
    "materials, equipment, and product requirements contracts — delivery of goods "
    "is still a goods buy, not a skip. "
    "PURSUE only when the title clearly indicates a competitive product/goods buy "
    "worth spending a get_opportunity credit on. "
    "Use ONLY the provided titles/agencies — do not invent. "
    "Return one valid JSON object only."
)

TITLE_TRIAGE_SCHEMA = {
    "results": [
        {
            "nb_id": "string",
            "action": "PURSUE_GET_OPPORTUNITY|SKIP",
            "want_guess": "PRODUCT|SERVICE|MIX|NON_COMPETITIVE|UNCLEAR",
            "why": "short reason from the title",
        }
    ]
}


def opportunity_corpus(opp: dict[str, Any]) -> str:
    return json.dumps(opp, indent=2, ensure_ascii=False, default=str)


def triage_opportunities_parallel(
    items: list[dict[str, Any]],
    *,
    max_workers: int = 10,
) -> list[dict[str, Any]]:
    """Parallel full-opportunity triage. items: [{nb_id, opportunity}, ...]."""
    from m3.research.parallel_work import map_parallel

    def _one(row: dict[str, Any]) -> dict[str, Any]:
        return triage_opportunity(nb_id=str(row["nb_id"]), opportunity=row["opportunity"])

    return map_parallel(_one, items, max_workers=max_workers, ordered=True)


def triage_listing_titles(
    listings: list[dict[str, Any]],
    *,
    client: Any | None = None,
    model: str | None = None,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """OpenAI screen of search titles BEFORE any get_opportunity spend.

    listings items: nb_id, title, optional agency/notice_type.
    """
    from openai import OpenAI

    settings = get_settings()
    model = model or settings.openai_default_model or MODEL_DEFAULT
    if client is None:
        api_key = (settings.openai_api_key or "").strip()
        if not api_key:
            return {
                "ok": False,
                "status": "ERROR",
                "error": "OPENAI_API_KEY missing",
                "openai_spent_usd": 0.0,
                "results": [],
            }
        client = OpenAI(api_key=api_key)

    compact = []
    for row in listings:
        nb = str(row.get("nb_id") or "")
        if not nb:
            continue
        compact.append(
            {
                "nb_id": nb,
                "title": str(row.get("title") or "")[:240],
                "agency": str(row.get("agency") or "")[:120],
                "notice_type": str(row.get("notice_type") or "")[:80],
            }
        )
    if not compact:
        return {
            "ok": True,
            "status": "OK",
            "openai_spent_usd": 0.0,
            "results": [],
            "pursue_ids": [],
            "skip_ids": [],
        }

    user = (
        "Screen each listing TITLE. For each nb_id return action "
        "PURSUE_GET_OPPORTUNITY or SKIP.\n"
        "PURSUE only competitive tangible-goods buys.\n"
        "SKIP services/labor/construction/mix/sole-source/exemption/demo/informational.\n"
        "If unsure from title alone → SKIP (do not spend get_opportunity on guesses).\n"
        f"JSON shape: {json.dumps(TITLE_TRIAGE_SCHEMA)}\n\n"
        f"LISTINGS:\n{json.dumps(compact, indent=2, ensure_ascii=False)}"
    )

    spent = Decimal("0")
    attempts: list[dict[str, Any]] = []
    parsed: dict[str, Any] | None = None
    last_text = ""
    for attempt in range(1, max_attempts + 1):
        prompt = user if attempt == 1 else (
            "Previous reply was INVALID JSON. Return one valid JSON object only.\n\n"
            f"Bad reply:\n{last_text[:1500]}\n\n{user}"
        )
        resp, text, usage, cost = _call_model(client, model, TITLE_TRIAGE_SYSTEM, prompt)
        spent += cost
        last_text = text
        ok, obj, err = require_json_object(text)
        attempts.append(
            {
                "attempt": attempt,
                "parse_ok": ok,
                "parse_error": err,
                "response_id": getattr(resp, "id", None),
                "openai_spent_usd": float(cost),
            }
        )
        if ok and obj is not None:
            parsed = obj
            break

    if parsed is None:
        return {
            "ok": False,
            "status": "PARSE_ERROR",
            "error": "title_triage_invalid_json_after_retry",
            "openai_spent_usd": float(spent),
            "raw_text": last_text,
            "attempts": attempts,
            "results": [],
            "pursue_ids": [],
            "skip_ids": [],
        }

    by_id = {c["nb_id"]: c for c in compact}
    rows_out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in parsed.get("results") or []:
        if not isinstance(item, dict):
            continue
        nb = str(item.get("nb_id") or "")
        if not nb or nb not in by_id or nb in seen:
            continue
        seen.add(nb)
        action = str(item.get("action") or "SKIP").upper()
        if action not in {"PURSUE_GET_OPPORTUNITY", "SKIP"}:
            action = "SKIP"
        want = str(item.get("want_guess") or "UNCLEAR").upper()
        # Fail closed: only PRODUCT pursue
        if action == "PURSUE_GET_OPPORTUNITY" and want not in {"PRODUCT", "UNCLEAR"}:
            action = "SKIP"
        if action == "PURSUE_GET_OPPORTUNITY" and want == "UNCLEAR":
            action = "SKIP"
        rows_out.append(
            {
                "nb_id": nb,
                "title": by_id[nb]["title"],
                "agency": by_id[nb]["agency"],
                "action": action,
                "want_guess": want,
                "why": str(item.get("why") or "")[:240],
            }
        )
    # Any listing the model omitted → SKIP (fail closed)
    for nb, meta in by_id.items():
        if nb in seen:
            continue
        rows_out.append(
            {
                "nb_id": nb,
                "title": meta["title"],
                "agency": meta["agency"],
                "action": "SKIP",
                "want_guess": "UNCLEAR",
                "why": "omitted_from_model_response_fail_closed",
            }
        )

    pursue_ids = [r["nb_id"] for r in rows_out if r["action"] == "PURSUE_GET_OPPORTUNITY"]
    skip_ids = [r["nb_id"] for r in rows_out if r["action"] == "SKIP"]
    return {
        "ok": True,
        "status": "OK",
        "model": model,
        "openai_spent_usd": float(spent),
        "attempts": attempts,
        "results": rows_out,
        "pursue_ids": pursue_ids,
        "skip_ids": skip_ids,
        "analyzed_at": _utc(),
        "checklist_version": "owner_title_pre_get_opportunity_v1",
    }


def build_user_prompt(nb_id: str, opp: dict[str, Any]) -> str:
    title = str(opp.get("title") or "")
    return (
        f"NB_ID: {nb_id}\n"
        f"TITLE (mandatory — you must use this): {title}\n\n"
        "Answer from the COMPLETE opportunity JSON only, in order:\n"
        "1) When was this posted to the site?\n"
        "2) What agency posted the solicitation?\n"
        "3) Is the agency BUYING goods from vendors? "
        "agency_is_buying=true ONLY if the government/agency itself is the purchaser. "
        "Grant/incentive/rebate programs where third parties buy (and agency funds them) "
        "→ agency_is_buying=false. Surplus sales → agency_is_buying=false.\n"
        "4) PRODUCT, SERVICE, or MIX? Use the body facts — ignore wrong "
        "procurement_kind/NAICS/NIGP/tradeline labels.\n"
        "   PRODUCT includes: furnish-and-deliver materials, purchase-and-delivery, "
        "bulk fuel/propane supply into an existing tank, and 'service contracts' that "
        "only furnish products (no install/labor).\n"
        "   Delivery of goods ≠ MIX.\n"
        "   MIX only if install/labor/construction/maintenance is required to win.\n"
        "5) SERVICE, true MIX, surplus sale, or agency_is_buying=false → "
        "decision=SERVICE_OR_MIX, next_step=SKIP, is_product_only=false "
        "(still fill posted_date + agency).\n"
        "6) PRODUCT → is_product_only=true, agency_is_buying=true, what they want, "
        "how many, when it closes; decision=PRODUCT, next_step=FETCH_DOCUMENTS.\n"
        "7) UNCLEAR only if title AND all other fields still leave product vs "
        "service undecidable. Empty/unknown/mixed procurement_kind is NOT undecidable "
        "and is NOT automatic MIX when the description lists goods to buy. "
        "'NOTICE OF SALE' / 'equipment for sale' is NEVER a product purchase.\n"
        "Set title_used=true.\n"
        f"JSON shape: {json.dumps(SCHEMA)}\n\n"
        f"COMPLETE_OPPORTUNITY_JSON:\n{opportunity_corpus(opp)}"
    )


def _call_model(client: Any, model: str, system: str, user: str) -> tuple[Any, str, dict[str, int], Decimal]:
    kwargs: dict[str, Any] = {
        "model": model,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    # Force JSON object when the API supports it — invalid structure is still wrong.
    kwargs["text"] = {"format": {"type": "json_object"}}
    try:
        resp = client.responses.create(**kwargs)
    except Exception:
        kwargs.pop("text", None)
        resp = client.responses.create(**kwargs)
    text = _output_text(resp)
    usage = _extract_usage(resp)
    cost = estimate_response_cost(
        model=model,
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        web_search_calls=0,
    )
    return resp, text, usage, cost


def triage_opportunity(
    *,
    nb_id: str,
    opportunity: dict[str, Any],
    client: Any | None = None,
    model: str | None = None,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """Live OpenAI checklist triage with strict JSON + title safety net."""
    from openai import OpenAI

    settings = get_settings()
    model = model or settings.openai_default_model or MODEL_DEFAULT
    if client is None:
        api_key = (settings.openai_api_key or "").strip()
        if not api_key:
            return {
                "ok": False,
                "status": "ERROR",
                "error": "OPENAI_API_KEY missing",
                "nb_id": nb_id,
                "decision": "PARSE_ERROR",
                "next_step": "OWNER_REVIEW_RAW",
                "live_ai_invoked": False,
                "openai_spent_usd": 0.0,
            }
        client = OpenAI(api_key=api_key)

    title = str(opportunity.get("title") or "")
    user = build_user_prompt(nb_id, opportunity)
    spent = Decimal("0")
    attempts: list[dict[str, Any]] = []
    parsed: dict[str, Any] | None = None
    last_text = ""
    last_resp = None
    last_usage = {"input_tokens": 0, "output_tokens": 0}

    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            prompt = user
        else:
            prompt = (
                "Your previous reply was INVALID JSON. That is a hard failure. "
                "Return ONE valid JSON object only — no markdown fences, no single-quoted "
                "keys/values, no commentary. Use this schema and the same opportunity facts.\n\n"
                f"Previous invalid reply:\n{last_text[:2000]}\n\n{user}"
            )
        resp, text, usage, cost = _call_model(client, model, SYSTEM, prompt)
        spent += cost
        last_text, last_resp, last_usage = text, resp, usage
        ok, obj, err = require_json_object(text)
        attempts.append(
            {
                "attempt": attempt,
                "parse_ok": ok,
                "parse_error": err,
                "response_id": getattr(resp, "id", None),
                "openai_spent_usd": float(cost),
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
            }
        )
        if ok and obj is not None:
            parsed = obj
            break

    if parsed is None:
        return {
            "ok": False,
            "status": "PARSE_ERROR",
            "error": "model_returned_invalid_json_after_retry",
            "nb_id": nb_id,
            "title": title,
            "decision": "PARSE_ERROR",
            "next_step": "OWNER_REVIEW_RAW",
            "live_ai_invoked": True,
            "mocked": False,
            "model": model,
            "openai_spent_usd": float(spent),
            "input_tokens": last_usage["input_tokens"],
            "output_tokens": last_usage["output_tokens"],
            "response_id": getattr(last_resp, "id", None),
            "raw_text": last_text,
            "attempts": attempts,
            "analyzed_at": _utc(),
            "checklist_version": "owner_posted_agency_want_qty_close_v2_strict",
        }

    decision, next_step = normalize_decision(parsed)
    title_override = None
    # Always force-skip non-competitive / obvious service titles even if model said PRODUCT.
    from m3.research.nextbid_owner_filters import listing_skip_before_detail_reason

    pre_skip = listing_skip_before_detail_reason(title)
    surplus = body_surplus_sale_reason(opportunity)
    not_buying = body_agency_not_buying_reason(opportunity)
    labor = body_install_labor_reason(opportunity)
    service_buy = body_service_contract_reason(opportunity)
    food = body_perishable_food_reason(opportunity) or perishable_food_reason(title)
    if surplus:
        title_override = {
            "want_type": "MIX",
            "decision": "SERVICE_OR_MIX",
            "next_step": "SKIP",
            "why": f"Agency surplus/sale — not a product purchase ({surplus})",
            "agency_is_buying": False,
            "is_product_only": False,
        }
    elif not_buying:
        title_override = {
            "want_type": "MIX",
            "decision": "SERVICE_OR_MIX",
            "next_step": "SKIP",
            "why": (
                "Agency is not the buyer — grant/incentive/third-party purchase "
                f"({not_buying})"
            ),
            "agency_is_buying": False,
            "is_product_only": False,
        }
    elif labor:
        title_override = {
            "want_type": "MIX",
            "decision": "SERVICE_OR_MIX",
            "next_step": "SKIP",
            "why": f"Body includes install/labor — not pure goods ({labor})",
            "agency_is_buying": True,
            "is_product_only": False,
        }
    elif service_buy:
        title_override = {
            "want_type": "SERVICE",
            "decision": "SERVICE_OR_MIX",
            "next_step": "SKIP",
            "why": f"Body is a service contract — not tangible goods ({service_buy})",
            "agency_is_buying": True,
            "is_product_only": False,
        }
    elif food:
        title_override = {
            "want_type": "MIX",
            "decision": "SERVICE_OR_MIX",
            "next_step": "SKIP",
            "why": f"Perishable food out of scope ({food})",
            "agency_is_buying": True,
            "is_product_only": False,
        }
    elif pre_skip:
        title_override = {
            "want_type": "MIX",
            "decision": "SERVICE_OR_MIX",
            "next_step": "SKIP",
            "why": f"Pre-detail skip ({pre_skip}): {title}",
            "is_product_only": False,
        }
    elif decision == "UNCLEAR" or not parsed.get("title_used"):
        title_override = title_signal_decision(title)
    if title_override:
        decision = title_override["decision"]
        next_step = title_override["next_step"]
        parsed = {
            **parsed,
            "want_type": title_override.get("want_type", parsed.get("want_type")),
            "decision": decision,
            "next_step": next_step,
            "why": title_override["why"],
            "title_used": True,
            "title_override_applied": True,
        }
        if "agency_is_buying" in title_override:
            parsed["agency_is_buying"] = title_override["agency_is_buying"]
        if "is_product_only" in title_override:
            parsed["is_product_only"] = title_override["is_product_only"]
        if title_override.get("goods_supply_salvage"):
            parsed["goods_supply_salvage"] = True

    # False-MIX salvage: model/labels said MIX but body is goods (+ delivery OK)
    if decision == "SERVICE_OR_MIX" or str(parsed.get("want_type") or "").upper() in {
        "MIX",
        "SERVICE",
    }:
        salvage = goods_supply_salvage(opportunity)
        if salvage:
            decision = salvage["decision"]
            next_step = salvage["next_step"]
            parsed = {
                **parsed,
                "want_type": salvage["want_type"],
                "decision": decision,
                "next_step": next_step,
                "why": salvage["why"],
                "agency_is_buying": True,
                "is_product_only": True,
                "title_used": True,
                "title_override_applied": True,
                "goods_supply_salvage": True,
            }

    # Fail-closed AFTER salvage: PRODUCT label + service/PBM evidence → SKIP.
    # Wrong PRODUCT is as bad as no info.
    contradiction = triage_product_service_contradiction_reason(
        {
            "want_type": parsed.get("want_type"),
            "decision": decision,
            "what_they_want": parsed.get("what_they_want"),
            "parsed": parsed,
        },
        opportunity=opportunity,
    )
    if contradiction:
        decision = "SERVICE_OR_MIX"
        next_step = "SKIP"
        parsed = {
            **parsed,
            "want_type": "SERVICE",
            "decision": decision,
            "next_step": next_step,
            "why": contradiction,
            "is_product_only": False,
            "service_product_contradiction": True,
            "title_override_applied": True,
        }

    docs_gate = authorize_document_fetch(
        {
            "ok": True,
            "want_type": parsed.get("want_type"),
            "decision": decision,
            "next_step": next_step,
            "parsed": parsed,
            "what_they_want": parsed.get("what_they_want"),
        },
        opportunity=opportunity,
    )

    return {
        "ok": True,
        "status": "OK",
        "nb_id": nb_id,
        "title": title,
        "decision": decision,
        "next_step": next_step,
        "posted_date": parsed.get("posted_date"),
        "agency": parsed.get("agency"),
        "want_type": parsed.get("want_type"),
        "what_they_want": parsed.get("what_they_want"),
        "quantity": parsed.get("quantity"),
        "close_date": parsed.get("close_date"),
        "parsed": parsed,
        "raw_text": last_text,
        "live_ai_invoked": True,
        "mocked": False,
        "model": model,
        "openai_spent_usd": float(spent),
        "input_tokens": sum(a["input_tokens"] for a in attempts),
        "output_tokens": sum(a["output_tokens"] for a in attempts),
        "response_id": getattr(last_resp, "id", None),
        "attempts": attempts,
        "title_override_applied": bool(title_override),
        "docs_fetch_authorized": bool(docs_gate.get("authorized")),
        "docs_fetch_gate": docs_gate,
        "opportunity_field_count": len(opportunity.keys()),
        "opportunity_json_chars": len(opportunity_corpus(opportunity)),
        "analyzed_at": _utc(),
        "goods_supply_salvage": bool(parsed.get("goods_supply_salvage")),
        "checklist_version": "owner_posted_agency_want_qty_close_v4_false_mix_salvage",
    }


MIX_SEPARABLE_SYSTEM = (
    "You are M3 Supply's MIX solicitation analyst. "
    "M3 is a goods supplier — we can sell tangible products/materials, but we do NOT "
    "perform installation, construction, maintenance, repair, labor, or ongoing service. "
    "Read the COMPLETE opportunity JSON. "
    "Decide whether there are separable product/goods line items M3 could bid on "
    "WITHOUT being required to provide service/install/labor. "
    "If yes → separable_goods_bid_possible=true, next_step=INVESTIGATE_FURTHER. "
    "If every award path requires service/install/labor/construction as a condition of "
    "supplying the goods → separable_goods_bid_possible=false, next_step=DELETE. "
    "Cancelled / no-bids / advertisement-only notices → DELETE. "
    "Never invent line items. Quote short verbatim snippets. "
    "Return one valid JSON object only."
)

MIX_SEPARABLE_SCHEMA = {
    "nb_id": "string",
    "title": "string",
    "separable_goods_bid_possible": "boolean",
    "product_line_items_we_could_bid": ["string — goods only, no service"],
    "service_or_install_required_for_award": "boolean",
    "why": "string",
    "source_snippets": ["string"],
    "next_step": "INVESTIGATE_FURTHER|DELETE",
}


def triage_mix_separable_goods(
    *,
    nb_id: str,
    opportunity: dict[str, Any],
    client: Any | None = None,
    model: str | None = None,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """For MIX notices: can M3 bid goods without providing the service?"""
    from openai import OpenAI

    settings = get_settings()
    model = model or settings.openai_default_model or MODEL_DEFAULT
    if client is None:
        api_key = (settings.openai_api_key or "").strip()
        if not api_key:
            return {
                "ok": False,
                "status": "ERROR",
                "error": "OPENAI_API_KEY missing",
                "nb_id": nb_id,
                "next_step": "DELETE",
                "separable_goods_bid_possible": False,
                "live_ai_invoked": False,
                "openai_spent_usd": 0.0,
            }
        client = OpenAI(api_key=api_key)

    title = str(opportunity.get("title") or "")
    user = (
        f"NB_ID: {nb_id}\n"
        f"TITLE: {title}\n\n"
        "This solicitation was previously labeled MIX (goods + service/install/labor). "
        "Answer from the COMPLETE opportunity JSON only:\n"
        "1) Are there tangible product/material line items?\n"
        "2) Can a vendor bid/supply ONLY those goods without also performing "
        "install, construction, maintenance, repair, or other labor/service?\n"
        "3) If goods-only bid is allowed or clearly separable → "
        "separable_goods_bid_possible=true, next_step=INVESTIGATE_FURTHER, "
        "list product_line_items_we_could_bid.\n"
        "4) If award requires bundled service/install/labor, or notice is cancelled / "
        "no-bids / advertisement-only → separable_goods_bid_possible=false, "
        "next_step=DELETE.\n"
        f"JSON shape: {json.dumps(MIX_SEPARABLE_SCHEMA)}\n\n"
        f"COMPLETE_OPPORTUNITY_JSON:\n{opportunity_corpus(opportunity)}"
    )

    spent = Decimal("0")
    attempts: list[dict[str, Any]] = []
    parsed: dict[str, Any] | None = None
    last_text = ""
    last_resp = None
    last_usage = {"input_tokens": 0, "output_tokens": 0}

    for attempt in range(1, max_attempts + 1):
        prompt = user
        if attempt > 1:
            prompt = (
                "Previous reply was INVALID JSON. Return ONE valid JSON object only.\n\n"
                f"Previous invalid reply:\n{last_text[:2000]}\n\n{user}"
            )
        resp, text, usage, cost = _call_model(client, model, MIX_SEPARABLE_SYSTEM, prompt)
        spent += cost
        last_text, last_resp, last_usage = text, resp, usage
        ok, obj, err = require_json_object(text)
        attempts.append(
            {
                "attempt": attempt,
                "parse_ok": ok,
                "parse_error": err,
                "response_id": getattr(resp, "id", None),
                "openai_spent_usd": float(cost),
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
            }
        )
        if ok and obj is not None:
            parsed = obj
            break

    if parsed is None:
        return {
            "ok": False,
            "status": "PARSE_ERROR",
            "error": "model_returned_invalid_json_after_retry",
            "nb_id": nb_id,
            "title": title,
            "separable_goods_bid_possible": False,
            "next_step": "DELETE",
            "live_ai_invoked": True,
            "openai_spent_usd": float(spent),
            "raw_text": last_text,
            "attempts": attempts,
            "analyzed_at": _utc(),
            "checklist_version": "owner_mix_separable_goods_v1",
        }

    # Fail closed on ambiguous / cancelled signals in title
    title_l = title.lower()
    forced_delete = None
    if "[cancelled]" in title_l or "advertisement only" in title_l or "no bids" in title_l:
        forced_delete = "title_cancelled_or_no_bids"
    separable = bool(parsed.get("separable_goods_bid_possible"))
    next_step = str(parsed.get("next_step") or "").upper()
    if forced_delete:
        separable = False
        next_step = "DELETE"
        parsed = {
            **parsed,
            "separable_goods_bid_possible": False,
            "next_step": "DELETE",
            "why": f"{forced_delete}; {parsed.get('why') or ''}".strip(),
            "forced_delete": forced_delete,
        }
    elif next_step not in {"INVESTIGATE_FURTHER", "DELETE"}:
        next_step = "INVESTIGATE_FURTHER" if separable else "DELETE"
        parsed["next_step"] = next_step
    elif separable and next_step == "DELETE":
        # model contradicted itself — trust separable flag toward investigate
        next_step = "INVESTIGATE_FURTHER"
        parsed["next_step"] = next_step
    elif (not separable) and next_step == "INVESTIGATE_FURTHER":
        next_step = "DELETE"
        parsed["next_step"] = next_step

    return {
        "ok": True,
        "status": "OK",
        "nb_id": nb_id,
        "title": title,
        "separable_goods_bid_possible": separable,
        "next_step": next_step,
        "product_line_items_we_could_bid": parsed.get("product_line_items_we_could_bid") or [],
        "service_or_install_required_for_award": parsed.get("service_or_install_required_for_award"),
        "why": parsed.get("why"),
        "parsed": parsed,
        "raw_text": last_text,
        "live_ai_invoked": True,
        "model": model,
        "openai_spent_usd": float(spent),
        "input_tokens": sum(a["input_tokens"] for a in attempts),
        "output_tokens": sum(a["output_tokens"] for a in attempts),
        "response_id": getattr(last_resp, "id", None),
        "attempts": attempts,
        "analyzed_at": _utc(),
        "checklist_version": "owner_mix_separable_goods_v1",
    }


def triage_mix_separable_parallel(
    items: list[dict[str, Any]],
    *,
    max_workers: int = 10,
) -> list[dict[str, Any]]:
    """items: [{nb_id, opportunity}, ...]"""
    from m3.research.parallel_work import map_parallel

    def _one(row: dict[str, Any]) -> dict[str, Any]:
        return triage_mix_separable_goods(
            nb_id=str(row["nb_id"]),
            opportunity=row["opportunity"],
        )

    return map_parallel(_one, items, max_workers=max_workers, ordered=True)
