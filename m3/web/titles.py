"""Readable opportunity display titles — never invent agency or product identity."""

from __future__ import annotations

import re
from typing import Any

# Solicitation / RFQ number fragments to strip from display titles
_SOL_NUM = re.compile(
    r"(?i)\b(?:"
    r"RFB|RFP|RFQ|IFB|ITB|RFI|SOQ|BID|SOL(?:ICITATION)?"
    r")[-_/\s]*[A-Z0-9][-A-Z0-9/]*(?:\s*[-–—]\s*)?"
)
_PURE_SOL = re.compile(r"(?i)^(?:RFB|RFP|RFQ|IFB|ITB)[-A-Z0-9/]+$")
_CLOSING = re.compile(
    r"(?i)\s*[-–—]?\s*Closing\s*:\s*.+$"
)
_DATE_TAIL = re.compile(
    r"(?i)\s*[-–—]?\s*\d{1,2}/\d{1,2}/\d{2,4}(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM)?)?\s*$"
)
_MULTI_SPACE = re.compile(r"\s+")
_ALL_CAPS_WORD = re.compile(r"^[A-Z0-9][A-Z0-9\-/&,.]{2,}$")
_PRESERVE = re.compile(
    r"(?i)^(USA|US|CA|NY|NE|IA|KS|MD|DE|LA|DWP|DAS|PPE|HVAC|MRO|LED|USB|"
    r"APX|XPR|CAT|JD|IFB|RFB|RFP|RFQ|ITB|SKU|MPN|LLC|INC|CO|LTD)$"
)


def _title_case_word(word: str) -> str:
    if _PRESERVE.match(word):
        return word.upper() if len(word) <= 4 and word.isalpha() else word
    if re.search(r"\d", word) or "/" in word or "-" in word and any(c.isdigit() for c in word):
        return word  # part numbers / model codes
    if _ALL_CAPS_WORD.match(word) and len(word) > 3:
        return word.capitalize()
    if word.isupper() and len(word) > 3:
        return word.capitalize()
    return word


def readable_product_phrase(raw: str) -> str:
    text = _MULTI_SPACE.sub(" ", (raw or "").strip(" -–—|"))
    if not text:
        return ""
    # Soften ALL CAPS sentences but keep short tokens / part numbers
    if text.isupper() and len(text) > 12:
        parts = [_title_case_word(w) for w in text.split(" ")]
        text = " ".join(parts)
    return text.strip(" -–—")


_ATTACHMENT_JUNK = re.compile(
    r"(?is)\s*(?:Original|Attachments?)\s*:\s*.*$"
)
_SOURCE_KEY_PREFIX = re.compile(
    r"(?i)^\s*[a-z][a-z0-9_]{2,40}\s*[-–—]\s*"
)
_DOTD_PREFIX = re.compile(r"(?i)^\s*\*?RE-?BID\*?\s*")


def extract_product_from_official_title(title: str, solicitation_number: str | None = None) -> str:
    """Strip solicitation numbers and closing-date clutter from official title."""
    text = (title or "").strip()
    if not text:
        return ""
    text = _ATTACHMENT_JUNK.sub("", text)
    text = _SOURCE_KEY_PREFIX.sub("", text)
    text = _DOTD_PREFIX.sub("", text)
    text = _CLOSING.sub("", text)
    text = _DATE_TAIL.sub("", text)
    if solicitation_number:
        sol = re.escape(solicitation_number.strip())
        text = re.sub(rf"(?i)\b{sol}\b", " ", text)
        text = re.sub(rf"(?i){sol}\s*[-–—]\s*", " ", text)
    # Remove leading solicitation tokens like "RFB-IS-27200335 - "
    text = _SOL_NUM.sub(" ", text, count=3)
    # Drop leading numeric job ids like "1095677-"
    text = re.sub(r"^\s*\d{5,}\s*[-–—:]\s*", "", text)
    text = re.sub(r"^\s*[-–—:]+\s*", "", text)
    text = _MULTI_SPACE.sub(" ", text).strip(" -–—")
    # If we stripped everything, fall back to original cleaned string
    if len(text) < 3:
        text = _MULTI_SPACE.sub(" ", (title or "").strip())
        text = _ATTACHMENT_JUNK.sub("", text)
        text = _CLOSING.sub("", text).strip()
    return readable_product_phrase(text)


