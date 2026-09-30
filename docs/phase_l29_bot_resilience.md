# Phase L.2.9 — Bot Resilience

## Rule

Bot block ≠ `PRICE_NOT_AVAILABLE`.

Bot block = `PRIMARY_SOURCE_BLOCKED`, then exhaust:

1. structured/API sources  
2. static PDF/XLSX/CSV  
3. product sitemap / synthesized product URLs  
4. optional public browser render flag (`PUBLIC_BROWSER_RENDER_REQUIRED`) — not CAPTCHA solve  
5. OEM alternate URL  
6. authorized distributor / alternate dealer  
7. government / cooperative / state contract sheets  
8. board purchase / bid tab  
9. indexed strong price lead  
10. cached known-product price  
11. manual verification queue  

Only after alternatives fail: `PRICE_NOT_RECOVERED_AUTOMATICALLY`.

## Explicitly forbidden

- CAPTCHA solving  
- credential circumvention  
- rotating identities to defeat access controls  
- fingerprint spoofing for anti-bot evasion  
- unauthorized proxy networks  
- login scraping without authorization  

## Soft-block detection (L.2.8/L.2.9)

Captcha interstitial, “Just a moment…”, “Whoops we couldn’t find that”, sterile href shells → treated as blocked and routed to alternates.
