# Pilot Owner Workflow

**Build:** `20260928-m3-controlled-real-world-test`

Owner can:

1. Open `artifacts/phase_l/pilot_ready_for_owner_approval.json` / `pilot_initial_batch.json`
2. Inspect each opportunity + original solicitation URL
3. Review suppliers and internal max-buy (`pilot_quote_packets.json` → `internal_only`)
4. Inspect supplier-facing packet (no internal economics)
5. Approve or reject — system does **not** send

Flow: `READY_FOR_OWNER_APPROVAL` → owner selects → later `APPROVED_FOR_QUOTE_OUTREACH`
