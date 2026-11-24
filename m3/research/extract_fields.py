"""Deterministic field extraction from solicitation text/documents (Build 002I).

Never invents quantities, specs, or deadlines. Returns only evidence-backed fields.

Quantity semantics (002I): distinguish ORDERED_QUANTITY from packaging, capacity,
dimensions, weights, model numbers, and ambiguous parenthetical numbers.
"""

from __future__ import annotations

import re
from typing import Any

# Affirmative ordered-quantity language
ORDERED_QTY_RE = re.compile(
    r"(?i)(?:quantity|qty|estimated\s+quantity|order\s+quantity|bid\s+quantity|"
    r"ordered\s+quantity|line\s*item\s*quantity|purchase\s+quantity)"
    r"\s*(?:of|:|=|-)?\s*([\d,]+(?:\.\d+)?)\s*([a-zA-Z]{0,16})?"
)
FURNISH_QTY_RE = re.compile(
    r"(?i)(?:furnish|provide|supply|purchase|buy|procure)\s+"
    r"(?:of\s+|approximately\s+|approx\.?\s+|up\s+to\s+)?"
    r"([\d,]+)\s+"
    r"(each|ea\.?|units?|pcs?|pieces|sets?|kits?|[a-z]{2,16})\b"
)
# Spelled cardinals used in solicitations (Build 002N: beyond twelve)
_WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "thousand": 1000,
}
_WORD_NUM_ALT = "|".join(sorted(_WORD_NUM.keys(), key=len, reverse=True))

# Government titles: "Five (5) New ..." / "One Hundred Sixty (160) ..." — TITLE + schedule text
WORD_PAREN_QTY_RE = re.compile(
    rf"(?i)\b((?:{_WORD_NUM_ALT})(?:[\s\-]+(?:{_WORD_NUM_ALT})){{0,4}})\s*\(\s*(\d{{1,5}})\s*\)"
)
# Spelled quantity without paren: "twenty-five (new) units" / "fifteen each"
SPELLED_QTY_RE = re.compile(
    rf"(?i)\b((?:{_WORD_NUM_ALT})(?:[\s\-]+(?:{_WORD_NUM_ALT})){{0,4}})\s+"
    rf"(?:\(\s*\d+\s*\)\s*)?(?:new\s+|unused\s+)?"
    rf"(each|ea\.?|units?|pcs?|pieces|sets?|kits?|vehicles?|trucks?|trailers?|"
    rf"pumps?|generators?|pagers?|lifts?|meters?|batteries|compactors?|boats?)\b"
)
# Singular product purchase: "purchase of a boom lift" / "one new trailer"
# Negative lookahead blocks "one hundred/thousand..." compound cardinals.
SINGULAR_PURCHASE_RE = re.compile(
    r"(?i)\b(?:purchase|procure|buy|furnish|provide|supply|acquire)\s+"
    r"(?:of\s+)?(?:a|an|(?:one|1)(?!\s+(?:hundred|thousand|million|\d)))\s+"
    r"(?:new\s+|unused\s+)?([a-z][a-z0-9 \-/]{2,60})"
)
# Reject service/construction titles even if a numeric appears
SERVICE_TITLE_REJECT_RE = re.compile(
    r"(?i)\b(maintenance|repairs?|rehabilitat\w*|car\s*wash|cleaning\s+services?|"
    r"system\s+improvements?|construction|install(?:ation)?|replace\s+\w+\s+coils?|"
    r"catalyst\s+fund|grants?|investment\s+fund|professional\s+services?|"
    r"consulting|design[- ]build)\b"
)
# Spreadsheet / schedule style Quantity columns
SCHEDULE_QTY_RE = re.compile(
    r"(?i)(?:(?:^|\n|;|\t|,)\s*(?:qty|quantity|order\s*qty|bid\s*qty)\s*[:\-/\t ]+\s*([\d,]+(?:\.\d+)?))"
    r"|"
    r"(?:([\d,]+(?:\.\d+)?)\s*(?:each|ea\.?|units?)\s*(?:$|\n|;|\t))"
)
# Reject legal/time boilerplate near word-paren matches in body text
WORD_PAREN_REJECT_FOLLOW = re.compile(
    r"(?i)^\s*(?:calendar\s+)?(?:days?|years?|months?|weeks?|hours?|copies|originals?|"
    r"duplicates?|pages?|business\s+days?|working\s+days?|sets?\s+of\s+documents)"
)
# Explicit "N each of <product>"
N_EACH_OF_RE = re.compile(
    r"(?i)\b([\d,]+)\s*(?:each|ea\.?|units?)\s+(?:of\s+)?([a-z][a-z0-9 \-/]{2,40})"
)
LINE_ITEM_RE = re.compile(
    r"(?i)(?:line\s*(?:item|#|no\.?)|item\s*(?:#|no\.?))\s*[:\s]*([A-Z0-9\-]+).{0,80}?"
    r"(?:qty|quantity)\s*[:\s]*([\d,]+)",
)
# Packaging / per-unit capacity — NOT total ordered quantity
PACKAGE_PER_RE = re.compile(
    r"(?i)\(\s*([\d,]+(?:\.\d+)?)\s*per\s+([a-zA-Z]{1,16})\s*\)"
    r"|"
    r"([\d,]+(?:\.\d+)?)\s*(?:units?|ea\.?|each|pcs?|pieces)?\s*per\s+"
    r"(case|box|cup|bag|drum|pallet|roll|pack|carton|gallon|kit)\b"
)
# Ambiguous parenthetical "(300 UNIT)" / "(250 CUP)" without "per"
PAREN_UNIT_RE = re.compile(
    r"(?i)\(\s*([\d,]+(?:\.\d+)?)\s*([a-zA-Z]{1,16})\s*\)"
)
# Bare "500 EA" without quantity keyword — weak; only accept with purchase context nearby
BARE_EA_RE = re.compile(
    r"(?i)(?<!per\s)([\d,]+(?:\.\d+)?)\s*"
    r"(each|ea\.?|units?|pcs?|pieces|sets?|boxes|cases|kits?)\b"
)

