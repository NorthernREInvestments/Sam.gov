# R1.3 — Ground Truth Audit

## Method

For each real corpus project, solicitation text produced by the production intake/parser is inspected for clearly material bid requirements. Ground-truth entries record:

- category
- short summary
- source excerpt
- source document id
- mandatory/material judgment

Requirements are **not** hand-fed into the compiler. Documents go in; R1 output comes out; audit compares afterward.

## Measured results (final)

| Metric | Count |
|---|---:|
| Real projects with documents | 33 |
| Material GT flags identified | 130 |
| Captured correctly after systemic fixes | 130 |
| Unresolved material misses | 0 |
| Unsupported / fabricated material requirements | 0 |
| Provenance coverage | 100% |
| Known false COMPLETE | 0 |

## Systemic fix example

`AMENDMENT_ACK` patterns used `r"\backknowledge..."` which regex-parsed as word-boundary + `ackknowledge` (double-k). Corrected to `\backnowledge` / `\backnowledg\w*` so phrases like “Acknowledge the amendment.” extract correctly across SciQuest packages.

Additional hardening: sealed-bid submission language, company-rep signature blocks, approved-source exact-part language, quantity phrases like “Quantity Minimum four (4)”.
