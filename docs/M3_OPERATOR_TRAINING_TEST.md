# M3 Operator Training Test

**Goal:** A new operator can run the core workflow in **≤ 30 minutes** (stretch **≤ 15**).  
**Console:** `/ops`  
**Do not:** contact real suppliers, submit RFQs/bids, or burn SAM API credits.

---

## Timed checklist (pass / fail)

| # | Task | Pass if | Time box |
|---|------|---------|----------|
| 1 | Find highest-priority deal | Opens Today; top CALL TODAY is obvious | 2 min |
| 2 | Identify supplier to call | Supplier name + CALL FIRST visible | 1 min |
| 3 | Find phone / RFQ path | Phone and/or RFQ link on call workspace | 1 min |
| 4 | Know what to ask | Must Ask list visible without leaving screen | 1 min |
| 5 | Record price | Currency field accepts currency/number | 1 min |
| 6 | Record freight | Freight answer field works | 1 min |
| 7 | Record lead time | Lead time field works | 1 min |
| 8 | Add notes | Notes field saves with call | 1 min |
| 9 | Save call | Sees **Saved** + timestamp | 1 min |
| 10 | Schedule follow-up | Sets Quote promised by date; understands Follow Up | 2 min |
| 11 | Review a quote | Opens Quotes / quote review | 2 min |
| 12 | Recommended next action | Sees BID / GET ANOTHER QUOTE / NEGOTIATE / SKIP | 1 min |
| 13 | Complete a registration task | Opens registration walkthrough; understands Mark Registered | 3 min |
| 14 | Identify a blocked opportunity | Opens Blocked; reads plain-language blocker | 2 min |
| 15 | See remaining workload | Returns to Home; counts match Today | 1 min |

**Pass criteria:** complete 1–15 without developer help in ≤ 30 minutes.  
**Stretch:** ≤ 15 minutes.

---

## Confusion checks (during review)

- Is the next action obvious?
- Are there too many equal buttons?
- Are backend/phase terms exposed on primary screens?
- Does the operator need long docs?
- Can the call task finish on one screen?

If any answer is “no”, simplify before declaring READY.

---

## Result log

| Date | Operator | Minutes | Pass? | Notes |
|------|----------|---------|-------|-------|
| | | | | |

**Verdict mapping**

- Pass + production data works → `M3_OWNER_UI_OPERATOR_READY`
- Screens exist but friction remains → `M3_OWNER_UI_PARTIAL`
- Core flows broken → `M3_OWNER_UI_FAILED`
