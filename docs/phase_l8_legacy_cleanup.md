# Phase L.8 — Legacy Cleanup

## Reconciliations

- Missing government value is **recovery branch**, not dead-end
- Missing supplier is **recovery branch**, not final failure
- Missing quantity allows **unit-only** evaluability (not automatic kill)
- Cap flags remain unified via `acquisition_lanes` (no fixed Stage3 / deep / 32-positive caps)
- L.5–L.7 recovery reused via imports; L.8 does not fork a parallel funnel
- `NO_PRICE_INFORMATION` remains public-price failure class only

## Retained compatibility

Historical rescue runners `l21`–`l7` kept for replay. Stored buyer/supplier memory formats preserved.
