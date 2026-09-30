# Pilot Quote Readiness

**Build:** `20260928-m3-controlled-real-world-test`

States:

- `READY_FOR_OWNER_APPROVAL`
- `NEEDS_MINOR_REVIEW`
- `NOT_READY`

Later (owner only): `APPROVED_FOR_QUOTE_OUTREACH`

Supplier-facing packets use `build_supplier_facing_packet` — **no** gov history / max-buy / profit.

Internal control stored separately in `pilot_quote_packets.json` → `internal_only`.
