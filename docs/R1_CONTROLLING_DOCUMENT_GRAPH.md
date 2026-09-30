# R1 — Controlling Document Graph

`response_engine.document_graph`

## Edges

- `SUPERSEDED_BY` — document A replaced by B
- `MODIFIES` — amendment modifies base

## Amendment records

Track acknowledgment required, materiality, and change flags (deadline, quantity, spec, delivery, forms, …).

## Package staleness

`PACKAGE_STALE_DUE_TO_AMENDMENT` when amendments change controlling requirements.

## Conflicts

Two controlling amendments with conflicting delivery dates → `DOCUMENT_CONFLICT` + clarification (no auto-pick).
