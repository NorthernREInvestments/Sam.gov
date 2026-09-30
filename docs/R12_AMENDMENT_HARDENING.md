# R1.2 Amendment Hardening

**Module:** `response_engine/amendment_diff.py`

## Approach

1. Structural text/paragraph + numeric token diff  
2. Workbook cell/sheet diff for buyer templates  
3. Field candidate extraction (deadline, qty, submission method, forms, …)  
4. Semantic classification only to label categories — low confidence → `AMENDMENT_CHANGE_REVIEW_REQUIRED`  

Same-filename hash change supersedes prior CONTROLLING doc and attaches diff; material changes mark `PACKAGE_STALE_DUE_TO_AMENDMENT`.
