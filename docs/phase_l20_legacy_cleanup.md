# L.20 Legacy Cleanup

Canonical supplier pipeline:
- channels: `discovery.manufacturer_channels`
- profiles: `discovery.supplier_profiles`
- grading: `phase_l.quality_audit.grade_supplier`
- upgrade: `phase_l.supplier_upgrade.run_supplier_upgrade_loop`
- pricing: `phase_l.acquisition_pricing.research_acquisition_price`
- financing: `phase_l.pilot_real_world.screen_financing`

Marketplace listings capped at Supplier D. MSRP ≠ acquisition cost.
