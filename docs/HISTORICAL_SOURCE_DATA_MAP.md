# Historical Source Data Map

Maps fields M3 needs → where sources typically expose them. On-demand only.

| M3 field | USAspending API | Bid tab / award PDF | Contract catalog (e.g. SmartBuy) | PO / expenditure |
| --- | --- | --- | --- | --- |
| Product description | Description (coarse) | Line description | Catalog item text | Line description |
| MPN / model | Rare (text only) | Often | Often | Sometimes |
| NSN | Sometimes in description | Often (DoD) | Rare | Sometimes |
| Quantity | Usually absent at award total | Line qty | Order qty / min | Line qty |
| UoM | Absent | Line UoM | Catalog UoM | Line UoM |
| Unit price | **No** (use Award Amount carefully) | Awarded unit | Catalog unit (not purchase) | PO unit |
| Extended price | Award Amount ≈ obligation total | Line extended | N/A | Line extended |
| Awardee | Recipient Name | Named vendor | Contract holder | Vendor |
| Award date | Start Date | Award / board date | Contract effective | PO date |
| Contract / sol # | Award ID | Header | Contract # | PO / contract |
| pricing_type | `total_contract_award_amount` | `awarded_unit_price` when shown | `contract_authorized_catalog_price` | `purchase_order_price` |

## USAspending request (verified)

`POST /api/v2/search/spending_by_award/` with `filters.keywords`, `filters.time_period`, `award_type_codes` A–D.  
Response fields used: Award ID, Recipient Name, Award Amount, Description, Awarding Agency, Start/End Date, NAICS, PSC.

## Jaggaer BSO

Shared UI path: `/bso/view/search/external/advancedSearchBid.xhtml` on OregonBuys, BidBuy, COMMBUYS.  
Field map for closed/award filters = TBD after first successful automated query (document here when proven).
