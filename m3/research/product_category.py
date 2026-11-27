"""Product category classification for Active/Category column."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from m3.research.free_municipal_adapters import _guess_category

CATEGORY_CHOICES = (
    "furniture",
    "janitorial",
    "it_hardware",
    "electrical",
    "automotive",
    "medical",
    "safety",
    "apparel",
    "appliances",
    "fuel",
    "building_materials",
    "plumbing",
    "fleet_equipment",
    "industrial_equipment",
    "chemicals",
    "office_supplies",
    "general_products",
)

_CATEGORY_SET = set(CATEGORY_CHOICES)

_EXTRA_GUESS = [
    ("plumbing", r"plumb|pipe|valve|fitting|faucet"),
    ("fleet_equipment", r"\b(?:truck|trailer|excavator|dozer|forklift|bus|van|boat|tractor)\b"),
    ("industrial_equipment", r"pump|generator|compressor|motor|chiller|boiler|filtration"),
    ("chemicals", r"chemical|herbicide|salt\b|cement|asphalt|aggregate|rip\s*rap"),
    ("office_supplies", r"office\s+suppl|paper|toner|ink"),
    ("building_materials", r"lumber|aggregate|stone|concrete|asphalt|cement|roofing"),
]


def normalize_product_category(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    if not s or s in {"unknown", "null", "none", "n/a"}:
        return None
    if s in _CATEGORY_SET:
        return s
    # soft aliases
    aliases = {
        "vehicles": "automotive",
        "vehicle": "automotive",
        "construction_materials": "building_materials",
        "aggregates": "building_materials",
        "equipment": "industrial_equipment",
        "heavy_equipment": "fleet_equipment",
        "tools": "industrial_equipment",
    }
    return aliases.get(s)


def guess_product_category(*texts: Any) -> str | None:
    blob = " ".join(str(t or "") for t in texts)
    hit = _guess_category(blob)
    if hit:
        return normalize_product_category(hit)
    low = blob.lower()
    for name, pat in _EXTRA_GUESS:
        if re.search(pat, low, re.I):
            return name
    return None


def classify_product_category_openai(
    *,
    title: str,
    what_they_want: str | None = None,
    quantity: str | None = None,
) -> tuple[str | None, Decimal]:
    """Cheap one-shot category classify. Returns (category|None, spend_usd)."""
    from openai import OpenAI

    from m3.config import get_settings
    from m3.research.every_word_doc_read import _ask_json
    from m3.research.openai_client import openai_ready

    ok, why = openai_ready()
    if not ok:
        return guess_product_category(title, what_they_want), Decimal("0")
    settings = get_settings()
    client = OpenAI(api_key=settings.openai_api_key)
    model = "gpt-4o-mini"
    prompt = (
        "Classify this government goods solicitation into exactly one product_category.\n"
        f"Allowed values: {', '.join(CATEGORY_CHOICES)}.\n"
        "Return JSON only: {\"product_category\": \"...\", \"why\": \"short\"}.\n\n"
        f"TITLE: {title}\n"
        f"WHAT_THEY_WANT: {what_they_want or ''}\n"
        f"QUANTITY: {quantity or ''}\n"
    )
    parsed, cost = _ask_json(client, model, prompt)
    cat = normalize_product_category(parsed.get("product_category"))
    if cat:
        return cat, cost
    return guess_product_category(title, what_they_want), cost
