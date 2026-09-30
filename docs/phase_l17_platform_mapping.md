# L.17 Platform Mapping

Top platforms by jurisdiction unlock:

```json
[
  {
    "platform": "StateOwned",
    "jurisdiction_count": 23,
    "sample_jurisdiction_ids": [
      "AZ:STATE:arizona",
      "CA:STATE:california",
      "CT:STATE:connecticut",
      "FL:STATE:florida",
      "IL:STATE:illinois",
      "IN:STATE:indiana",
      "KS:STATE:kansas",
      "MA:STATE:massachusetts",
      "MN:STATE:minnesota",
      "MO:STATE:missouri",
      "NV:STATE:nevada",
      "NJ:STATE:new_jersey",
      "NM:STATE:new_mexico",
      "ND:STATE:north_dakota",
      "OH:STATE:ohio"
    ],
    "adapter_status": "VARIES",
    "known_auth_issues": false,
    "engineering_priority_hint": 2.3
  },
  {
    "platform": "BidNet",
    "jurisdiction_count": 20,
    "sample_jurisdiction_ids": [
      "NY:STATE:new_york",
      "FL:COUNTY:12011:broward_county",
      "FL:COUNTY:12086:miami_dade_county",
      "IL:COUNTY:17031:cook_county",
      "VA:COUNTY:51059:fairfax_county",
      "IL:CITY:1714000:chicago_city",
      "MN:CITY:2743000:minneapolis_city",
      "NM:CITY:3502000:albuquerque_city",
      "NC:CITY:3712000:charlotte_city",
      "OH:CITY:3918000:columbus_city",
      "OR:CITY:4159000:portland_city",
      "TX:CITY:4805000:austin_city",
      "WI:CITY:5553000:milwaukee_city",
      "IL:TRANSIT:chicago_transit_authority",
      "NY:TRANSIT:mta_new_york"
    ],
    "adapter_status": "VARIES",
    "known_auth_issues": true,
    "engineering_priority_hint": 2.0
  },
  {
    "platform": "SimpleHTML",
    "jurisdiction_count": 16,
    "sample_jurisdiction_ids": [
      "AR:STATE:arkansas",
      "GA:STATE:georgia",
      "ID:STATE:idaho",
      "LA:STATE:louisiana",
      "ME:STATE:maine",
      "MD:STATE:maryland",
      "MS:STATE:mississippi",
      "NE:STATE:nebraska",
      "NH:STATE:new_hampshire",
      "NC:STATE:north_carolina",
      "OK:STATE:oklahoma",
      "PA:STATE:pennsylvania",
      "RI:STATE:rhode_island",
      "TX:STATE:texas",
      "VT:STATE:vermont"
    ],
    "adapter_status": "VARIES",
    "known_auth_issues": false,
    "engineering_priority_hint": 1.6
  },
  {
    "platform": "Socrata",
    "jurisdiction_count": 10,
    "sample_jurisdiction_ids": [
      "MD:COUNTY:24031:montgomery_county",
      "NY:CITY:nyc_city_record_online_solicitations",
      "NY:CITY:nyc_m_wbe_upcoming_procurements",
      "CA:CITY:los_angeles_ramp_open_bid_opportunities",
      "LA:CITY:baton_rouge_purchase_orders_and_contracts",
      "NY:CITY:nyc_discretionary_contract_awards",
      "IL:CITY:city_of_chicago_contracts",
      "TX:CITY:city_of_austin_contracts",
      "VA:CITY:richmond_va_city_contracts",
      "MA:CITY:cambridge_ma_contracts_bid_list"
    ],
    "adapter_status": "VARIES",
    "known_auth_issues": false,
    "engineering_priority_hint": 1.0
  },
  {
    "platform": "OpenGov",
    "jurisdiction_count": 10,
    "sample_jurisdiction_ids": [
      "NC:COUNTY:37183:wake_county",
      "WA:COUNTY:53033:king_county",
      "AZ:CITY:0455000:phoenix_city",
      "CA:CITY:0664000:sacramento_city",
      "MA:CITY:2507000:boston_city",
      "NC:CITY:3755000:raleigh_city",
      "TX:CITY:4865000:san_antonio_city",
      "WA:CITY:5363000:seattle_city",
      "CA:PUBLIC_UNIVERSITY:university_of_california_procurement",
      "MI:PUBLIC_UNIVERSITY:university_of_michigan"
    ],
    "adapter_status": "VARIES",
    "known_auth_issues": true,
    "engineering_priority_hint": 0.2
  },
  {
    "platform": "Bonfire",
    "jurisdiction_count": 9,
    "sample_jurisdiction_ids": [
      "AZ:COUNTY:04013:maricopa_county",
      "TX:COUNTY:48201:harris_county",
      "CO:CITY:0820000:denver_city",
      "GA:CITY:1304000:atlanta_city",
      "TX:CITY:4835000:houston_city",
      "TX:SCHOOL_DISTRICT:houston_independent_school_district",
      "TX:PUBLIC_UNIVERSITY:university_of_texas_at_austin",
      "TX:AIRPORT:dallas_fort_worth_international_airport",
      "TX:SCHOOL_DISTRICT:houston_isd_bonfire"
    ],
    "adapter_status": "VARIES",
    "known_auth_issues": false,
    "engineering_priority_hint": 0.18
  },
  {
    "platform": "Jaggaer",
    "jurisdiction_count": 8,
    "sample_jurisdiction_ids": [
      "AL:STATE:alabama",
      "CO:STATE:colorado",
      "DE:STATE:delaware",
      "IA:STATE:iowa",
      "KY:STATE:kentucky",
      "MI:STATE:michigan",
      "MT:STATE:montana",
      "TX:PUBLIC_UNIVERSITY:texas_a_m_jaggaer_public"
    ],
    "adapter_status": "VARIES",
    "known_auth_issues": false,
    "engineering_priority_hint": 0.8
  },
  {
    "platform": "PlanetBids",
    "jurisdiction_count": 7,
    "sample_jurisdiction_ids": [
      "CA:CITY:0643000:long_beach_city",
      "CA:CITY:0644000:los_angeles_city",
      "CA:CITY:0666000:san_diego_city",
      "CA:SCHOOL_DISTRICT:los_angeles_unified_school_district",
      "CA:AIRPORT:los_angeles_world_airports",
      "CA:PUBLIC_UTILITY:los_angeles_department_of_water_and_power",
      "CA:AIRPORT:lawa_planetbids_portal"
    ],
    "adapter_status": "VARIES",
    "known_auth_issues": false,
    "engineering_priority_hint": 0.7
  }
]
```

Priority metric: `JurisdictionsUnlocked × CommercialYield ÷ EngineeringBurden`