_USELESS_QTY = re.compile(
    r"(?i)^("
    r"unknown|n/?a|none|null|tbd|not\s+specified\.?|see\s+(?:bid|docs?)|"
    r"approximate(?:\s+and\s+not\s+guaranteed)?.*|"
    r"university\s+reserves.*|"
    r"various\s+quantit(?:y|ies).*|"
    r"(?:total\s+)?estimated\s+quantit(?:y|ies).*"
    r")\s*$"
)


def _humanize_qty_key(key: str) -> str:
    s = re.sub(r"[_]+", " ", str(key or "")).strip()
    s = re.sub(r"(?i)^(product|item|line)\s*\d+\s*", "", s).strip()
    return readable_product_phrase(s) or s


def normalize_quantity_for_title(quantity: Any) -> tuple[str | None, bool]:
    """Return (qty_text, is_multi_parts) for Active titles.

    Examples:
      "500 EA" -> ("500 EA", False)
      {"APC_Smart_UPS": 10, "APC_Smart_RM_UPS": 10} -> ("10 APC Smart UPS + 10 APC Smart RM UPS", True)
      [{"description": "Ibuprofen", "quantity": 250}, ...] -> ("Multiple Parts Listing", True)
    """
    if quantity is None:
        return None, False
    if isinstance(quantity, (int, float)) and not isinstance(quantity, bool):
        if float(quantity) <= 0:
            return None, False
        if float(quantity).is_integer():
            return str(int(quantity)), False
        return str(quantity), False
    if isinstance(quantity, dict):
        mats = quantity.get("materials") if isinstance(quantity.get("materials"), list) else None
        if mats and len(mats) >= 2:
            return "Multiple Parts Listing", True
        pairs: list[str] = []
        for k, v in quantity.items():
            if isinstance(v, (list, dict)) or str(k) == "materials":
                continue
            label = _humanize_qty_key(str(k))
            num = None
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                num = str(int(v)) if float(v).is_integer() else str(v)
            elif isinstance(v, str) and re.search(r"\d", v):
                num = readable_product_phrase(v)
            if not num:
                continue
            # Keys like product_1_quantity → keep the number only
            if re.search(r"(?i)(product|item|line)[_\s]*\d+[_\s]*quantity|^quantity$|^qty$", str(k)):
                pairs.append(num)
            elif label and not re.search(r"(?i)^(quantity|qty)$", label):
                pairs.append(f"{num} {label}")
            else:
                pairs.append(num)
        if len(pairs) >= 3:
            return "Multiple Parts Listing", True
        if len(pairs) == 2:
            return " + ".join(pairs), True
        if len(pairs) == 1:
            return pairs[0], False
        return None, False
    if isinstance(quantity, list):
        if not quantity:
            return None, False
        if len(quantity) >= 2:
            return "Multiple Parts Listing", True
        only = quantity[0]
        if isinstance(only, dict):
            label = (
                only.get("description")
                or only.get("item")
                or only.get("material")
                or only.get("name")
                or ""
            )
            num = only.get("quantity") or only.get("qty") or only.get("quantity_tons")
            label_s = readable_product_phrase(str(label))
            if isinstance(num, (int, float)) and label_s:
                n = str(int(num)) if float(num).is_integer() else str(num)
                return f"{n} {label_s}", False
            if label_s:
                return label_s, False
        return normalize_quantity_for_title(only)
    # string / other
    raw = str(quantity).strip()
    # Preserve schedule totals like "9,062 BG", "9,062 40lb bags", "13,924 EA + 15,670 FT".
    if re.match(
        r"^\d[\d,]*(?:\.\d+)?\s+(?:\d+(?:\.\d+)?\s*)?(?:lb\s*)?[A-Za-z]{1,12}"
        r"(?:\s*\+\s*\d[\d,]*(?:\.\d+)?\s+(?:\d+(?:\.\d+)?\s*)?(?:lb\s*)?[A-Za-z]{1,12})*$",
        raw,
        flags=re.IGNORECASE,
    ):
        return raw[:128], ("+" in raw)
    qty = readable_product_phrase(raw)
    if not qty or _USELESS_QTY.match(qty):
        return None, False
    # JSON-ish list dumped as string
    if qty.startswith("[") or qty.startswith("{"):
        return "Multiple Parts Listing", True
    if len(qty) > 90:
        if re.search(r"(?i)\b(various|multiple|line\s+items|tons)\b", qty):
            return "Multiple Parts Listing", True
        qty = qty[:87] + "..."
    return qty, False


