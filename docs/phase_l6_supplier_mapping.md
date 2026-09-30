# Phase L.6 — Supplier Mapping

## Classification

authorized dealer · authorized distributor · OEM direct · major commercial reseller · specialist dealer · unknown seller

## Enrichment

`enrich_suppliers()` builds candidates from commercial identity + known-supplier memory (`data/phase_l6_supplier_memory.json`).

Tracks: product fit, public family evidence, quote availability path, contact path, location vs delivery.

**No contact / no outreach in L.6.**

## Diversity targets

- Common products: prefer 3+
- Specialized commercial: prefer 2+
- Fewer legitimate suppliers does **not** fail the opportunity

## Memory

Reuse prior relationship intelligence by manufacturer / family / category. Never assume old price is current.
