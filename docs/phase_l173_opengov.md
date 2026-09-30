# L.17.3 OpenGov

```json
{
  "kind": "L173OpenGovResults",
  "build": "20260928-m3-phase-l173-platform-adapters-accessible-now-growth",
  "generated_at": "2026-09-29T03:21:10.569683+00:00",
  "platform": "OpenGov",
  "mapped": 102,
  "tested": 102,
  "previously_completed": 0,
  "access_state_counts": {
    "ANTI_BOT": 66,
    "NO_CURRENT_OPPORTUNITIES": 4,
    "AUTH_REQUIRED_TO_VIEW": 1,
    "ADAPTER_FAILED": 26,
    "PUBLIC_LISTING_AUTOMATABLE": 5
  },
  "activation_state_counts": {
    "BLOCKED": 67,
    "PUBLIC_DISCOVERY_READY": 4,
    "MAPPED_ONLY": 26,
    "INGESTION_ACTIVE": 5
  },
  "ingestion_active": 5,
  "live_rows": 29,
  "jurisdictions": [
    {
      "jurisdiction_id": "CO:COUNTY:08005:arapahoe_county",
      "list_url": "https://procurement.opengov.com/portal/arapahoecounty",
      "access_state": "ANTI_BOT",
      "activation_state": "BLOCKED",
      "docs_state": "DOCS_UNAVAILABLE",
      "opportunity_count": 0,
      "error": "opengov_cdn_cloudflare",
      "followed_child": null,
      "portal_slug": "arapahoecounty"
    },
    {
      "jurisdiction_id": "CO:COUNTY:08059:jefferson_county",
      "list_url": "https://procurement.opengov.com/portal/jeffco",
      "access_state": "ANTI_BOT",
      "activation_state": "BLOCKED",
      "docs_state": "DOCS_UNAVAILABLE",
      "opportunity_count": 0,
      "error": "opengov_cdn_cloudflare",
      "followed_child": null,
      "portal_slug": "jeffco"
    },
    {
      "jurisdiction_id": "FL:COUNTY:12057:hillsborough_county",
      "list_url": "https://procurement.opengov.com/portal/hillsboroughcounty",
      "access_state": "ANTI_BOT",
      "activation_state": "BLOCKED",
      "docs_state": "DOCS_UNAVAILABLE",
      "opportunity_count": 0,
      "error": "opengov_cdn_cloudflare",
      "followed_child": null,
      "portal_slug": "hillsboroughcounty"
    },
    {
      "jurisdiction_id": "FL:COUNTY:12095:orange_county",
      "list_url": "https://procurement.opengov.com/portal/orangecountyfl",
      "access_state": "ANTI_BOT",
      "activation_state": "BLOCKED",
      "docs_state": "DOCS_UNAVAILABLE",
      "opportunity_count": 0,
      "error": "opengov_cdn_cloudflare",
      "followed_child": null,
      "portal_slug": "orangecountyfl"
    },
    {
      "jurisdiction_id": "GA:COUNTY:13067:cobb_county",
      "list_url": "https://www.cobbcounty.org/finance/purchasing",
      "access_state": "NO_CURRENT_OPPORTUNITIES",
      "activation_state": "PUBLIC_DISCOVERY_READY",
      "docs_state": "DOCS_PUBLIC",
      "opportunity_count": 0,
      "error": null,
      "followed_child": null,
      "portal_slug": null
    },
    {
      "jurisdiction_id": "GA:COUNTY:13089:dekalb_county",
      "list_url": "https://www.dekalbcountyga.gov/purchasing",
      "access_state": "AUTH_REQUIRED_TO_VIEW",
      "activation_state": "BLOCKED",
      "docs_state": "DOCS_AUTH_REQUIRED",
      "opportunity_count": 0,
      "error": null,
      "followed_child": null,
      "portal_slug": null
    },
    {
      "jurisdiction_id": "GA:COUNTY:13121:fulton_county",
      "list_url": "https://www.fultoncountyga.gov/services/doing-business/procurement",
      "access_state": "ADAPTER_FAILED",
      "activation_state": "MAPPED_ONLY",
      "docs_state": "DOCS_UNAVAILABLE",
      "opportunity_count": 0,
      "error": null,
      "followed_child": null,
      "portal_slug": null
    },
    {
      "jurisdiction_id": "GA:COUNTY:13135:gwinnett_county",
      "list_url": "https://www.gwinnettcounty.com/web/gwinnett/Departments/SupportServices/Purchasing",
      "access_state": "ADAPTER_FAILED",
      "activation_state": "MAPPED_ONLY",
      "docs_state": "DOCS_UNAVAILABLE",
      "opportunity_count": 0,
      "error": null,
      "followed_child": null,
      "portal_slug": null
    },
    {
      "jurisdiction_id": "IL:COUNTY:17043:dupage_county",
      "list_url": "https://www.dupagecounty.gov/government/departments/finance/purchasing.php",
      "access_state": "ADAPTER_FAILED",
      "activation_state": "MAPPED_ONLY
```

CDN portals classified `ANTI_BOT`. Agency mirrors → `PUBLIC_LISTING_AUTOMATABLE` / `INGESTION_ACTIVE` when parseable.
