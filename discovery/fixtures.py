"""Fixture payloads for discovery adapter tests — synthetic, no live capture required."""

from __future__ import annotations

HTML_CITY_BIDS = """
<html><body>
<table>
<tr><td>Title</td><td>Solicitation</td><td>Deadline</td><td>Document</td></tr>
<tr>
  <td><a href="https://city.example.gov/bids/ITB-2026-100">Network Switches Equipment Purchase</a></td>
  <td>ITB-2026-100</td>
  <td>2026-12-15</td>
  <td>https://city.example.gov/docs/ITB-2026-100.pdf</td>
</tr>
<tr>
  <td><a href="https://city.example.gov/bids/RFP-99">Janitorial Services for City Hall</a></td>
  <td>RFP-99</td>
  <td>2026-11-01</td>
  <td>https://city.example.gov/docs/RFP-99.pdf</td>
</tr>
</table>
</body></html>
"""

JSON_STATE_BIDS = {
    "opportunities": [
        {
            "id": "CA-2026-441",
            "title": "Purchase of Laboratory Equipment and Supplies",
            "solicitation_number": "IFB-26-441",
            "agency": "CA Dept of General Services",
            "department": "Procurement Division",
            "state": "CA",
            "status": "OPEN",
            "response_deadline": "2026-10-30T17:00:00",
            "timezone": "America/Los_Angeles",
            "description": "Procurement of laboratory equipment for state facilities",
            "estimated_value": None,
            "documents": ["https://state.example.gov/docs/IFB-26-441.pdf"],
            "amendments": ["https://state.example.gov/docs/IFB-26-441-amd1.pdf"],
            "qa_links": [{"url": "https://state.example.gov/qa/IFB-26-441"}],
            "detail_url": "https://state.example.gov/bids/IFB-26-441",
            "submission_method": "Portal upload",
        },
        {
            "id": "CA-2026-900",
            "title": "Professional Consulting Services — IT Strategy",
            "solicitation_number": "RFP-26-900",
            "agency": "CA Dept of Technology",
            "state": "CA",
            "status": "OPEN",
            "response_deadline": "2026-09-20",
            "description": "Professional consulting services for IT strategy",
        },
    ]
}

RSS_COUNTY_BIDS = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>County Bids</title>
    <item>
      <title>HVAC Equipment for County Facilities</title>
      <link>https://county.example.gov/bids/BID-778</link>
      <guid>BID-778</guid>
      <description>Supply of HVAC equipment. Deadline: 12/01/2026</description>
      <solicitationNumber>BID-778</solicitationNumber>
    </item>
    <item>
      <title>Award Notice — Road Paving Contract</title>
      <link>https://county.example.gov/awards/A-1</link>
      <guid>A-1</guid>
      <description>Award notice for paving</description>
    </item>
  </channel>
</rss>
"""

COOP_LISTING = {
    "contracts": [
        {
            "id": "SW-12345",
            "contract_number": "SW-12345",
            "cooperative": "Fixture Sourcewell-style",
            "title": "Office Furniture and Related Equipment",
            "description": "Cooperative contract for furniture and equipment",
            "status": "OPEN",
            "url": "https://coop.example.org/contracts/SW-12345",
            "documents": ["https://coop.example.org/docs/SW-12345.pdf"],
        }
    ]
}

FEDERAL_PUBLIC_NOTICES = {
    "notices": [
        {
            "notice_id": "FED-PUB-9001",
            "title": "Servers and Network Equipment — Brand Name or Equal",
            "solicitation_number": "47QSWA26Q0001",
            "agency": "General Services Administration",
            "office": "FAS",
            "set_aside": "Total Small Business",
            "naics": "423430",
            "psc": "7025",
            "description": "Purchase of servers and network equipment for federal agency",
            "response_deadline": "2026-11-15T14:00:00",
            "timezone": "America/New_York",
            "detail_url": "https://federal.example.gov/notices/FED-PUB-9001",
            "source_url": "https://federal.example.gov/notices/FED-PUB-9001",
            "document_links": [
                {"url": "https://federal.example.gov/docs/base.pdf", "kind": "solicitation"},
                {"url": "https://federal.example.gov/docs/specs.pdf", "kind": "specifications"},
            ],
            "amendment_links": [
                {"url": "https://federal.example.gov/docs/amd1.pdf", "kind": "amendment"}
            ],
            "submission_method": "email",
        }
    ]
}

SHARED_PLATFORM = {
    "projects": [
        {
            "projectId": "BF-555",
            "projectName": "Supply and Install Classroom Interactive Displays",
            "referenceNumber": "RFP-DISP-555",
            "organizationName": "Fixture School District",
            "state": "WA",
            "city": "Seattle",
            "description": "Furnish and install interactive display equipment",
            "closeDate": "2026-10-05",
            "publicUrl": "https://platform.example.com/portal/BF-555",
            "attachments": [
                {"name": "RFP.pdf", "url": "https://platform.example.com/files/rfp.pdf", "type": "rfp"},
                {"name": "pricing.xlsx", "url": "https://platform.example.com/files/pricing.xlsx", "type": "pricing"},
            ],
            "amendments": [
                {"url": "https://platform.example.com/files/amd1.pdf"}
            ],
        }
    ]
}

# Same solicitation seen on aggregator (tier 3) then official (tier 1) for dedup tests
AGGREGATOR_MIRROR = {
    "opportunities": [
        {
            "id": "mirror-CA-2026-441",
            "title": "Purchase of Laboratory Equipment and Supplies",
            "solicitation_number": "IFB-26-441",
            "agency": "CA Dept of General Services",
            "state": "CA",
            "status": "OPEN",
            "response_deadline": "2026-10-30",
            "description": "Lab equipment",
            "detail_url": "https://aggregator.example.com/IFB-26-441",
            "source_url": "https://aggregator.example.com/IFB-26-441",
        }
    ]
}

ALL_FIXTURES = {
    "fixture_html_city_bids": HTML_CITY_BIDS,
    "fixture_json_state_bids": JSON_STATE_BIDS,
    "fixture_rss_county_bids": RSS_COUNTY_BIDS,
    "fixture_coop_sourcewell_style": COOP_LISTING,
    "fixture_federal_public_notice": FEDERAL_PUBLIC_NOTICES,
    "fixture_shared_platform_listing": SHARED_PLATFORM,
}
