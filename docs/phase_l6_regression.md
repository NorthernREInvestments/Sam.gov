# Phase L.6 — Regression

## Caps

`STAGE3_NO_ROW_CAP` · `DEEP_RESEARCH_NO_FIXED_COUNT` · `MANUAL_QUEUE_NO_FIXED_CAP` — all asserted.

## Buyer memory isolation

Agency-wide medians removed. Memory is buyer×product only. Contaminated DoD-wide median ($5,937) from early L.6 run purged.

## Verified-positive gate

`EXACT_VERIFIED` / final verified-positive path unchanged. Quote-dependent positives are **not** reported as guaranteed profit.

## Lead-price hygiene

Tiny APPROXIMATE acquisition placeholders (e.g. $50–$5,000 milspec stubs) are not treated as strong leads.

## History budget

Quote-required / commercial lanes processed before specialty so USAspending budget is not burned on non-commercial first.

## Tests

`tests/test_phase_l6_quote_economics.py` — exact/strong/comparable/range, revenue, max-buy tiers, financing, freight, quote bands/simulator, quote-dependent tiers, unknown conversion, UOM, headroom, no-cap, verified gate, category benchmark, buyer-memory product key.
