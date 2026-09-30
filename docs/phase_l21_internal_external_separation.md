# L.21 Internal / External Separation

Internal fields never in supplier packet: `['BREAK_EVEN_MAX_BUY', 'ExpectedRevenueHigh', 'ExpectedRevenueLow', 'ExpectedRevenueMid', 'MAX_BUY_FOR_10K_PROFIT', 'MAX_BUY_FOR_25K_PROFIT', 'MAX_BUY_FOR_5K_PROFIT', 'desired_profit', 'financing_assumption', 'government_historical_price', 'government_value', 'margin_ceiling', 'max_buy', 'risk_reserve', 'supplier_quote_target']`

`assert_no_internal_leak` runs on every prepared packet.
