# R1 — Solicitation Intake

`SolicitationIntakeService` → `response_engine.intake`

## Behavior

- Ingests a **document set** (not one PDF assumption)
- Classifies types: BASE_SOLICITATION, AMENDMENT, PRICING_SHEET, BUYER_TEMPLATE, MASTER_SOLICITATION, …
- Content/file hashing prevents byte-identical duplicates; provenance refs retained
- Amendments auto-register with materiality defaulting toward material/potentially material

## API

`POST /api/response-projects/from-opportunity/{id}` with optional `documents[]`
