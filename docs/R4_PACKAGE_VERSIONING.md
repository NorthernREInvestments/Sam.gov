# R4 — Package Versioning

Storage: `data/response_projects/{id}/generated/v{N}/`

- `originals/` — immutable buyer binaries  
- `buyer_facing/` — clean draft outputs  
- `internal/` — hash manifest (not in buyer ZIP)

Stale reasons: amendment, product, price, company profile.  
Selective regeneration via dependency awareness; prior versions preserved (never silent overwrite).
