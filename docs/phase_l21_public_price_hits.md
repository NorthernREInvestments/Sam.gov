# Phase L.2.1 — Public Price Hits

Only rows where a current public unit price was attached during the live rescue.

| Solicitation | Product | Hist gov unit | Market unit | Qty | Evidence URL | Confidence | Notes |
|---|---|---:|---:|---:|---|---|---|
| SPRTA126D0008 | P/N 37D401750P104 Spraybar | 922.00 | 20000.00 | — | cinemaseattle.com | EXACT_PUBLIC_PRICE_LOW | Low-confidence / likely off-topic landing — retain for audit only |
| SPRTA126F0068 | NSN 2840-01-599-4493 | 3270.27 | 25000.00 | 161 | cbc.ca | EXACT_PUBLIC_PRICE_LOW | Economics ran; **negative** net (−$3.7M) — not owner queue |
| W519TC…MD65MD66 | Impulse Cartridge MD65/66 | — | 220000.00 | 31500 | techworm.net | EXACT_PUBLIC_PRICE_LOW | Unverified / suspicious magnitude |
| SPRTA1-26-Q-0473 | NSN 1650-00-404-0445 Valve | — | 9.00 | 1 | newegg.com | EXACT_PUBLIC_PRICE_LOW | Retailer host; qty=1; no hist → no pass |

**Owner-worthy (≥$10K expected net): none.**

Post-run hardening: selection now requires known commercial domain **or** product-hint match / JSON-LD|meta — to reject news/blog SERP roots.