MPN_RE = re.compile(
    r"(?i)(?:mfr\.?\s*part|manufacturer\s*part|mpn|part\s*#|model\s*(?:#|no\.?))\s*[:\s]*([A-Z0-9][A-Z0-9\-/.]{2,})"
)
NSN_RE = re.compile(r"(?i)\bNSN\s*[:\s]*(\d{4}-\d{2}-\d{3}-\d{4})\b")
DEADLINE_RE = re.compile(
    r"(?i)(?:bid\s*(?:due|opening|deadline)|proposals?\s*due|closing\s*date|due\s*date)"
    r"[^\n:]{0,20}[:\s]+([A-Za-z0-9,/\- ]{6,40})"
)
FIXED_QTY_MARKERS = re.compile(
    r"(?i)\b(firm\s+quantity|fixed\s+quantity|definite\s+quantity|one[\s-]time\s+purchase)\b"
)
ESTIMATED_QTY_MARKERS = re.compile(
    r"(?i)\b("
    r"estimated\s+quantit(?:y|ies)|quantity\s+estimated|"
    r"approximately\s+[\d,]+|approx\.?\s+[\d,]+|"
    r"up\s+to\s+[\d,]+|not\s+to\s+exceed\s+[\d,]+|"
    r"estimated\s+(?:annual|total)?\s*usage"
    r")\b"
)
IDIQ_MARKERS = re.compile(
    r"(?i)\b(indefinite\s+quantity|idiq|requirements\s+contract|blanket\s+purchase\s+agreement|"
    r"purchase\s+as[\s-]needed|ordered\s+as[\s-]needed|quantit(?:y|ies)\s+as[\s-]needed)\b"
)

# Spec/weight/dimension/time/electrical units are NOT purchase quantities
REJECT_QTY_UNITS = {
    "lb",
    "lbs",
    "pound",
    "pounds",
    "gvwr",
    "kg",
    "kilogram",
    "kilograms",
    "mhz",
    "ghz",
    "vac",
    "vdc",
    "volt",
    "volts",
    "kv",
    "kilovolt",
    "kilovolts",
    "amp",
    "amps",
    "psi",
    "psig",
    "btu",
    "hp",
    "kw",
    "inch",
    "inches",
    "ft",
    "foot",
    "feet",
    "mm",
    "cm",
    "meter",
    "meters",
    "year",
    "years",
    "yr",
    "yrs",
    "month",
    "months",
    "day",
    "days",
    "hour",
    "hours",
    "hr",
    "hrs",
    "watt",
    "watts",
    "va",
    "kva",
}

