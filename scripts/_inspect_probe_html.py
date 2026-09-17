"""Inspect BidNet / Boston / Chicago HTML structure for parser design."""
from __future__ import annotations

import re
from pathlib import Path

ART = Path(__file__).resolve().parents[1] / "artifacts"


def inspect(name: str) -> None:
    p = ART / f"_probe_{name}.html"
    if not p.exists():
        print(f"{name}: MISSING")
        return
    t = p.read_text(encoding="utf-8", errors="ignore")
    print(f"\n=== {name} len={len(t)} ===")
    for pat in (
        "solicitation-title",
        "openBids",
        "solicitationNumber",
        "closingDate",
        "Due Date",
        "bid-list",
        "opportunity",
        "data-id",
        "project-title",
    ):
        print(f"  {pat}: {len(re.findall(re.escape(pat), t, re.I))}")
    titles = re.findall(
        r'href="(/[^"]*(?:solicitations|bids|opportunity)[^"]*)"[^>]*>([^<]{8,200})</a>',
        t,
        re.I,
    )
    print(f"  link_titles={len(titles)}")
    for href, title in titles[:6]:
        print(f"    - {title.strip()[:70]} | {href[:70]}")
    # BidNet card blocks
    cards = re.findall(
        r'class="[^"]*(?:solicitation|bid-card|result-item|list-item)[^"]*"',
        t,
        re.I,
    )
    print(f"  card_classes={len(cards)} sample={cards[:5]}")
    # Look for JSON embeds
    for m in re.finditer(r'<script[^>]*>([^<]{0,20}(?:window\.|var |\{)[^<]{50,500})</script>', t, re.I):
        snippet = m.group(1)[:120].replace("\n", " ")
        if any(k in snippet.lower() for k in ("solicitation", "bid", "opportunity", "project")):
            print(f"  script_hint: {snippet}")
            break


def main() -> None:
    for n in (
        "bidnet_il",
        "bidnet_il_open",
        "bidnet_fl_open",
        "boston_bids",
        "boston",
        "chicago",
        "pp_wy",
        "seattle",
        "seattle_og",
    ):
        inspect(n)


if __name__ == "__main__":
    main()
