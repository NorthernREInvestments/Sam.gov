# Build 002L — Data Reconciliation

**Build ID:** `20261122-m3-2-build-002l`  
**Metered NextBid calls:** **0**  
**Paid acquisition:** LOCKED

## A. Six original NextBid fits (Build 002I)

| 002I ID | 002K list? | Current stage | Qty | Package | Status |
|---------|------------|---------------|-----|---------|--------|
| NB00348901 | False | VERIFIED_M3_PROCUREMENT_FIT | 12 | PARTIAL | RETAINED_STAGE_C |
| NB00345848 | False | VERIFIED_M3_PROCUREMENT_FIT | 2 | PARTIAL | RETAINED_STAGE_C |
| NB00375339 | True | VERIFIED_M3_PROCUREMENT_FIT | 53 | PARTIAL | RETAINED_STAGE_C |
| NB00321554 | False | VERIFIED_M3_PROCUREMENT_FIT | 1 | PARTIAL | RETAINED_STAGE_C |
| NB00339475 | True | VERIFIED_M3_PROCUREMENT_FIT | 2 | PARTIAL | RETAINED_STAGE_C |
| NB00326707 | True | VERIFIED_M3_PROCUREMENT_FIT | 1 | PARTIAL | RETAINED_STAGE_C |

### What happened

- Build 002I reported six strict NextBid Stage C fits (NB00348901, NB00345848, NB00375339, NB00321554, NB00339475, NB00326707).
- Build 002K's six-item qualifying list mixed three of those NextBid fits with three prior free-source fits — it was not the same six NextBid IDs.
- NB00348901, NB00345848, and NB00321554 remained in durable checkpoint/inventory but lost quantity_semantic when slim enriched checkpoints omitted evidence; 002K offline reprocess therefore demoted them.
- Build 002L restores semantic/evidence from 002I acceptance + title affirmative patterns without metered API calls.

### Free-source entries that appeared in the 002K “six”

These were **not** the missing NextBid fits; they were prior free-source Stage C rows retained alongside three NextBid fits.

| Solicitation | Source |
|--------------|--------|
| PE-74600-NONST-2027-000000031 | free_source_prior |
| PE-61700-NONST-2027-000000082 | free_source_prior |
| SRS27002-ATHSUPPLY | free_source_prior |

## B. 917 vs 675

| Metric | Value |
|--------|------:|
| 002I discovered (reported) | 917 |
| 002J durable seen_nb_ids | 675 |
| Gap | 242 |
| Current recoverable unique | 849 |
| Gap status | **NOT_PRESENT_IN_ANY_DURABLE_ID_LIST** |

Evidence:
- BUILD_002I_NEXTBID_ACCEPTANCE.json reports discovered=917 and 36 completed_queries.
- BUILD_002J_ACCEPTANCE.json forensic checkpoint shows seen_nb_ids=675 with only 21 completed_queries.
- No artifact retains the full list of 917 nb_ids — only the count.
- 15 discovery query labels present in 002I are absent from the 002J checkpoint, consistent with durable state rewrite/loss rather than proven in-run duplicates.
- Current merged unique population (listings∪enriched) is recoverable and preserved into the permanent registry.

## C. 710-call discrepancy

| Metric | Value |
|--------|------:|
| Local 002I reported | 2344 |
| Provider used (era) | 3054 |
| Gap | 710 |
| Status | **UNVERIFIED** |
| 002K provider delta | 16 |
| Current verified provider usage (free `/usage`) | used **3070** / remaining **2930** / allowance **6000** |
| Provider solicitations.delivered | **922** |
| 002L metered delta | **0** |

- Free /account/activity was unavailable or empty during 002J (no_such_route / 0 rows) — cannot attribute the 710 calls to specific opportunities.
- Likely contributors remain: production scheduler after 002I deploy, retries, concurrent jobs, or local under-counting — NOT independently proven from call-level logs.
- Build 002L does not invent attribution. Gap remains UNVERIFIED.
