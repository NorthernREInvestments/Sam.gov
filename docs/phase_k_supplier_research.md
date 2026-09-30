# Phase K — Supplier Research (Public Only)

**Hard stop:** No contact, quotes, emails, calls, or bid submissions.  
**Rule:** Random web listings ≠ government-compliant sources for defense items.

---

## 1. Pipefitter toolkit — NSN 5180-00-596-1509

### Product nature
Army supply catalog kit (`SC5180-90-CL-N42` / PN `0684A0000:59678`). Typically **assembled to TDP**, not a single OEM shelf SKU.

### Channels (priority order)

| Priority | Channel | Evidence | Authenticity | Notes |
|----------|---------|----------|--------------|-------|
| 1 | Kit assembler with MIL packaging / IPI capability | Catalog kit structure; IFB IPI = 3 units | Approved kit configuration path | Best fit to solicitation text |
| 2 | Hand-tool / industrial distributors who will assemble to SC5180-90CLN42 | Public NSN depot / AeroBase / WBParts RFQ pages | Independent / quote-required | Must match exact kit config |
| 3 | Established government suppliers of prior NSN awards | Phase J history awardee on SPRDL119F0107 | Prior gov supplier | May not still stock |
| 4 | Commercial reseller of “pipefitter tools” | Generic e-commerce | **Reject as primary** | Not exact kit |

### Public pricing
None reliable for exact kit configuration. Listings are **quote-required**.

### Stock / lead time
Unknown without outreach. IPI + production packaging implies lead time beyond “ship from stock.”

### Shortlist (research identities only — do not contact)
1. Prior SPRDL119F0107 awardee (identity from USAspending / Phase J packet)
2. Defense kit assemblers advertising SC5180 / pipefitter tool kits
3. Authorized tool distributors willing to build to Army catalog BOM + MIL-STD-2073 packaging

### Assessment
**Channel exists in principle but is not simple resale.** `VERIFY_SUPPLIER_CHANNEL` — manufacturing/assembly path must be chosen before outreach.

---

## 2. Turbine support — NSN 2840-00-411-8852

### Product nature
Aircraft gas turbine support (T-56), PN `6870409`, **source-controlled / critical**.

### Approved sources (from solicitation summaries)
| Source | CAGE | Role |
|--------|------|------|
| Rolls-Royce | 63005 | OEM / approved |
| Electro Methods | 33617 | Approved source |
| Pacific Sky Supply | 66905 | Approved source |

### Channels
| Priority | Channel | Viable? |
|----------|---------|---------|
| OEM / approved source | Required | Only path |
| Authorized distributor of approved source | Possible if ASL allows | Weak without ASL |
| Broker / surplus | | **Reject** for compliant bid |
| Commercial reseller listing | | **Reject** |

### Assessment
**No realistic reseller path.** Deal expired for calendar reasons; even if open → `VERIFY_SOURCE_APPROVAL` / not executable for current company.

---

## 3. Diesel engine — NSN 2815-01-536-9262

### Product nature
HATZ diesel, PN `1D81C-24VOLT-MMC-SPEC323`, CAGE **61080**. Solicitation restricts to HATZ or **authorized distributors**.

### Channels
| Priority | Channel | Viable? |
|----------|---------|---------|
| HATZ Diesel of America | OEM | Only if company is HATZ |
| HATZ authorized distributor | Auth distributor | Only with written authorization |
| “Similar” commercial HATZ engines | | **Reject** — configuration must match MMC SPEC |
| Broker / reman uncertain | | **Reject** without reman/new rule + auth |

### Public pricing
None for exact MMC-SPEC323 configuration suitable for government bid.

### Assessment
**Supplier path = OEM-restricted.** Company lacks confirmed authorization → do not outreach as reseller.

---

## 4. Transmission kit — NSN 2520-01-682-2226

### Product nature
PN `12591930` kit under **TDP Distribution Statement D** (DoD/U.S. contractors only with need-to-know). FAT required.

### Channels
| Priority | Channel | Viable? |
|----------|---------|---------|
| OEM / TDP holder manufacturing kit | Required | Needs Dist-D access |
| Approved source on TDP | Required | Same |
| Industrial distributor without TDP | | **Reject** |
| Broker / surplus | | **Reject** for FAT/TDP IFB |

### Public pricing
None for Dist-D controlled kit. Phase J AGED award ($19,520.80/EA on 100) is economics basis only.

### Assessment
**Weak/none for open commercial sourcing.** Financing scale dominates even if TDP access obtained.

---

## Cross-deal authenticity rule applied

| Deal | Acceptable authenticity | Current company fit |
|------|-------------------------|---------------------|
| Pipefitter | Kit assembler / gov supplier with TDP-A | Unproven |
| Turbine | OEM / ASL only | No |
| Diesel | HATZ / auth distributor only | No |
| Transmission | TDP-D manufacturer / approved | No |

**Broker-only paths:** not used for any of the four.
