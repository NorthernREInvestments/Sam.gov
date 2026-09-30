# Phase J — Identity Rules

**Build:** `20260925-m3-phase-j-identity-1`

## Confidence hierarchy

| Level | Internal | Phase H map | Rule |
|-------|----------|-------------|------|
| 1 | `EXACT` | `EXACT_CONFIRMED` | Exact NSN, or MPN+CAGE |
| 2 | `STRONG` | `STRONG_MATCH` | Exact MPN without manufacturer, or drawing ref |
| 3 | `PARTIAL` | `PARTIAL` | Normalized nomenclature only |
| 4 | `UNKNOWN` | `UNKNOWN` | Truncated FSC-- title without NSN/MPN; insufficient evidence |

Title/nomenclature **supports** a match; it never creates a historical price match alone.

## Keys extracted

NSN · MPN · CAGE · drawing · FSC · nomenclature (normalized abbreviations) · kit flag

## Historical join order

1. Exact NSN  
2. Exact MPN (+ CAGE when present)  
3. Stop — no bare noun keywords (`WHEEL ASSEMBLY`, etc.)

## False-match safety

If current identity has NSN/MPN and award description does not share it → `MATCH_REJECTED` → cannot drive economics.