def format_quantity_storage(quantity: Any) -> str | None:
    """Compact string for Opportunity.quantity column (max ~128)."""
    text, multi = normalize_quantity_for_title(quantity)
    if not text:
        return None
    if multi and text == "Multiple Parts Listing":
        return "Multiple Parts Listing"
    return text[:128]


def _product_phrase_after_of(product: str) -> str:
    """Natural mid-sentence product wording after 'of'."""
    text = readable_product_phrase(product or "").strip()
    if not text:
        return ""
    # Prefer common commercial phrasing when the packet says Softener Salt.
    if re.match(r"(?i)^water\s+softener\s+salt\b", text):
        return "water softening salt"
    if text[0].isupper() and not text.isupper():
        return text[0].lower() + text[1:]
    return text


def build_product_list_title(
    *,
    what_they_want: Any = None,
    quantity: Any = None,
    official_title: str | None = None,
    solicitation_number: str | None = None,
) -> str:
    """Active/list Title column: qty + product (e.g. '15 3\" ball valves', 'Multiple Parts Listing')."""
    # what_they_want sometimes arrives as a list/dict from model quirks
    if isinstance(what_they_want, (list, dict)):
        what = "Multiple Parts Listing"
    else:
        what = readable_product_phrase(str(what_they_want or "").strip())
        if what.startswith("[") or what.startswith("{"):
            what = "Multiple Parts Listing"
    qty, _multi = normalize_quantity_for_title(quantity)
    product = extract_product_from_official_title(official_title or "", solicitation_number)

    # Prefer concrete product phrase; fall back to cleaned official title.
    product_phrase = what if what and len(what) >= 3 else product
    if not product_phrase:
        if qty:
            return qty[:240]
        return "Untitled"

    # Explicit multi-line packages
    if qty and qty.lower() == "multiple parts listing":
        if product_phrase.lower() == "multiple parts listing":
            return "Multiple Parts Listing"
        return f"Multiple Parts Listing — {product_phrase}"[:240]

    if qty and qty.lower() not in product_phrase.lower():
        # Natural bag/case wording: "9,062 40lb bags of water softening salt"
        if re.search(r"(?i)\b(?:\d+(?:\.\d+)?\s*)?(?:lb\s*)?bags?\b|\bcases?\b", qty):
            return f"{qty} of {_product_phrase_after_of(product_phrase)}"[:240]
        # "15 EA — Ball Valves" / "10 APC Smart UPS + 10 APC Smart RM UPS — UPS Purchase"
        return f"{qty} — {product_phrase}"[:240]
    return product_phrase[:240]


def resolve_buyer_display(buyer_name: str | None, *, source_name: str | None = None) -> str | None:
    buyer = (buyer_name or "").strip()
    if not buyer or buyer.lower() in {"unknown", "n/a", "none"}:
        buyer = (source_name or "").strip()
    if not buyer or buyer.lower() in {"unknown", "n/a", "none"}:
        return None
    # Soften ALL CAPS agency names
    if buyer.isupper() and len(buyer) > 8:
        buyer = " ".join(_title_case_word(w) for w in buyer.split())
    return buyer


def build_display_title(
    *,
    official_title: str | None,
    buyer_name: str | None = None,
    solicitation_number: str | None = None,
    source_name: str | None = None,
) -> dict[str, str]:
    """
    Return display_title + components. Never invent buyer or product.
    Format when both known: '{Buyer} - {Product}'.
    """
    official = (official_title or "").strip() or "Untitled"
    buyer = resolve_buyer_display(buyer_name, source_name=source_name)
    product = extract_product_from_official_title(official, solicitation_number)
    # Avoid "Buyer - Buyer" duplication
    if buyer and product and product.lower() == buyer.lower():
        product = ""
    if buyer and product:
        display = f"{buyer} - {product}"
    elif buyer:
        display = buyer
    elif product:
        display = product
    else:
        display = readable_product_phrase(official) or "Untitled"
    return {
        "display_title": display[:240],
        "official_title": official[:500],
        "buyer_display": buyer or "Unknown",
        "product_display": product or "Not Yet Researched",
    }


def display_title_for_item(item: dict[str, Any]) -> str:
    return build_display_title(
        official_title=item.get("title") or item.get("official_title"),
        buyer_name=item.get("buyer_name"),
        solicitation_number=item.get("solicitation_number"),
        source_name=(item.get("evidence") or {}).get("source_name") if isinstance(item.get("evidence"), dict) else None,
    )["display_title"]
