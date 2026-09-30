# Phase J — History Reconciliation Report

**Date:** 2026-09-25

## Before → After (Phase I deep 80)

| Metric | Before | After |
|--------|-------:|------:|
| Quote-ready | 0 | **4** |
| Bid-ready | 0 | 0 |
| EXACT_CONFIRMED identity | 0 | 36 |
| STRONG_MATCH | 31 | 9 |
| UNKNOWN identity | 49 | 29 |
| STRONG_HISTORY | 4 | 0 (false noun matches removed) |
| MODERATE_HISTORY | 0 | **4** |
| WEAK_HISTORY | 10 | 10 |
| NO_HISTORY | 66 | 66 |
| False-match awards rejected | — | **32** |
| Usable exact-NSN award links | — | **4** |

## Quote-ready after audit (exact NSN + AGED comparable)

| Solicitation | NSN | Hist award | Age | Lot basis | Max supplier ≤ |
|--------------|-----|------------|-----|-----------|----------------|
| SUPPORT,TURBINE COM | 2840-00-411-8852 | SPRTA120P0012 (25 EA FMS) | AGED | $188,890 | $178,890 |
| Transmission Kit | 2520-01-682-2226 | SPRDL120C0108 (100 EA) | AGED | $1,952,080 | $1,942,080 |
| Toolkit, Pipefitter | 5180-00-596-1509 | SPRDL119F0107 (26) | AGED | $124,121 | $114,121 |
| Engine Diesel | 2815-01-536-9262 | W56HZV20F0427 (38 EA) | AGED | $318,014 | $308,014 |

All four: eligibility N/A · identity EXACT · history COMPARABLE · current solicitation qty still **UNKNOWN** (disclosed — operator must confirm qty/UOM before outreach).

## Wheel false-match

Prior STRONG_HISTORY without NSN → **rejected**. No quote-ready from noun similarity.

## Recency / unit price

- AGED exact-NSN with lot total or QTY-normalized price → COMPARABLE (drives economics)  
- STALE → reference only  
- UNIT_PRICE_NORMALIZED when QTY parsed from award text  
- No CPI inflation treated as verified willingness-to-pay
