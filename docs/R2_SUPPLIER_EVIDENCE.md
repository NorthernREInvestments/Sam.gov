# R2 — Supplier Evidence

Integrates L.22 / existing quotes via `response_engine/supplier_evidence.py`.

- Never fabricates prices
- Written / portal quotes may support `VERIFIED` acquisition
- Historical / verbal / web ≠ verified acquisition
- Quote conditions stay INTERNAL (firewall)
- Gap questions feed L.22 — no max-buy / historical / margin leakage

`sanitize_supplier_facing_payload()` strips internal economics.
