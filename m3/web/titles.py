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


def extract_product_from_official_title(title: str, solicitation_number: str | None = None) -> str:
    """Strip solicitation numbers and closing-date clutter from official title."""
    text = (title or "").strip()
    if not text:
        return ""
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
        text = _CLOSING.sub("", text).strip()
    return readable_product_phrase(text)


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
