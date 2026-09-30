# R1.3 — Real Corpus Acquisition

Build: `20260929-m3-r13-real-corpus-final-validation`

## Purpose

Acquire and preserve a durable library of **real buyer-issued** government solicitation packages for final R1 compiler validation. No SAM API calls. No R2 pricing/forms/submit work.

## Location

- Corpus root: `data/response_corpus/real/`
- Manifest: `data/response_corpus/real/real_corpus_manifest.json`
- CDN/WAF cache of official files: `data/response_corpus/cache/`
- Acquisition harness: `scripts/run_r13_corpus_acquisition_validation.py`
- Store helpers: `response_engine/corpus_store.py`

## Acquisition rules

- Prefer authoritative government buyer/agency sources
- Public fetch only; mark `AUTH_REQUIRED` when login/CAPTCHA blocks access
- Browser-like User-Agent for agency CDNs that reject custom library UAs
- SHA-256 hash every document; dedupe within project
- Cache locally so regression does not depend on buyer sites staying online
- DLA PDFs blocked by WAF for urllib were preserved from the same official URLs via read-only fetch cache

## Result (this build)

- Candidates attempted: 37
- Accepted real projects: 33
- Rejected / auth-blocked / unavailable: 0 on final run (earlier DLA CDN 403s resolved via authoritative cache)
- SAM API calls: **0**

## Diversity obtained

Federal, DLA masters + RFPs, PIEE notices, SF1449 embassy RFQs, Nebraska ITBs (cameras, DASDEC, GM market basket, Leica), Iowa/Montana SciQuest packages, Phoenix OpenGov HTML, local IFBs (KY/OK/SC), buyer XLSX, Q&A/addenda, multi-amendment, ZIP assembled from Nebraska buyer files, OCR-tagged Iowa package.
