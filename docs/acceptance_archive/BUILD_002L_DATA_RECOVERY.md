# Build 002L — Data Recovery

**Metered NextBid calls:** **0**

| Metric | Count |
|--------|------:|
| Unique NextBid reprocessed | 849 |
| Stage B | 442 |
| Stage C (NextBid) | 6 |
| Quantity defined | 7 |
| Unknown kept (not destroyed) | 676 |
| Prior free-source fits | 3 |
| Registry records | 849 |
| 002I semantic restores | 3 |

## Stage C fits after 002L offline reprocess

- **NB00348901** — qty 12 — C26RI103317777R - REBID - Invitation For Bids (Non- Negotiable) - Furnish and Deliver Twelve (12) Miles of 288 Ribbon Fiber Optic Cable.
- **NB00321554** — qty 1 — PURCHASE OF ONE (1) 6” HIGH PRESSURE DIESEL PUMP  WITH A TRAILER
- **NB00345848** — qty 2 — Two (2) Animal Transport Vehicles
- **NB00375339** — qty 53 — Unication G5 Dual Band P25 Voice Pagers 700-800 MHz/VHF (136-174 MHz)
- **NB00339475** — qty 2 — Two (2) New/Unused 2026 or Newer Hybrid Full Size Light Duty four (4) door pick up
- **NB00326707** — qty 1 — One (1) Compact Tracked Articulating Boom Lift with Transport Trailer for Saratoga Region

## Method

- Replayed durable checkpoint listings∪enriched, inventory, and 002I/002J/002K acceptance evidence.
- Restored quantity_semantic from 002I fit evidence + title affirmative extraction when slim checkpoints had dropped it.
- Ingested all rows into permanent procurement registry (append-only history).
- No metered NextBid calls. No scheduler activation.
