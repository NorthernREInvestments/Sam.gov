# Historical Research Source Registry

**Architecture:** On-demand research capability — **not** a historical award database.  
The government keeps the awards. M3 keeps **how to find them**.

Last validated: 2026-11-26 (targeted live probes).  
Cost: **free** portals/APIs. OpenAI only when reading evidence for an active opportunity.

| Status | Meaning |
| --- | --- |
| **Verified method** | Targeted search returned structured usable leads in software |
| **Partial** | Public access + method known; automation incomplete |
| **Blocked** | Bot wall / obfuscation / operating lock |
| **Not unit price** | Totals/funding/catalog — label carefully |

Code: `m3/research/historical_sources.py`, `historical_usaspending.py`, `historical_match.py`

---

## Scoreboard (2026-11-26)

| Source | Status | What works | Unit price? |
| --- | --- | --- | --- |
| USAspending | Verified | `POST /api/v2/search/spending_by_award/` | No — contract totals |
| Texas ESBD awards | **Verified** | `GET /esbdawards?keyword=` → PO #, amount, vendor, date | No — PO totals |
| USAC E-Rate FRN lines | **Verified** | SODA `hbj5-2bpj` by manufacturer/model | Funding-line costs ≠ awards |
| Louisiana LaPAC bid # | **Verified** | `dspBid.cfm?search=bidno&term=` → public PDFs | After doc read |
| Louisiana eCat | Partial | Form loads; ColdFusion field quirks block reliable POST | Catalog/contract |
| OregonBuys / BidBuy / COMMBUYS | Partial | Status codes + doc types mapped; Faces ajax results not stable yet | TBD |
| Missouri Awarded Search | Blocked | UI loads; Incapsula + obfuscated Angular API | TBD |
| PA eMarketplace | Open IFBs only | Materials CSV + FileDownload for **active** bids | No awarded filter found |
| DIBBS | Deferred | Operating lock | — |

---

## 1. USAspending.gov — Verified (totals)

| Field | Value |
| --- | --- |
| Endpoint | `POST https://api.usaspending.gov/api/v2/search/spending_by_award/` |
| Code | `historical_usaspending.search_awards_by_keyword` |
| Pricing type | `total_contract_award_amount` |
| Limitation | Never divide Award Amount into unit prices |

---

## 2. Texas SmartBuy / ESBD awards — Verified (PO totals)

| Field | Value |
| --- | --- |
| URL | https://www.txsmartbuy.gov/esbdawards |
| Method | `GET ?keyword=` (no login) |
| Returns | PO Number, PO Amount, Vendor Name, Award Date, Agency # |
| Code | `historical_sources.search_texas_esbd_awards` |
| Also | `/esbd?status=Awarded` lists awarded solicitations; `/browsecontracts` statewide catalogs |
| Pricing type | `purchase_order_price` total — **not** line unit price |
| Catalog vs purchase | SmartBuy contract browse = authorized catalog; ESBD awards = PO evidence |
| Validated | 2026-11-26 keyword `medical` / `furniture` returned parseable PO cards |
| Unsuccessful | Expecting unit prices on award cards; keyword filter is soft (review match_reason) |

---

## 3. USAC E-Rate Open Data — Verified (funding lines)

| Field | Value |
| --- | --- |
| Dataset | FRN Line Items `https://opendata.usac.org/resource/hbj5-2bpj.json` |
| UI | https://opendata.usac.org/d/hbj5-2bpj |
| Search | `$where` on manufacturer/model or `$q` |
| Fields | manufacturer, model, product, unit, qty, one-time/monthly costs |
| Code | `historical_sources.search_usac_erate_line_items` |
| Pricing type | Treat as `government_estimate` / funding context |
| Validated | 2026-11-26 Cisco model query returned line costs (e.g. unit eligible cost + qty) |
| Hard rule | Approved/requested E-Rate cost ≠ awarded/paid government unit price |

Related datasets: FRN status `qdmp-ygft`, 471 basic `9s6i-myen`, commitments `avi8-svp9`.

---

## 4. Louisiana LaPAC — Bid number verified; eCat partial

| Path | Status | Method |
| --- | --- | --- |
| Bid number search | **Verified** | `GET https://wwwcfprd.doa.louisiana.gov/osp/lapac/dspBid.cfm?search=bidno&term={BID}` → `/agency/pdf/` docs |
| Open category segments | Verified (live) | Existing `louisiana_lapac` adapter |
| eCat contract catalog | Partial | `eCat/dsp_eCatSearchLagov.cfm` — brittle ColdFusion (`searchForLaGOV`, `Coop`, …) |
| PROACT login contracts | Blocked | Login wall |

Code: `search_louisiana_lapac_by_bidno` (+ live open-bid adapter).

---

## 5. Jaggaer BSO (OregonBuys / Illinois BidBuy / COMMBUYS) — Partial

Shared UI: `/bso/view/search/external/advancedSearchBid.xhtml`

### Proven mapping

| Control | Values |
| --- | --- |
| Status | Closed=`2BCL`, Intent To Award=`2BIA`, Bid to PO=`2BPO`, Opened=`2BO`, … |
| COMMBUYS type | Historical Solicitation=`SH` |
| IL document types | `BID_SOLICITATIONS`, `CONTRACT_BLANKETS`, `VENDORS`, `PURCHASE_ORDER` |
| OR document types | `BID_SOLICITATIONS`, `CONTRACT_BLANKETS` |

### Blocked / incomplete

- Must POST option **values** (`2BCL`), not labels (`Closed`) — label causes `Validation Error: Value is not valid`.
- PrimeFaces `searchNew` ajax returns large form markup; **result table extraction not reliable yet**.
- Next step: one browser Network trace of a successful Closed/PO search → mirror partial-render IDs in a shared BSO adapter.

Helper: `historical_sources.bso_automation_status()`.

---

## 6. Missouri Awarded Bid/Contract Search — Blocked

| Field | Value |
| --- | --- |
| URL | https://awardedsearch.oa.mo.gov/PublicAccess/index.html |
| Stack | Angular (`obpa-search-panel` / `obpa-document-hitlist`) + Incapsula |
| Finding | Bundled JS does not expose readable REST paths; naive `/api/search` = 404 |
| Next | Owner-attended XHR capture or published API |

`historical_sources.missouri_awarded_status()`

---

## 7. Pennsylvania eMarketplace — Active Materials only

| Field | Value |
| --- | --- |
| Live adapter | `pa_emarketplace` Materials CSV (`ad_type=10`) + FileDownload |
| Ad types found | Service=1, Materials=10, Service&Materials=9, Construction=3/12, Real Estate=4, All=11 |
| Awarded filter | **Not present** on public Search.aspx |
| Historical path | TBD (different report/board); do not pretend Materials CSV is award history |

---

## 8. DIBBS — Deferred (lock)

No client. Unlock before NSN award research.

---

## Agent instructions

1. Prefer verified free adapters above before OpenAI web_search.
2. Always run results through `normalize_award_record` (match_class + match_reason).
3. Do not bulk download. Cap `limit` small.
4. Do not store unrelated awards permanently — opportunity-scoped research runs only.
