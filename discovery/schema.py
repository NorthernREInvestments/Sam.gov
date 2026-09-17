"""Canonical opportunity schema + source adapter interface."""

from __future__ import annotations
from application_clock import now_utc

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Protocol

from discovery.constants import SOURCE_TYPES, TIER_3


@dataclass
class CanonicalOpportunity:
    """Normalized opportunity — same shape for federal/state/local/cooperative."""

    external_id: str
    source_id: str
    source_url: str | None = None
    detail_url: str | None = None
    title: str = "Untitled"
    solicitation_number: str | None = None
    agency: str | None = None
    subagency: str | None = None
    jurisdiction: str | None = None
    buyer_type: str | None = None
    state_code: str | None = None
    city: str | None = None
    posted_date: date | None = None
    response_deadline: datetime | None = None
    deadline_raw: str | None = None
    deadline_timezone: str | None = None
    deadline_tz_confidence: str | None = None  # KNOWN | UNKNOWN
    status: str = "OPEN"
    procurement_method: str | None = None
    set_aside: str | None = None
    naics: str | None = None
    psc: str | None = None
    commodity_codes: list[str] = field(default_factory=list)
    description: str | None = None
    estimated_value: str | None = None
    estimated_value_status: str = "UNKNOWN"
    contact: dict[str, Any] = field(default_factory=dict)
    document_links: list[dict[str, Any]] = field(default_factory=list)
    amendment_links: list[dict[str, Any]] = field(default_factory=list)
    qa_links: list[dict[str, Any]] = field(default_factory=list)
    submission_method: str | None = None
    source_updated_at: datetime | None = None
    trust_tier: int = TIER_3
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    content_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("posted_date",):
            if isinstance(d.get(k), date):
                d[k] = d[k].isoformat()
        for k in ("response_deadline", "source_updated_at"):
            if isinstance(d.get(k), datetime):
                d[k] = d[k].isoformat()
        return d

    def to_gt_contract_fields(self) -> dict[str, Any]:
        """Map into gt_contracts upsert shape — source-agnostic."""
        notice = self.external_id
        if not notice.startswith(("disc-", "fed-", "st-", "loc-", "coop-")):
            notice = f"disc-{self.source_id}-{self.external_id}"
        notice = notice[:120]
        due = None
        if self.response_deadline:
            due = self.response_deadline.date().isoformat()
        return {
            "notice_id": notice,
            "title": self.title[:512],
            "agency": self.agency,
            "location": self.city or self.state_code or self.jurisdiction,
            "naics_code": self.naics,
            "set_aside": self.set_aside,
            "due_date": due,
            "link": self.detail_url or self.source_url,
            "description": self.description,
            "estimated_value": self.estimated_value if self.estimated_value_status != "UNKNOWN" else None,
            "sam_raw": {
                "_discovery_source_id": self.source_id,
                "_solicitation_number": self.solicitation_number,
                "_buyer_type": self.buyer_type,
                "_jurisdiction": self.jurisdiction,
                "_trust_tier": self.trust_tier,
                "_canonical": self.to_dict(),
                "solicitationNumber": self.solicitation_number,
                "classificationCode": self.psc,
            },
        }


class SourceAdapter(Protocol):
    """Adapter interface for discovery sources."""

    source_id: str
    source_name: str
    source_type: str
    jurisdiction: str | None
    discovery_method: str
    adapter_status: str
    trust_tier: int

    def preflight(self, **kwargs: Any) -> dict[str, Any]:
        ...

    def parse_fixture(self, payload: str | bytes | dict[str, Any], **kwargs: Any) -> list[CanonicalOpportunity]:
        """Parse fixture HTML/JSON/XML — no network. Primary test path."""
        ...

    def fetch(self, *, authorize_live: bool = False, **kwargs: Any) -> list[CanonicalOpportunity]:
        """Live fetch — refused unless authorize_live and policy allows."""
        ...


def utc_now() -> datetime:
    return now_utc()


def validate_source_type(source_type: str) -> str:
    st = str(source_type or "").upper()
    if st not in SOURCE_TYPES:
        return "OTHER_PUBLIC"
    return st
