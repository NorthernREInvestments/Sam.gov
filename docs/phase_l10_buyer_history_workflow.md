# Phase L.10 — Buyer History Workflow

## Search order (exact)

1. same buyer + exact model  
2. same buyer + exact MPN/SKU  
3. same buyer + exact product family  
4. buyer bid tabs  
5. buyer award records  
6. buyer board/council approvals  
7. buyer purchase orders  
8. buyer check/expenditure registers  
9. buyer prior solicitations  
10. buyer contract renewals/extensions  

Only after buyer-specific paths: product-history broaden, then **category benchmark last resort**.

## Persistence

Buyer retrieval memory: `data/phase_l10_buyer_retrieval_memory.json`  
(platform, bid-tab URL patterns, board packets, quantity paths)

Module: `phase_l.buyer_history_workflow`
