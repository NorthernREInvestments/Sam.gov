# Phase L.14.1 — Structured Source Strategy

**Build:** `20260928-m3-phase-l141-l14-repair-structured-source-audit`

## Priority order

1. Official API  
2. Official JSON/XML/CSV/bulk  
3. RSS/Atom / open-data (Socrata/CKAN/ArcGIS)  
4. Stable static HTML  
5. Fragile JS/anti-bot — only if high unique value  
6. Else `PARKED_FRAGILE_SOURCE`

## Tiers

| Tier | Meaning |
|------|---------|
| 1 | Structured official (SAM API, USAspending) |
| 2 | Stable public structured / public search |
| 3 | Stable static HTML (Sourcewell, HGAC, Boston bid-listings) |
| 4 | Fragile JS/auth — park by default |

## API primary / HTML fallback

When API succeeds → HTML is enrichment only.  
When API fails → stable HTML fallback. No duplicate ingestion.

## Engineering stop-loss

Do not endlessly reverse-engineer low-yield anti-bot portals. Park and move on unless unique commercial/history value is documented.