# Numbers that are electrical ratings / service windows, not counts
NON_QUANTITY_CONTEXT_RE = re.compile(
    r"(?i)("
    r"\d+\s*k\s*v\b|\d+\s*kv\b|\d+\s*kilovolt|"
    r"\d+\s*hours?\s*(?:per\s*day|/|/?\s*7|technical\s+support|telephone)|"
    r"\d+\s*hour\s*(?:technical\s+)?support|"
    r"24\s*/\s*7|24x7|"
    r"\$\s*[\d,]+|\d+\s*(?:million|billion)\b|"
    r"option\s*(?:year|period|term)s?|renewal\s+options?"
    r")"
)
PACKAGE_UNITS = {
    "case",
    "cases",
    "box",
    "boxes",
    "cup",
    "cups",
    "bag",
    "bags",
    "drum",
    "drums",
    "pallet",
    "pallets",
    "pack",
    "packs",
    "carton",
    "cartons",
    "roll",
    "rolls",
}
PURCHASE_LIKE_UNITS = {
    "unit",
    "units",
    "each",
    "ea",
    "pc",
    "pcs",
    "piece",
    "pieces",
    "set",
    "sets",
    "kit",
    "kits",
    "lot",
    "gallon",
    "gallons",
    "ton",
    "tons",
}

# Semantics that may count as defined purchase quantity for Stage C
ORDERED_SEMANTICS = {
    "ORDERED_QUANTITY",
    "LINE_ITEM_QUANTITY",
    "ESTIMATED_QUANTITY",
    "MINIMUM_GUARANTEED_QUANTITY",
    "MAXIMUM_AUTHORIZED_QUANTITY",
}


def _num(s: str) -> float | None:
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def _unit_norm(unit: str | None) -> str:
    return ((unit or "").strip().lower().rstrip("."))


def _parse_spelled_number(words: str) -> float | None:
    """Parse 'twenty-five' / 'one hundred sixty' into a number. No invention beyond map."""
    parts = re.split(r"[\s\-]+", (words or "").strip().lower())
    if not parts or parts == [""]:
        return None
    if any(p not in _WORD_NUM for p in parts):
        return None
    total = 0
    current = 0
    for p in parts:
        val = _WORD_NUM[p]
        if val == 100:
            current = (current or 1) * 100
        elif val == 1000:
            total += (current or 1) * 1000
            current = 0
        else:
            current += val
    total += current
    return float(total) if total > 0 else None


