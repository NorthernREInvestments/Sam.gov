# Permanent Solicitation Research Workflow

**DISCOVER → SAVE → READ → UNDERSTAND → INVESTIGATE MISSING → VERIFY → PROMOTE OR REJECT**

Canonical code paths: `solicitation_sop.py`, `local_evidence.py`, `document_inventory.py`, `ai_output_verify.py`, `document_recovery_run.py`, NextBid controller gates.

## Stages

1. **DISCOVER** — Provider id, agency solicitation #, title, agency, URLs, dates, status, document counts, amendments. Save unknowns as unknown.
2. **SAVE** — Persist full raw response/body before interpretation (`nextbid_paid_responses`, manifests, bytes).
3. **READ body** — Classify product vs service/construction/install/mixed/unclear from substantive text. Conclusive unsuitable → reject early (no further paid docs). Uncertain → continue.
4. **UNDERSTAND known** — Reuse local evidence; do not rebuy.
5. **MISSING list** — Specific missing fields/docs (not vague “needs research”).
6. **INVESTIGATE docs** — Inventory attachments; note contractual docs beyond product schedule.
7. **RETRIEVE** — Order: saved bytes → verified extracted text → cached responses/manifests → free official URLs → authorized provider access → authorized paid download → manual upload.
8. **UNDERSTAND package** — Cross-document requirements.
9. **VERIFY** — Evidence gates; amendments; duplicates; qty/UoM; obligations.
10. **STATUS** — Investigate / reject / blocked / supplier-pricing / bid-ready. AI cannot bypass gates.

## Stop / continue / authorize

| Condition | Action |
| --- | --- |
| Conclusive unsuitable service/construction/install | Reject; fingerprint; stop paid spend |
| Verified deadline passed | Leave actionable queues; 48h retention; archive per policy |
| Manifest present, bytes missing | Continue only with owner credit authorization |
| Emergency stop / zero budget | Offline reuse only |
| Live OpenAI | Only when `openai_enabled` + spend authorized |
| Partial line items ready, others unknown | `partial_supplier_quote` — not full ready |

## Promotion authority

`ai_output_verify.verify_analysis_against_sources` is the shared readiness authority. Dashboards/queues must consume its promotion flags / blockers.
