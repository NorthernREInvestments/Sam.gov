# R4 — Response Firewall

Buyer-facing outputs must never contain:

| Leak class | Target |
|------------|--------|
| supplier cost | 0 |
| max-buy | 0 |
| target profit | 0 |
| margin | 0 |
| historical government price | 0 |
| financing strategy | 0 |
| supplier boilerplate | 0 |
| internal notes | 0 |

Supplier quotes default `INTERNAL_ONLY` unless buyer requires + owner approves.

Package ZIP uses an allowlist — no project dump.
