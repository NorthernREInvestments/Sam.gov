"""Post-run: rebuild demotions from checkpoint and emit Phase 20 summary."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from live_exact_priced_pdp.models import (
    ACCESS_BLOCKED_DOMAIN,
    BUILD,
    DEAD_404,
    DEMOTIONS,
    EXTRACTABLE_STATES,
    IDENTITY_ONLY,
    LIVE_EXACT_PDP_ACCESS_BLOCKED,
    LIVE_EXACT_PDP_LOGIN_REQUIRED,
    LIVE_EXACT_PDP_NO_PUBLIC_PRICE,
    LIVE_EXACT_PDP_QUOTE_ONLY,
    NON_PRICEABLE,
    QUOTE_ONLY_DOMAIN,
    WRONG_MANUFACTURER,
    WRONG_MPN,
    WRONG_PRODUCT,
)
from live_exact_priced_pdp.score import _save, demotion_snapshot
from live_exact_priced_pdp.sweep import build_final_report, format_report

ck = json.loads(Path("data/m3_live_exact_priced_pdp_v1_checkpoint.json").read_text(encoding="utf-8"))
agg: dict[str, dict] = defaultdict(
    lambda: {
        "candidates": 0,
        "live_exact": 0,
        "public_offers": 0,
        "prices": 0,
        "blocks": 0,
        "no_offer": 0,
        "quote_only": 0,
        "wrong": 0,
        "dead": 0,
    }
)
for row in ck["items"].values():
    for a in row.get("audits") or []:
        d = a.get("domain") or ""
        if not d:
            continue
        r = agg[d]
        r["candidates"] += 1
        st = a.get("state") or ""
        if st in EXTRACTABLE_STATES or st in {
            LIVE_EXACT_PDP_NO_PUBLIC_PRICE,
            LIVE_EXACT_PDP_QUOTE_ONLY,
        }:
            r["live_exact"] += 1
        if st in EXTRACTABLE_STATES:
            r["public_offers"] += 1
        if st == LIVE_EXACT_PDP_ACCESS_BLOCKED:
            r["blocks"] += 1
        if st == LIVE_EXACT_PDP_NO_PUBLIC_PRICE:
            r["no_offer"] += 1
        if st in {LIVE_EXACT_PDP_QUOTE_ONLY, LIVE_EXACT_PDP_LOGIN_REQUIRED}:
            r["quote_only"] += 1
        if st in {WRONG_MPN, WRONG_PRODUCT, WRONG_MANUFACTURER}:
            r["wrong"] += 1
        if st == DEAD_404:
            r["dead"] += 1
    if row.get("status") == "EXECUTABLE_PRICE" and row.get("seller"):
        agg[row["seller"]]["prices"] = int(agg[row["seller"]].get("prices") or 0) + 1

dem = {"build": BUILD, "domains": {}}
for d, r in agg.items():
    cand = r["candidates"]
    if cand < 3:
        continue
    klass = None
    if r["prices"] == 0 and r["blocks"] >= max(2, cand // 2):
        klass = ACCESS_BLOCKED_DOMAIN
    elif r["prices"] == 0 and r["quote_only"] >= 2 and r["live_exact"] >= 2:
        klass = QUOTE_ONLY_DOMAIN
    elif r["prices"] == 0 and r["no_offer"] >= 2 and r["live_exact"] >= 2:
        klass = IDENTITY_ONLY
    elif r["prices"] == 0 and r["live_exact"] == 0 and (r["wrong"] + r["dead"] + r["blocks"]) >= max(
        3, int(cand * 0.6)
    ):
        klass = NON_PRICEABLE
    if klass:
        dem["domains"][d] = {
            "domain": d,
            "class": klass,
            "reason": (
                f"cand={cand} live={r['live_exact']} prices={r['prices']} "
                f"blocks={r['blocks']} wrong={r['wrong']} dead={r['dead']}"
            ),
        }
_save(DEMOTIONS, dem)

rep = build_final_report()
print(format_report(rep))
print("---DEMOTIONS---")
print(json.dumps(demotion_snapshot(), indent=2))
print("---EASY25---")
print(json.dumps(rep.get("EASY_25"), indent=2))
print("---FINAL---")
print(json.dumps(rep.get("FINAL_ANSWERS"), indent=2))
print("---HARD---")
for h in rep.get("KNOWN_HARD_CASES") or []:
    print(
        f"{h['benchmark_id']}: state={h.get('pdp_state')} offer={h.get('public_offer')} "
        f"price={h.get('price')} result={h.get('result')} seller={h.get('live_seller')}"
    )
print("---CONTRADICTIONS---")
c = rep.get("BENCHMARK_CONTRADICTIONS") or {}
print("count", c.get("count"))
for it in (c.get("items") or [])[:30]:
    print(f"  {it.get('benchmark_id')}: {it.get('reasons')}")
