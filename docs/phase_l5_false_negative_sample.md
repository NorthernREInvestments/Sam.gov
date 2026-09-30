# Phase L.5 — False Negative Sample

100 inspectable commercial false negatives captured in `artifacts/phase_l/l5_retention_audit.json` → `false_negative_sample`.

## Pattern

1. **Buyer wants:** tangible commercial goods (IT, fleet, pumps, furniture, equipment).
2. **Why not advanced (L.4):** `no_identity_anchor` — no NSN/MPN/regex model.
3. **Genuinely disqualifying?** No — missing exact identity ≠ non-commercial.
4. **Could Stage 2 research?** Yes — descriptive/brand-or-equal/category anchors.
5. **Rule change:** L.5 `collect_stage2_anchors_l5` admits `descriptive_spec`, `brand_or_equal`, `brand_clue`, `document_signal`, `category_tangible`.

Lost commercial candidates identified: **1,340**. Recovered under L.5: **322**.
