# R2 — UOM Normalization

Canonical aliases in `response_engine/uom.py`.

- EA/EACH/UNIT → EA
- CASE/BOX/PACK require **known pack size** or `UOM_CONVERSION_BLOCKED`
- DOZEN → ×12, PAIR → ×2
- FT vs ROLL without roll length → blocked

**Rule:** unknown conversion never becomes verified economics.
