# Phase L.11 — Exact Award-History Recovery Design

## State machine

Every Gov D row enters `EXACT_HISTORY_RECOVERY`.

Outcomes:

- `GOV_UPGRADED_A` / `GOV_UPGRADED_B` / `GOV_UPGRADED_C`
- `HISTORY_AUTH_REQUIRED`
- `HISTORY_DOCUMENT_MISSING`
- `HISTORY_SOURCE_BLOCKED`
- `HISTORY_BUYER_RECORDS_NOT_FOUND`
- `HISTORY_EVIDENCE_EXHAUSTED`

## Buyer-first order

Same buyer: solicitation family → model → MPN → description → prior award → bid tab → contract → board → PO → expenditure → renewal → archived.

Then: product graph → platform history → **buyer pivot** → solicitation-number / doc-title ledger → optional board live fetches → USAspending/cache.

## Gov A / B / C rules (deterministic)

| Grade | Rule IDs |
|-------|----------|
| A | `L11_GOV_A_SAME_BUYER_EXACT_AWARD`, `…_BID_TAB_LINE`, `…_BOARD_APPROVED`, `…_CURRENT_BUDGET`, `…_NSN_EXACT` |
| B | `L11_GOV_B_OTHER_GOV_EXACT_MODEL`, `…_SAME_BUYER_NEAR_CONFIG`, `…_REPEATED_STABLE` |
| C | `L11_GOV_C_NEAR_FAMILY`, `…_MODEL_BAND` |

Config mismatch (upfit/loaded/bare) blocks Gov A.

Never invent unit price from total without quantity. Lot awards → `LOT_PRICE`.

## Auth classes (not collapsed)

`PUBLIC_REGISTRATION_REQUIRED` · `AUTH_ACCOUNT_REQUIRED` · `BUYER_VENDOR_ACCOUNT_REQUIRED` · `PRIVATE_RESTRICTED` · `UNKNOWN_AUTH`

No CAPTCHA/login bypass. Alternate public paths only.

## Module

`phase_l.exact_history_recovery`
