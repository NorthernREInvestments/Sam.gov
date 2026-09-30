# R2 — Financing / Execution

`response_engine/financing.py`

- Financing cost = amount × rate (default 5%, configurable)
- Applied once (no double count)
- PG / personal credit / prepay → `EXECUTION_FAIL` (µLab alignment)
- `OWNER_COMPANY_CASH_REQUIRED_BEFORE_GOV_PAYMENT` visible
- No lender contact / applications

Preferred: supplier terms / PO finance / factoring / $0 owner cash.
