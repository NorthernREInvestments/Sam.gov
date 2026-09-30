# Phase G — Validation Scorecard

**Method:** Factual PASS / PARTIAL / FAIL / NOT TESTED  
**Evidence:** LIVE_SOURCE batch 2026-09-24 (800 retrieved) + audits

| Domain | State | Evidence | Note |
|--------|-------|----------|------|
| Live discovery/intake | PASS | 800 LIVE_SOURCE via SAM API | Stopped on budget after 8 calls |
| Provenance labeling | PASS | 800/800 LIVE_SOURCE | Fixtures not counted as live |
| Classification (product vs service) | PASS | 11 service/construction rejects correct | Ambiguous UNKNOWN often held |
| Product identity | PARTIAL | NSN/P/N signals present; gate keeps UNCONFIRMED | Listing-only |
| Quantity/UOM | PARTIAL | QUANTITY_UOM_UNCONFIRMED on enriched set | Correctly blocks READY |
| Document/TDP | PARTIAL | Submission/delivery UNKNOWN; few explicit TDP codes | Known blind spot |
| Government price history | FAIL* | 0 history attached in batch | *batch path gap, not fabricated |
| Supplier / market evidence | PARTIAL | QUOTE_NOT_EXECUTABLE / SUPPLIER_NOT_VALIDATED | Public ≠ quote |
| Economics | PASS | ECONOMICS_INCOMPLETE blocks READY | No fake profit |
| Financeability | PASS | FINANCING_INCOMPATIBLE_OR_UNKNOWN blocks | No owner-cash assumption |
| Deadline | PARTIAL | No deadline-primary blocks in sample | Not deeply stressed |
| Owner readiness gate | PASS | 0 false READY | Conservative |
| Operator next action | PARTIAL | Gate plain-English present; action enum priority fixed | Still multi-blocker dense |
| False-readiness audit | PASS | 0 READY; 0 false | — |
| False-rejection audit | PASS | 0/11 false | — |
| DIBBS direct | NOT TESTED | Bot-blocked | SAM DLA fallback only |
| WAWF / post-award | NOT TESTED | Out of scope | Phase G stop |

\*History FAIL means the Phase G batch did not retrieve history — not that the gate invented history.

## Live-test readiness (scorecard view)

See completion report §21 for final classification.