def extract_quantity_fields(text: str, *, title: str = "") -> dict[str, Any]:
    """Extract quantity with semantic class. Empty / non-ordered = not a Stage-C fit basis."""
    blob = f"{title}\n{text or ''}"
    evidence: list[str] = []
    qty_kind = "unknown"
    # Prefer title + first 4k for contract-type markers so vendor-form boilerplate
    # ("as needed" help text) does not override affirmative line quantities.
    marker_blob = f"{title}\n{(text or '')[:4000]}"
    if IDIQ_MARKERS.search(marker_blob) and not ORDERED_QTY_RE.search(blob):
        qty_kind = "indefinite"
        evidence.append("idiq_or_as_needed_language")
    elif ESTIMATED_QTY_MARKERS.search(marker_blob):
        qty_kind = "estimated"
        evidence.append("estimated_quantity_language")
    elif FIXED_QTY_MARKERS.search(marker_blob):
        qty_kind = "fixed"
        evidence.append("fixed_quantity_language")

    candidates: list[dict[str, Any]] = []

    def _add(
        val: float,
        unit: str | None,
        snippet: str,
        semantic: str,
        *,
        ordered: bool,
    ) -> None:
        unit_l = _unit_norm(unit)
        if val is None or val <= 0 or val > 10_000_000:
            return
        if 1900 <= val <= 2100 and not unit_l:
            return
        if unit_l in REJECT_QTY_UNITS:
            return
        # Money / fund language is never an ordered product quantity
        if re.search(r"(?i)\b(million|billion|\$|usd|fund|grant)\b", snippet or ""):
            return
        if re.search(r"(?i)\bgvwr\b", snippet):
            return
        # Voltage / 24-hour support / option years are not product counts
        if NON_QUANTITY_CONTEXT_RE.search(snippet or ""):
            return
        if re.search(r"(?i)\b\d+\s*k\s*v\b|\b\d+kv\b", snippet or ""):
            return
        if title and SERVICE_TITLE_REJECT_RE.search(title) and ordered:
            return
        candidates.append(
            {
                "value": val,
                "unit": (unit or "")[:32] or None,
                "snippet": snippet[:120],
                "semantic": semantic,
                "counts_as_ordered": ordered,
            }
        )

    # 1) Packaging / per-unit — record but never treat as ordered total
    for m in PACKAGE_PER_RE.finditer(blob):
        raw = m.group(1) or m.group(3)
        unit = m.group(2) or m.group(4)
        val = _num(raw or "")
        if val is None:
            continue
        _add(val, unit, m.group(0), "PACKAGE_QUANTITY", ordered=False)

    # 2) Affirmative "Quantity: N"
    for m in ORDERED_QTY_RE.finditer(blob):
        val = _num(m.group(1))
        unit = m.group(2)
        if val is None:
            continue
        sem = "ESTIMATED_QUANTITY" if qty_kind == "estimated" else "ORDERED_QUANTITY"
        _add(val, unit, m.group(0), sem, ordered=True)

    # 3) "furnish/purchase N units" / "purchase of N ..."
    for m in FURNISH_QTY_RE.finditer(blob):
        val = _num(m.group(1))
        unit = m.group(2)
        if val is None:
            continue
        # Reject model years: "Purchase of 2027 Ford ..."
        unit_l = (unit or "").lower()
        if val >= 2000 and val <= 2100 and unit_l in {
            "ford", "chevy", "chevrolet", "gmc", "dodge", "ram", "toyota", "honda",
            "vehicle", "vehicles", "truck", "trucks", "pursuit",
        }:
            continue
        _add(val, unit, m.group(0), "ORDERED_QUANTITY", ordered=True)

    # 3b) "Five (5) / Twenty-Five (25) New ..." — prefer TITLE; also allow bid-schedule excerpts
    # Body legal boilerplate ("one (1) original") rejected via follow-token rules.
    title_and_schedule = f"{title or ''}\n{(text or '')[:8000]}"
    _PRODUCT_CTX = re.compile(
        r"(?i)\b(new|unused|purchase|purchasing|furnish|furnishing|supply|supplied|"
        r"equipment|vehicle|pump|generator|trailer|pager|lift|unit|each|qty|quantity|"
        r"line\s*item|deliver|delivery\s+of)\b"
    )
    for m in WORD_PAREN_QTY_RE.finditer(title_and_schedule):
        spelled = m.group(1)
        val = _num(m.group(2))
        spelled_val = _parse_spelled_number(spelled)
        if val is None:
            continue
        # Prefer paren digits; if spelled disagrees wildly, skip (corruption/OCR)
        if spelled_val is not None and abs(spelled_val - val) > 0 and spelled_val != val:
            # Allow twenty-five (25) style where spelled sums to digits
            if int(spelled_val) != int(val):
                continue
        after = title_and_schedule[m.end() : m.end() + 48]
        before = title_and_schedule[max(0, m.start() - 80) : m.start()]
        if WORD_PAREN_REJECT_FOLLOW.match(after):
            continue
        if val >= 2000 and val <= 2100 and re.search(
            r"(?i)\b(ford|chevy|chevrolet|gmc|dodge|ram|toyota|honda|vehicle|truck|pursuit)\b",
            after or title or "",
        ):
            continue
        # Only count as ordered when in title or near product/schedule language
        # (inspect BEFORE and AFTER — "purchase of twelve (12)" puts cues before digits)
        in_title = bool(title) and m.group(0).lower() in (title or "").lower()
        near_product = bool(_PRODUCT_CTX.search(before) or _PRODUCT_CTX.search(after or m.group(0)))
        if in_title or near_product:
            _add(val, "EA", m.group(0), "ORDERED_QUANTITY", ordered=True)

    # 3b2) "N each of <product>" / bare "500 EA" with purchase context
    for m in N_EACH_OF_RE.finditer(title_and_schedule):
        val = _num(m.group(1))
        if val is None:
            continue
        if 1900 <= val <= 2100:
            continue
        _add(val, "EA", m.group(0), "ORDERED_QUANTITY", ordered=True)
    for m in BARE_EA_RE.finditer(title_and_schedule):
        val = _num(m.group(1))
        unit = m.group(2)
        if val is None:
            continue
        if 1900 <= val <= 2100:
            continue
        # Skip values that are only packaging parentheticals: "(300 UNIT)"
        before = title_and_schedule[max(0, m.start() - 2) : m.start()]
        after = title_and_schedule[m.end() : m.end() + 2]
        if "(" in before and ")" in after:
            continue
        ctx = title_and_schedule[max(0, m.start() - 60) : m.end() + 40]
        if not _PRODUCT_CTX.search(ctx) and not (
            title and m.group(0).lower() in (title or "").lower()
        ):
            continue
        # Title-only bare N UNIT without purchase/qty language stays ambiguous
        if title and m.group(0).lower() in (title or "").lower():
            if not re.search(
                r"(?i)\b(purchase|furnish|supply|qty|quantity|order)\b", title or ""
            ):
                _add(val, unit, m.group(0), "UNKNOWN_NUMERIC_VALUE", ordered=False)
                continue
        _add(val, unit, m.group(0), "ORDERED_QUANTITY", ordered=True)

    # 3c) Spelled quantities without reliable paren: "fifteen units"
    for m in SPELLED_QTY_RE.finditer(title_and_schedule):
        val = _parse_spelled_number(m.group(1))
        if val is None:
            continue
        _add(val, m.group(2), m.group(0), "ORDERED_QUANTITY", ordered=True)

    # 3d) Singular purchase of one tangible item (title only; never service titles)
    if title and not SERVICE_TITLE_REJECT_RE.search(title):
        for m in SINGULAR_PURCHASE_RE.finditer(title or ""):
            noun = (m.group(1) or "").strip()
            if re.search(
                r"(?i)\b(service|services|maintenance|repair|install|construction|"
                r"license|software|hundred|thousand|million)\b",
                noun,
            ):
                continue
            _add(1.0, "EA", m.group(0), "ORDERED_QUANTITY", ordered=True)

    # 4) Line-item qty columns
    line_items: list[dict[str, Any]] = []
    for m in LINE_ITEM_RE.finditer(blob):
        val = _num(m.group(2))
        if val is None:
            continue
        line_items.append(
            {
                "line_ref": m.group(1),
                "quantity": val,
                "snippet": m.group(0)[:160],
                "semantic": "LINE_ITEM_QUANTITY",
            }
        )
        _add(val, None, m.group(0), "LINE_ITEM_QUANTITY", ordered=True)
        if len(line_items) >= 20:
            break

    # 4b) Schedule / spreadsheet-style quantity cells
    for m in SCHEDULE_QTY_RE.finditer(blob):
        raw = m.group(1) or m.group(2)
        val = _num(raw or "")
        if val is None:
            continue
        if 1900 <= val <= 2100:
            continue
        _add(val, "EA", m.group(0), "LINE_ITEM_QUANTITY", ordered=True)

    # 5) Ambiguous parenthetical "(300 UNIT)" — NOT ordered without more evidence
    # Exception: title-only "(N units)" on a product purchase title is ordered.
    for m in PAREN_UNIT_RE.finditer(blob):
        # Skip if already classified as PACKAGE via "per"
        if re.search(r"(?i)\bper\b", m.group(0)):
            continue
        val = _num(m.group(1))
        unit = m.group(2)
        unit_l = _unit_norm(unit)
        if val is None:
            continue
        in_title = bool(title) and m.group(0).lower() in (title or "").lower()
        title_product = in_title and not SERVICE_TITLE_REJECT_RE.search(title or "")
        # Bare "(N UNIT)" is packaging/capacity-ambiguous unless affirmative order language
        # also appears (e.g. "Purchase of 300 units" / "Three Hundred (300) Units").
        affirmative = bool(
            re.search(
                r"(?i)\b(purchase\s+of|furnish(?:ing)?|buy\b|ordered?\b|"
                r"qty\.?\s*:|quantity\s*:)\b",
                title or blob,
            )
            or WORD_PAREN_QTY_RE.search(title or "")
        )
        if unit_l in PACKAGE_UNITS and not (title_product and affirmative):
            _add(val, unit, m.group(0), "PACKAGE_QUANTITY", ordered=False)
        elif title_product and affirmative and unit_l in PURCHASE_LIKE_UNITS | {"units", "unit"}:
            _add(val, unit, m.group(0), "ORDERED_QUANTITY", ordered=True)
        elif unit_l in PURCHASE_LIKE_UNITS | {"units", "unit"}:
            _add(val, unit, m.group(0), "UNKNOWN_NUMERIC_VALUE", ordered=False)
        else:
            _add(val, unit, m.group(0), "UNKNOWN_NUMERIC_VALUE", ordered=False)

    # Multi-line product schedules: "two (2) Reach Trucks, one (1) Sit-Down..., four (4) chargers"
    # Do not collapse distinct product lines into a single scalar quantity.
    # Do not duplicate the same requirement when title is echoed in body text.
    multi_line_raw: list[dict[str, Any]] = []
    for m in WORD_PAREN_QTY_RE.finditer(blob):
        words, digits = m.group(1), m.group(2)
        parsed = _parse_spelled_number(words)
        val = _num(digits)
        if parsed is None or val is None or abs(parsed - val) > 0.01:
            continue
        after = blob[m.end() : m.end() + 80]
        if WORD_PAREN_REJECT_FOLLOW.match(after):
            continue
        if NON_QUANTITY_CONTEXT_RE.search(m.group(0) + after):
            continue
        # Capture following product noun phrase (allow "with <accessory>" package)
        noun_m = re.match(
            r"(?i)\s*(?:new\s+|unused\s+)?"
            r"([A-Za-z][A-Za-z0-9 \-/,&]{2,80}?)(?=,|\band\b(?!\s+transport)|\.|$|for\s+(?:the\s+)?(?:Saratoga|region|Lottery))",
            after,
        )
        product = (noun_m.group(1).strip() if noun_m else after.strip()[:60])
        product = re.sub(r"(?i)\s+for\s+(?:the\s+)?\w+.*$", "", product).strip()
        if re.search(
            r"(?i)\b("
            r"service|services|maintenance|repair|hour|hours|day|days|option|"
            r"contracts?|awards?|vendors?|consultants?|bidders?|proposals?|"
            r"will\s+be\s+awarded|to\s+be\s+awarded"
            r")\b",
            product or "",
        ):
            continue
        multi_line_raw.append(
            {
                "quantity": str(int(val)) if val == int(val) else str(val),
                "unit": "EA",
                "product_description": product,
                "snippet": (m.group(0) + " " + product)[:160],
                "semantic": "LINE_ITEM_QUANTITY",
                "counts_as_ordered": True,
                "span_start": m.start(),
                "span_end": m.end() + len(product),
                "source_offset": m.start(),
            }
        )
        if len(multi_line_raw) >= 20:
            break

    # Companion bare "(N) product" after word-paren lists: "and (4) compatible ... Chargers"
    if multi_line_raw:
        for m in re.finditer(
            r"(?i)(?:and|,)\s*\(\s*(\d{1,5})\s*\)\s+"
            r"((?:compatible\s+|new\s+|unused\s+)?[A-Za-z][A-Za-z0-9 \-/]{2,60}?)(?=,|\.|$|for\s+each|to\s+be)",
            blob,
        ):
            val = _num(m.group(1))
            product = (m.group(2) or "").strip()
            if val is None or not product:
                continue
            if re.search(
                r"(?i)\b("
                r"hour|hours|day|days|option|service|services|"
                r"contracts?|awards?|vendors?|consultants?|bidders?"
                r")\b",
                product,
            ):
                continue
            multi_line_raw.append(
                {
                    "quantity": str(int(val)) if val == int(val) else str(val),
                    "unit": "EA",
                    "product_description": product,
                    "snippet": m.group(0)[:160],
                    "semantic": "LINE_ITEM_QUANTITY",
                    "counts_as_ordered": True,
                    "span_start": m.start(),
                    "span_end": m.end(),
                    "source_offset": m.start(),
                }
            )

    def _norm_item_identity(li: dict[str, Any]) -> tuple[str, str]:
        prod = re.sub(r"\s+", " ", str(li.get("product_description") or "").lower()).strip()
        prod = re.sub(r"[^a-z0-9 ]+", "", prod)[:80]
        return (str(li.get("quantity") or ""), prod)

    def _dedupe_extracted_lines(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop title/body echoes of the same requirement; keep distinct products.

        Identical (qty, normalized description) without distinct line_ref = same
        procurement requirement repeated in the text (e.g. title echoed in body).
        Genuinely separate schedule rows should differ by description, quantity,
        or an explicit line_ref.
        """
        out: list[dict[str, Any]] = []
        seen: dict[tuple[str, str], dict[str, Any]] = {}
        for li in items:
            ident = _norm_item_identity(li)
            if not ident[1]:
                continue
            prev = seen.get(ident)
            if prev is None:
                seen[ident] = li
                out.append(li)
                continue
            # Same identity: keep earliest span (usually title), skip echo
            if li.get("line_ref") and li.get("line_ref") != prev.get("line_ref"):
                # Distinct schedule line numbers — retain both
                out.append(li)
                continue
            # Overlapping/echo duplicate — skip
            continue
        return out

    distinct_lines = _dedupe_extracted_lines(multi_line_raw)

    # Prefer ordered candidates
    ordered = [c for c in candidates if c.get("counts_as_ordered")]
    primary = ordered[0] if ordered else None
    semantic = primary["semantic"] if primary else (
        candidates[0]["semantic"] if candidates else None
    )

    if primary and qty_kind == "unknown":
        if primary["semantic"] == "ESTIMATED_QUANTITY":
            qty_kind = "estimated"
        else:
            qty_kind = "fixed"
        evidence.append("affirmative_ordered_quantity_language")
    elif primary and primary["semantic"] in {"ORDERED_QUANTITY", "LINE_ITEM_QUANTITY"}:
        # Firm ordered extraction wins over distant estimated boilerplate
        qty_kind = "fixed"
        evidence.append("firm_ordered_overrides_estimated_boilerplate")
    elif candidates and not primary:
        evidence.append(f"non_ordered_numeric:{candidates[0]['semantic']}")

    # One unique equipment package (e.g. boom lift with trailer) → single ordered qty
    if len(distinct_lines) == 1:
        only = distinct_lines[0]
        evidence.append("single_requirement_after_dedupe")
        evidence.append(f"source_offset:{only.get('source_offset')}")
        return {
            "quantity": only["quantity"],
            "quantity_unit": only.get("unit") or "EA",
            "quantity_kind": "fixed",
            "quantity_semantic": "ORDERED_QUANTITY",
            "quantity_status": "VERIFIED",
            "quantity_evidence": evidence + [only.get("snippet")],
            "quantity_candidates": candidates[:8],
            "line_items": [
                {
                    "line_ref": only.get("line_ref"),
                    "quantity": only["quantity"],
                    "product_description": only.get("product_description"),
                    "snippet": only.get("snippet"),
                    "semantic": "ORDERED_QUANTITY",
                    "span_start": only.get("span_start"),
                    "span_end": only.get("span_end"),
                    "source_offset": only.get("source_offset"),
                }
            ],
            "quantity_extracted": True,
            "quantity_is_ordered": True,
            "quantity_line_count": 1,
            "verified_line_items": [only],
        }

    # Distinct product lines → do not report a single misleading scalar
    if len(distinct_lines) >= 2:
        evidence.append(f"multi_line_products:{len(distinct_lines)}")
        for li in distinct_lines:
            if not any(
                _norm_item_identity(x) == _norm_item_identity(li) for x in line_items
            ):
                line_items.append(
                    {
                        "line_ref": li.get("line_ref"),
                        "quantity": li["quantity"],
                        "product_description": li["product_description"],
                        "snippet": li["snippet"],
                        "semantic": "LINE_ITEM_QUANTITY",
                        "span_start": li.get("span_start"),
                        "span_end": li.get("span_end"),
                        "source_offset": li.get("source_offset"),
                    }
                )
        result = {
            "quantity": None,
            "quantity_unit": None,
            "quantity_kind": "fixed",
            "quantity_semantic": "MULTI_LINE_ITEM",
            "quantity_status": "MULTI_LINE_ITEM",
            "quantity_evidence": evidence + [li["snippet"] for li in distinct_lines[:6]],
            "quantity_candidates": candidates[:8],
            "line_items": line_items[:20],
            "quantity_extracted": True,
            "quantity_is_ordered": True,
            "quantity_line_count": len(distinct_lines),
            "verified_line_items": distinct_lines,
        }
        return result

    result: dict[str, Any] = {
        "quantity": (
            str(int(primary["value"]))
            if primary and primary["value"] == int(primary["value"])
            else (str(primary["value"]) if primary else None)
        ),
        "quantity_unit": primary.get("unit") if primary else None,
        "quantity_kind": qty_kind if primary or qty_kind != "unknown" else "unknown",
        "quantity_semantic": semantic,
        "quantity_status": (
            "VERIFIED"
            if primary
            else ("AMBIGUOUS" if candidates else "NOT_FOUND_IN_RETAINED_EVIDENCE")
        ),
        "quantity_evidence": evidence + ([primary["snippet"]] if primary else []),
        "quantity_candidates": candidates[:8],
        "line_items": line_items[:20],
        "quantity_extracted": bool(primary),
        "quantity_is_ordered": bool(primary),
    }
    # Surface first non-ordered candidate for investigation (never as quantity field)
    if not primary and candidates:
        result["ambiguous_quantity_candidates"] = candidates[:5]
        result["quantity_evidence"] = evidence + [candidates[0]["snippet"]]
        result["quantity_status"] = "AMBIGUOUS"
    if not primary and not candidates:
        result["quantity_status"] = "NOT_FOUND_IN_RETAINED_EVIDENCE"
    return result


def extract_identity_fields(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    m = MPN_RE.search(text or "")
    if m:
        out["manufacturer_part_number"] = m.group(1)[:64]
        out["mpn_evidence"] = m.group(0)[:100]
    m = NSN_RE.search(text or "")
    if m:
        out["nsn"] = m.group(1)
        out["nsn_evidence"] = m.group(0)[:80]
    m = DEADLINE_RE.search(text or "")
    if m:
        out["deadline_extracted"] = m.group(1).strip()[:80]
        out["deadline_evidence"] = m.group(0)[:120]
    return out


def enrich_item_from_text(item: dict[str, Any], text: str, *, source: str = "document") -> dict[str, Any]:
    """Return copy of item with extracted fields when missing. Does not overwrite verified values."""
    out = dict(item)
    qty = extract_quantity_fields(text, title=str(out.get("title") or ""))
    ident = extract_identity_fields(text)
    if qty.get("quantity_semantic") == "MULTI_LINE_ITEM" and qty.get("quantity_extracted"):
        out["quantity"] = None  # never collapse multi-line into one scalar
        out["quantity_unit"] = None
        out["quantity_kind"] = qty.get("quantity_kind") or "fixed"
        out["quantity_semantic"] = "MULTI_LINE_ITEM"
        out["quantity_status"] = "MULTI_LINE_ITEM"
        out["quantity_evidence"] = list(qty.get("quantity_evidence") or []) + [f"source:{source}"]
        out["line_items"] = qty.get("line_items") or out.get("line_items")
        out["verified_line_items"] = qty.get("verified_line_items") or []
        out["quantity_line_count"] = qty.get("quantity_line_count")
    elif qty.get("quantity_extracted") and qty.get("quantity_is_ordered") and not out.get("quantity"):
        out["quantity"] = qty["quantity"]
        out["quantity_unit"] = qty.get("quantity_unit")
        out["quantity_kind"] = qty.get("quantity_kind")
        out["quantity_semantic"] = qty.get("quantity_semantic")
        out["quantity_status"] = qty.get("quantity_status") or "VERIFIED"
        out["quantity_evidence"] = list(qty.get("quantity_evidence") or []) + [f"source:{source}"]
        out["line_items"] = qty.get("line_items") or out.get("line_items")
    else:
        # Preserve investigation trail for packaging / ambiguous numbers — do not set quantity
        if qty.get("ambiguous_quantity_candidates") and not out.get("quantity"):
            out["quantity_semantic"] = qty.get("quantity_semantic") or "UNKNOWN_NUMERIC_VALUE"
            out["quantity_kind"] = out.get("quantity_kind") or "unknown"
            out["quantity_evidence"] = list(qty.get("quantity_evidence") or []) + [
                f"source:{source}",
                "rejected_non_ordered_quantity_semantics",
            ]
            out["ambiguous_quantity_candidates"] = qty.get("ambiguous_quantity_candidates")
        elif qty.get("quantity_kind") in {"indefinite", "estimated"} and not out.get("quantity_kind"):
            out["quantity_kind"] = qty["quantity_kind"]
            out["quantity_evidence"] = qty.get("quantity_evidence")
    for k, v in ident.items():
        if v and not out.get(k):
            out[k] = v
    out["_extraction"] = {
        "quantity_extracted": bool(qty.get("quantity_extracted")),
        "quantity_is_ordered": bool(qty.get("quantity_is_ordered")),
        "quantity_semantic": qty.get("quantity_semantic"),
        "source": source,
        "quantity_kind": out.get("quantity_kind") or qty.get("quantity_kind"),
    }
    return out


def ocr_pdf_bytes(data: bytes, *, max_pages: int = 5) -> dict[str, Any]:
    """OCR fallback for scanned PDFs. Optional deps; never fabricates text."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return {"ok": False, "reason": "pypdfium2_not_installed", "text": ""}
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return {"ok": False, "reason": "pytesseract_or_pillow_not_installed", "text": ""}

    try:
        pdf = pdfium.PdfDocument(data)
        parts: list[str] = []
        n = min(len(pdf), max_pages)
        for i in range(n):
            page = pdf[i]
            # 150 dpi — bounded cost
            bitmap = page.render(scale=150 / 72)
            pil = bitmap.to_pil()
            if pil.mode not in ("RGB", "L"):
                pil = pil.convert("RGB")
            txt = pytesseract.image_to_string(pil) or ""
            if txt.strip():
                parts.append(f"[page {i+1}]\n{txt.strip()}")
        text = "\n\n".join(parts)
        text = re.sub(r"\s+", " ", text).strip()[:20000]
        return {
            "ok": len(text) >= 40,
            "text": text,
            "reason": None if len(text) >= 40 else "ocr_insufficient_text",
            "pages_ocrd": n,
            "method": "pypdfium2+tesseract",
        }
    except Exception as exc:
        return {"ok": False, "reason": f"ocr_error:{type(exc).__name__}", "text": ""}
