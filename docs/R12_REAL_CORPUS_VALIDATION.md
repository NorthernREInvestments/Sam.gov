# R1.2 Real Corpus Validation

**Build:** `20260929-m3-r12-production-corpus-ocr-amendment-hardening`  
**Script:** `scripts/run_r12_corpus_validation.py`

## Method

- Exhaust unique real solicitation PDFs under evidence + `_temp_live_autonomous` (hash-deduped)
- Include companion `ViewSourcingEvent` HTML where present
- Supplement with stored city/state HTML artifacts (labeled `REAL_HTML`)
- Manual/semi-auto requirement audit on real cases
- **0 SAM API calls**

## Artifacts

`artifacts/response_engine/r12_*.json`

## Honest limits

Fewer than 15 unique buyer PDFs may exist on disk. Report actual count; do not fabricate packages.
