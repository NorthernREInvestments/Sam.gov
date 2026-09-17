"""Run public evidence recovery for blade solicitation with injected search hits."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from public_evidence_recovery import run_public_evidence_recovery  # noqa: E402


SEARCH_HITS = [
    {
        "query": "645-DOTRFB-2975-2027 deadline",
        "title": "Starbridge mirror",
        "url": "https://starbridge.ai/rfp/tungsten-carbide-blades-for-snow-ice-removal",
        "snippet": "Closes Sep 15, 2026 1:00 p.m. CDT bidding from September 12 through September 15",
    },
    {
        "query": "645-DOTRFB-2975-2027",
        "title": "CLEATUS mirror",
        "url": "https://www.cleat.ai/government/contracts/tungsten-carbide-blades-for-snow-ice-removal-wklv",
        "snippet": "Response Deadline 09/15/2026 Matthew Barker",
    },
    {
        "query": "645-DOTRFB-2975-2027",
        "title": "BidsFactory mirror",
        "url": "https://bidsfactory.com/en/tenders/tungsten-carbide-blades-for-snowice-removal-iowa_bidopp-4f306feffe1abb91",
        "snippet": "Submission deadline September 15, 2026",
    },
    {
        "query": "645-DOTRFB-2975-2027",
        "title": "GovCB mirror",
        "url": "https://www.govcb.com/government-bids/TUNGSTEN-CARBIDE-BLADES-FOR-SNOW-ICE-AND-23890959.htm",
        "snippet": "Due Date Sep 15, 2026",
    },
    {
        "query": "Iowa DOT carbide blades RFB",
        "title": "Prior RFB 2907 Carbide Blades",
        "url": "https://www.govcb.com/government-bids/CARBIDE-BLADES-FOR-SNOW-ICE-REMOVAL-23761006.htm",
        "snippet": "645-DOTRFB-2907-2027 Carbide Blades Due Jul 22, 2026 Matthew Barker",
    },
    {
        "query": "Iowa DOT bid item information",
        "title": "Iowa DOT Bid Item Information",
        "url": "https://iowadot.gov/consultants-contractors/contracts/general-letting-information/bid-item-information",
        "snippet": "Bid item descriptions and item master",
    },
]


def main() -> int:
    results = run_public_evidence_recovery(
        authorize_live=True,
        max_http=50,
        target_http=35,
        search_hits=SEARCH_HITS,
    )
    print(
        json.dumps(
            {
                "request_counts": results.get("request_counts"),
                "deadline": results.get("deadline_reconciliation"),
                "portal": results.get("portal_registration"),
                "economics_status": (results.get("preliminary_economics") or {}).get("status"),
                "answers": results.get("answers"),
                "commercial_count": len(results.get("commercial_matches") or []),
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
