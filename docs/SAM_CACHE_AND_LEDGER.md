# SAM Cache and Ledger

- Ledger: `artifacts/sam/sam_call_ledger.json` (persists across restarts)
- Cache: `artifacts/sam/response_cache/<fingerprint>.json`
- Lock: `artifacts/sam/.sam_budget.lock` (multi-process safety)
- Productivity: `artifacts/sam/sam_query_productivity.json`

Fingerprint = sha256(endpoint + params without api_key).
