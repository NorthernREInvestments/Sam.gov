# Phase L.14.1 — API / Feed Inventory

**Build:** `20260928-m3-phase-l141-l14-repair-structured-source-audit`

See `artifacts/phase_l/l141_api_feed_audit.json` and `l141_structured_source_inventory.json`.

## Highest priority (owner decision for keys/accounts later — no auto-register)

| Source | Path | Auth | Cost | Role |
|--------|------|------|------|------|
| SAM.gov Opportunities API | REST | FREE_API_KEY | free | Live federal opps |
| SAM public search | Public search | none | free | Live federal (active) |
| USAspending API | REST | none | free | Award history |
| Sourcewell solicitations | Stable HTML | none | free | Live coop RFPs |
| HGACBuy | Stable HTML | none | free | Live regional coop |
| Boston bid-listings | Stable HTML | none | free | OpenGov alternate |
| State open-data | Socrata/CKAN/ArcGIS | usually none | free | Awards/PO expansion |

## Do not reject free API keys

Classify: `PUBLIC_NO_AUTH` | `FREE_API_KEY` | `FREE_ACCOUNT` | `PAID_API` | `RESTRICTED_API`. Owner decides registration later.
