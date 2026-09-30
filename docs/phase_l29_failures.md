# Phase L.2.9 — Failures

Typical failure classes after expansion:

| Class | Meaning |
|-------|---------|
| `NO_PRICE_INFORMATION` | No identity-tied public price across expanded paths |
| `PRIMARY_SOURCE_BLOCKED` | Storefront blocked; alternates attempted |
| `PRICE_NOT_RECOVERED_AUTOMATICALLY` | Alternates exhausted |
| `STRONG_PRICE_LEAD_REQUIRES_VERIFICATION` | Lead exists; page verify failed |
| `SOURCE_RESTRICTED` / `AUTH_REQUIRED` | Portal needs gov credentials / login |
| `FETCH_TIMEOUT` | Per-row wall clock (bounded) |

Remaining bottleneck (expected): many mil-spec NSN lines lack public commercial storefront prices; OEM/distributor HTML remains captcha-heavy; static gov/coop PDFs are the highest-leverage recovery path.
