# M3 2.0 — System Architecture (Build 001)

**Build:** `20261120-m3-2-build-001`  
**Style:** Modular monolith on FastAPI + PostgreSQL + server-rendered UI  
**Principle:** Research & business operations OS — not a procurement data warehouse

---

## 1. Mission boundary

M3 knows *where* information lives, *how* to retrieve and verify it, and *how* to turn it into actionable commercial work. It retrieves public procurement data **on demand**, caches briefly when needed, and **persists only business-owned records**.

**Out of scope for Build 001:** financing engines, automated bidding, Outlook/Graph, paid APIs, SAM live, fabricated profitability scoring.

---

## 2. Module map (eventual → Build 001)

| # | Capability | Build 001 | Authoritative store |
|---|------------|-----------|---------------------|
| 01 | Authentication | **Yes** | `users`, sessions |
| 02 | RBAC / permissions | **Yes** | `roles`, `permissions`, `user_permissions` |
| 03 | Audit history | **Yes** | `audit_events` (append-only) |
| 04 | Tasks / assignments | **Yes** | `tasks` |
| 05 | Research orchestration | **Yes (shared)** | `research_runs` |
| 06 | Source discovery / registry | **Yes** | `source_registry` |
| 07 | Buyer intelligence | Stub relations | `buyers` (minimal) |
| 08 | Live solicitation discovery | **Yes** | ephemeral results + `opportunities` (saved only) |
| 09 | Historical awards | Deferred | — |
| 10 | Forecasts | Deferred | — |
| 11 | Document analysis | Deferred (link only) | — |
| 12 | Supplier discovery | **Yes** | research results → `suppliers` |
| 13 | Supplier CRM | **Yes** | `suppliers`, contacts, notes, applications |
| 14 | Catalog ingestion | **Yes** | `catalog_uploads`, `catalog_products` |
| 15 | Product identity / matching | Foundation | `catalog_products` MPN index; exact match only |
| 16–20 | Pricing / profit / financing / eligibility | Deferred (rules documented) | config placeholder |
| 21–25 | Bid / contract / finance / performance / Outlook | Deferred | interface stubs in docs only |

One implementation per capability. One record per business entity. No parallel research engines.

---

## 3. Data ownership

```
Users ──┐
        ├──► AuditEvents (immutable)
        ├──► Tasks
Roles/Permissions

ResearchRuns ──► cites SourceRegistry entries
             ──► produces OpportunitySearchResults (session/cache)
             ──► produces SupplierSearchResults (session/cache)

Opportunities (saved) ← operator save ← discovery results
Suppliers (CRM) ← manual | discovery save
  ├── SupplierContacts
  ├── SupplierNotes / FollowUps
  ├── SupplierApplications (reseller | credit | dealer — separate statuses)
  ├── CatalogUploads → CatalogProducts
  └── Assignments → Users

SearchProfiles (saved filters) owned by Users
```

**Persistence rule:** discovery hits are transient unless the operator saves them into Opportunities / Suppliers.

---

## 4. Research framework

Single package: `m3.research`

- `ResearchObjective` enum (contracts, suppliers, sources, …)
- `SourceAdapter` protocol: `probe()`, `search()`, `fetch_detail()`
- `SourceRegistry` DB + seed free official sources
- Bounded HTTP client: timeouts, retries, robots-respecting User-Agent, SSRF allowlist (https public hosts only)
- Result envelope: `{ items, evidence_urls, verified_at, limitations, source_id }`

Adapters for Build 001 (free only):

1. **Socrata / open-data city boards** (NYC, Chicago-style JSON where public)
2. **State portal HTML/JSON listings** (NE, IA, CO, KS, MO, SD public pages)
3. **OpenGov public project pages** (unauthenticated listings)
4. **Manufacturer / distributor public pages** (supplier discovery scrape of contact/reseller URLs)

SAM.gov, BidNet, Euna, GovSpend, paid SERP: **disabled** until owner authorization.

---

## 5. Auth & security

- Session cookies (HTTP-only, Secure, SameSite=Lax)
- Passwords: Argon2 / bcrypt
- Server-side permission checks on every mutating route
- Owner bootstrapped via `M3_OWNER_EMAIL` + `M3_OWNER_PASSWORD` env (or first-run setup if unset — never hardcoded)
- Audit never stores secrets
- Uploads: size/type allowlist, stored under `/data/uploads` on Railway volume
- External fetch: block private IP ranges (SSRF)

Roles (seed): `owner`, `supplier_manager`, `contract_specialist` + configurable extras.

---

## 6. UI / UX

Server-rendered Jinja2 + progressive CSS. Nav:

- Home · Opportunities · Suppliers · Tasks · Settings

Mobile-first breakpoints verified at 360 / 390 / 768 / 1024 / 1440.

Role dashboards share the same records; differ only by default filters and primary actions.

---

## 7. Deployment

- Existing Railway service + Postgres + GitHub integration
- Dockerfile runs uvicorn
- `M3_DATA_ROOT=/data` for uploads/cache
- **No deploy-time DB/volume wipe**
- Migrations via Alembic on startup (safe, idempotent)

---

## 8. Release roadmap

| Build | Focus |
|-------|--------|
| 001 / 001A–C | Auth, audit, research core, CRM, catalogs, team UX |
| **002 (this)** | OpenAI Responses web_search + budget manager, nationwide pagination, keyword search, historical awards, supplier recruitment, commercial evidence |
| 003 | Exact catalog↔contract matching UI, quote RFQ drafts |
| 004 | Eligibility/registration tracking |
| 005 | Verified economics + financing gates |
| 006 | Bid prep / owner approval |
| 007 | Outlook Graph (owner authorized) |
| 008 | Contract execution & financial BI |

---

## 9. Commercial fact types (never conflate)

| Fact | Meaning |
|------|---------|
| Catalog price | Published list — not negotiated |
| Supplier quote | Verified acquisition only when documented |
| Historical award | Past government price — not future revenue |
| Estimated value | Procurement size estimate — not profit |
| Proposed bid | M3 offer — not award |
| Exact MPN match | Identity confirmed |
| Potential equivalent | Unverified |
