# Phase L.13 — BidNet Public Artifacts

Primary validation set: **82** L.12 history-access gaps, enriched from L.10 recon with observed BidNet abstract URLs.

## Recovery loop (every row)

1. exact solicitation search  
2. public print search  
3. public attachment search  
4. public award search  
5. buyer + solicitation search  
6. buyer public-record pivot  
7. registration classification  
8. evidence exhausted  

## Observed URL family (learned, not invented)

Example seed (from prior discovery, not guessed IDs):

`https://www.bidnetdirect.com/public/supplier/solicitations/statewide/{SOLICITATION}/abstract?...`

Print/attachment siblings are added only when:

- linked from a fetched public page, or  
- previously verified via `record_observed_url_pattern`

## Outcomes

`PUBLIC_ARTIFACT_RECOVERED` · `BUYER_ARTIFACT_RECOVERED` · `PUBLIC_METADATA_ONLY` ·  
`FREE_REGISTRATION_STILL_REQUIRED` · `AUTHENTICATED_HISTORY_REQUIRED` ·  
`NO_PUBLIC_ARTIFACT_FOUND` · `EVIDENCE_EXHAUSTED`
