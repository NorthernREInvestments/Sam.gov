# R4 — Buyer Forms

Buyer-supplied PDFs/DOCX/XLSX are **immutable originals**. R4 writes `WORKING_COPY` / `GENERATED_RESPONSE_COPY` only.

## PDF

- AcroForm fill when `pypdf` available
- Signature fields never filled → `OWNER_SIGNATURE_REQUIRED`
- Non-fillable / missing binary → `MANUAL_GENERATION_REQUIRED` or bidder-info DOCX fallback

## DOCX

Populate intended response locations only; preserve styles/tables. Signature line left for owner.

## Precedence

Buyer template **overrides** any M3 prettier substitute.
