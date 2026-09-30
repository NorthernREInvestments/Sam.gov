# Phase L.2.4 — Failures

| Code | Count | Meaning |
|---|---:|---|
| `CURRENT_PRICE_NOT_FOUND` | 23 | No L.2.2-verified accessible acquisition price |
| `HISTORY_NOT_FOUND` | 21 | No exact/strong USAspending unit history |
| `QUANTITY_UNKNOWN` | 22 | Total profit blocked |
| `FREIGHT_UNRESOLVED` | 11 | Heavy / non-CONUS freight before final PASS |
| `IDENTITY_CONFLICT` | 1 | Product-page identity mismatch (Expedition SSV) |

No generic UNKNOWN for researched rows — each has an explicit `convergence_state`.
