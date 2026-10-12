"""Deadline extraction repair — recover closing dates from existing evidence.

Does NOT rediscover the universe. Applies staged BidNet sidecars, list-recovery
results, title-embedded Closing dates, and broader structured field promotion.
Never overwrites a verified existing deadline with an unverified extraction.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from application_clock import now_utc
from discovery.deadline import normalize_deadline
from m3_data_root import data_path
from phase_l.l23_full_population_funnel import load_store, save_store

log = logging.getLogger("govtracker.deadline_extraction_repair_v1")

BUILD = "20261012-m3-deadline-extraction-repair-v1"
OUT = "_og_commercial_research_pipeline_out"
REPORT = f"{OUT}/m3_der_v1_report.json"
BEFORE = f"{OUT}/m3_der_v1_before_counts.json"
AFTER = f"{OUT}/m3_der_v1_after_counts.json"
SAMPLES = f"{OUT}/m3_der_v1_manual_sample50.json"
CHECKPOINT = f"{OUT}/m3_der_v1_checkpoint.json"

OPEN_CONFIRMED = "OPEN_CONFIRMED"
CLOSED_CONFIRMED = "CLOSED_CONFIRMED"
DEADLINE_UNKNOWN = "DEADLINE_UNKNOWN"
DEADLINE_CONFLICT = "DEADLINE_CONFLICT"
CANCELLED = "CANCELLED"

# Title / description phrases that mean response/bid closing (not Q&A / site visit alone)
_TITLE_CLOSING_RE = re.compile(
    r"(?:Closing|Bids?\s+due|Quotes?\s+due|Proposals?\s+due|Submission\s+deadline|"
    r"Bid\s+opening|Response\s+due|Offers?\s+due)\s*[:\-]?\s*"
    r"(\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM))?(?:\s+[A-Z]{2,5})?)",
    re.I,
)

# Structured aliases observed in real source payloads (not guessed blindly)
_STRUCTURED_KEYS = (
    "responseDeadLine",
    "responseDeadline",
    "response_deadline",
    "deadline",
    "deadline_raw",
    "closing_date_raw",
    "close_date_raw",
    "closeDate",
    "closingDate",
    "closing_date",
    "bidDueDate",
    "dueDate",
    "due_date",
    "submissionDeadline",
    "proposalDeadline",
    "endDate",
)


def _utc() -> str:
    return now_utc().isoformat()


def _save(rel: str, obj: Any) -> Path:
    path = Path(data_path(rel))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    return path


def _load(rel: str) -> Any:
    path = Path(data_path(rel))
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    try:
        from dateutil import parser as dp

        d = dp.isoparse(s) if "T" in s or s.endswith("Z") else None
        if d is None:
            nd = normalize_deadline(s)
            raw = nd.get("utc_deadline") or nd.get("parsed_local")
            if raw is None:
                return None
            if isinstance(raw, datetime):
                return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
            d = dp.isoparse(str(raw))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        try:
            nd = normalize_deadline(s)
            raw = nd.get("utc_deadline") or nd.get("parsed_local")
            if not raw:
                return None
            from dateutil import parser as dp

            d = dp.isoparse(str(raw)) if not isinstance(raw, datetime) else raw
            return d if getattr(d, "tzinfo", None) else d.replace(tzinfo=timezone.utc)
        except Exception:
            return None


def collect_structured_deadline_candidates(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk record + nested dicts for known deadline field names. Evidence only."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def consider(raw: Any, *, path: str, kind: str) -> None:
        if raw is None or str(raw).strip() == "":
            return
        key = f"{path}|{raw}"
        if key in seen:
            return
        seen.add(key)
        nd = normalize_deadline(str(raw).strip())
        if nd.get("not_stated"):
            found.append(
                {
                    "path": path,
                    "kind": kind,
                    "raw": str(raw).strip(),
                    "normalized": None,
                    "not_stated": True,
                    "timezone": nd.get("timezone"),
                }
            )
            return
        norm = nd.get("utc_deadline") or nd.get("parsed_local")
        if not norm:
            return
        found.append(
            {
                "path": path,
                "kind": kind,
                "raw": str(raw).strip(),
                "normalized": norm.isoformat() if isinstance(norm, datetime) else str(norm),
                "not_stated": False,
                "timezone": nd.get("timezone"),
                "timezone_confidence": nd.get("timezone_confidence"),
            }
        )

    bags: list[tuple[str, dict[str, Any]]] = [("", rec)]
    for nest_key in (
        "row_ref",
        "sam_raw",
        "bidnet_recovery",
        "opengov_recovery",
        "raw_metadata",
        "deep_research",
    ):
        nested = rec.get(nest_key)
        if isinstance(nested, dict):
            bags.append((nest_key, nested))
            br = nested.get("bidnet_deadline_recovery")
            if isinstance(br, dict):
                bags.append((f"{nest_key}.bidnet_deadline_recovery", br))

    for prefix, bag in bags:
        for k in _STRUCTURED_KEYS:
            if k in bag and bag.get(k) not in (None, ""):
                path = f"{prefix}.{k}" if prefix else k
                consider(bag.get(k), path=path, kind="structured")
    return found


def extract_title_closing(rec: dict[str, Any]) -> dict[str, Any] | None:
    title = str(rec.get("title") or "")
    m = _TITLE_CLOSING_RE.search(title)
    if not m:
        desc = str(rec.get("description") or "")[:500]
        m = _TITLE_CLOSING_RE.search(desc)
        if not m:
            return None
        src = "description"
    else:
        src = "title"
    raw = m.group(1).strip()
    nd = normalize_deadline(raw)
    if nd.get("not_stated") or not (nd.get("utc_deadline") or nd.get("parsed_local")):
        return None
    norm = nd.get("utc_deadline") or nd.get("parsed_local")
    return {
        "path": f"{src}.Closing",
        "kind": "title_embedded_closing",
        "raw": raw,
        "normalized": norm.isoformat() if isinstance(norm, datetime) else str(norm),
        "not_stated": False,
        "timezone": nd.get("timezone"),
        "timezone_confidence": nd.get("timezone_confidence"),
    }


_SOL_IN_URL_RE = re.compile(r"/(\d{9,14})(?:/|\?|$)")


def _bidnet_sol_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = _SOL_IN_URL_RE.search(str(url))
    return m.group(1) if m else None


def _index_store_for_deadline_remap(store: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Map BidNet solicitation numbers / titles → current canonical IDs."""
    by_key: dict[str, str] = {}
    for oid, rec in store.items():
        if not isinstance(rec, dict):
            continue
        sol = str(rec.get("solicitation_event_id") or "").strip()
        if sol:
            by_key[f"sol:{sol}"] = oid
            digits = re.sub(r"\D", "", sol)
            if digits:
                by_key[f"sol:{digits}"] = oid
        url = str(rec.get("authoritative_url") or "")
        sol_url = _bidnet_sol_from_url(url)
        if sol_url:
            by_key[f"sol:{sol_url}"] = oid
        title = re.sub(r"\s+", " ", str(rec.get("title") or "").strip().lower())
        if title:
            by_key[f"title:{title}"] = oid
    return by_key


def _remap_staged_oid(
    old_oid: str,
    patch: dict[str, Any],
    *,
    index: dict[str, str],
    title: str | None = None,
) -> str | None:
    url = patch.get("evidence_url") or patch.get("source_url")
    sol = _bidnet_sol_from_url(str(url) if url else None)
    if sol and f"sol:{sol}" in index:
        return index[f"sol:{sol}"]
    if title:
        t = re.sub(r"\s+", " ", title.strip().lower())
        if f"title:{t}" in index:
            return index[f"title:{t}"]
    return None


def load_staged_recovery_evidence(
    store: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load previously recovered BidNet deadlines; remap to current canonical IDs."""
    raw_out: list[dict[str, Any]] = []

    sidecar = _load(f"{OUT}/m3_bidnet_deadline_recovery_sidecar_staged_v1.json") or {}
    patches = sidecar.get("patches") or {}
    if isinstance(patches, dict):
        for oid, patch in patches.items():
            if not isinstance(patch, dict):
                continue
            if patch.get("deadline") or patch.get("deadline_raw"):
                meta = patch.get("raw_metadata.bidnet_deadline_recovery") or {}
                raw_out.append(
                    {
                        "old_oid": str(oid),
                        "deadline": patch.get("deadline"),
                        "deadline_raw": patch.get("deadline_raw"),
                        "deadline_timezone": patch.get("deadline_timezone"),
                        "deadline_tz_confidence": patch.get("deadline_tz_confidence"),
                        "source": "bidnet_deadline_recovery_sidecar_staged_v1",
                        "evidence_url": meta.get("source_url") if isinstance(meta, dict) else None,
                        "title": None,
                    }
                )

    for name in (
        f"{OUT}/m3_bidnet_deadline_recovery_results_v1.json",
        f"{OUT}/m3_bidnet_list_deadline_recovery_results_v1.json",
    ):
        blob = _load(name) or {}
        for row in blob.get("rows") or []:
            if not isinstance(row, dict):
                continue
            oid = str(row.get("OPPORTUNITY_ID") or "")
            raw = row.get("DEADLINE_RAW")
            norm = row.get("DEADLINE_NORMALIZED")
            if not oid or not (raw or norm):
                continue
            if row.get("NOT_STATED"):
                continue
            nd = normalize_deadline(str(norm or raw))
            deadline = nd.get("utc_deadline") or nd.get("parsed_local") or norm
            if isinstance(deadline, datetime):
                deadline = deadline.isoformat()
            raw_out.append(
                {
                    "old_oid": oid,
                    "deadline": str(deadline) if deadline else None,
                    "deadline_raw": raw or str(norm),
                    "deadline_timezone": nd.get("timezone"),
                    "deadline_tz_confidence": nd.get("timezone_confidence"),
                    "source": Path(name).name,
                    "evidence_url": row.get("FINAL_URL") or row.get("URL") or row.get("LIST_URL"),
                    "title": row.get("TITLE"),
                }
            )

    cp = _load(f"{OUT}/m3_bidnet_deadline_recovery_checkpoint_v1.json") or {}
    for row in cp.get("results") or []:
        if not isinstance(row, dict):
            continue
        oid = str(row.get("OPPORTUNITY_ID") or "")
        raw = row.get("DEADLINE_RAW")
        norm = row.get("DEADLINE_NORMALIZED")
        if not oid or not (raw or norm):
            continue
        raw_out.append(
            {
                "old_oid": oid,
                "deadline": str(norm) if norm else None,
                "deadline_raw": raw,
                "deadline_timezone": None,
                "deadline_tz_confidence": None,
                "source": "m3_bidnet_deadline_recovery_checkpoint_v1",
                "evidence_url": row.get("FINAL_URL") or row.get("URL"),
                "title": row.get("TITLE"),
            }
        )

    index = _index_store_for_deadline_remap(store or {})
    out: dict[str, dict[str, Any]] = {}
    for item in raw_out:
        old = item["old_oid"]
        new_oid = old if store and old in store else None
        if not new_oid:
            new_oid = _remap_staged_oid(
                old,
                item,
                index=index,
                title=item.get("title"),
            )
        if not new_oid:
            continue
        # Prefer richer / first deadline; do not clobber better sidecar with thinner row
        if new_oid in out and out[new_oid].get("deadline") and not item.get("deadline_timezone"):
            continue
        out[new_oid] = {
            "deadline": item.get("deadline"),
            "deadline_raw": item.get("deadline_raw"),
            "deadline_timezone": item.get("deadline_timezone"),
            "deadline_tz_confidence": item.get("deadline_tz_confidence"),
            "source": item.get("source"),
            "evidence_url": item.get("evidence_url"),
            "remapped_from": old if old != new_oid else None,
        }
    return out


def classify_deadline_status(rec: dict[str, Any], *, now: datetime | None = None) -> str:
    """Classify closing status without inventing dates."""
    now = now or now_utc()
    funnel = str(rec.get("current_funnel_state") or "").upper()
    source_status = str(rec.get("source_status") or rec.get("live_status") or "").upper()
    title = str(rec.get("title") or "").lower()
    if funnel == "CANCELED" or "CANCEL" in source_status or "cancelled" in title[:80]:
        # Explicit cancel wins over future dates
        if "CANCEL" in source_status or funnel == "CANCELED" or re.search(r"\bcancell?ed\b", title):
            return CANCELLED

    deadline = _parse_dt(rec.get("deadline"))
    if deadline is None:
        return DEADLINE_UNKNOWN

    # Conflict: both a cancel marker and a future deadline
    if deadline >= now and (
        "CANCEL" in source_status or re.search(r"\bcancell?ed\b", str(rec.get("title") or ""), re.I)
    ):
        return DEADLINE_CONFLICT

    if deadline >= now:
        return OPEN_CONFIRMED
    return CLOSED_CONFIRMED


def snapshot_counts(store: dict[str, dict[str, Any]]) -> dict[str, Any]:
    now = now_utc()
    by_platform: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "total": 0,
            "valid": 0,
            "future": 0,
            "past": 0,
            "missing": 0,
            "invalid": 0,
            "open_confirmed": 0,
            "closed_confirmed": 0,
            "deadline_unknown": 0,
            "cancelled": 0,
            "conflict": 0,
        }
    )
    totals = Counter()
    for rec in store.values():
        if not isinstance(rec, dict):
            continue
        plat = str(rec.get("platform") or "NONE")
        by_platform[plat]["total"] += 1
        totals["total"] += 1
        status = classify_deadline_status(rec, now=now)
        status_key = {
            OPEN_CONFIRMED: "open_confirmed",
            CLOSED_CONFIRMED: "closed_confirmed",
            DEADLINE_UNKNOWN: "deadline_unknown",
            CANCELLED: "cancelled",
            DEADLINE_CONFLICT: "conflict",
        }.get(status, "deadline_unknown")
        by_platform[plat][status_key] += 1
        totals[status_key] += 1
        dl = rec.get("deadline")
        if not dl:
            by_platform[plat]["missing"] += 1
            totals["missing"] += 1
            continue
        parsed = _parse_dt(dl)
        if parsed is None:
            by_platform[plat]["invalid"] += 1
            totals["invalid"] += 1
            continue
        by_platform[plat]["valid"] += 1
        totals["valid"] += 1
        if parsed >= now:
            by_platform[plat]["future"] += 1
            totals["future"] += 1
        else:
            by_platform[plat]["past"] += 1
            totals["past"] += 1
    return {
        "at": _utc(),
        "totals": dict(totals),
        "by_platform": {k: dict(v) for k, v in sorted(by_platform.items(), key=lambda x: -x[1]["total"])},
    }


def apply_candidate(
    rec: dict[str, Any],
    *,
    deadline: str | None,
    deadline_raw: str | None,
    timezone_name: str | None = None,
    tz_confidence: str | None = None,
    source: str,
    evidence_url: str | None = None,
    evidence_path: str | None = None,
) -> dict[str, Any]:
    """Apply deadline if missing. Never overwrite verified existing deadline."""
    existing = rec.get("deadline")
    if existing:
        return {"applied": False, "reason": "preserved_existing", "existing": existing}

    if not deadline and not deadline_raw:
        return {"applied": False, "reason": "empty_candidate"}

    # Normalize if only raw provided
    if not deadline and deadline_raw:
        nd = normalize_deadline(str(deadline_raw))
        if nd.get("not_stated"):
            rec["deadline_raw"] = deadline_raw
            rec["deadline_status"] = DEADLINE_UNKNOWN
            return {"applied": False, "reason": "not_stated"}
        norm = nd.get("utc_deadline") or nd.get("parsed_local")
        if not norm:
            return {"applied": False, "reason": "unparseable"}
        deadline = norm.isoformat() if isinstance(norm, datetime) else str(norm)
        timezone_name = timezone_name or nd.get("timezone")
        tz_confidence = tz_confidence or nd.get("timezone_confidence")

    rec["deadline"] = str(deadline)
    if deadline_raw:
        rec["deadline_raw"] = str(deadline_raw)
    if timezone_name:
        rec["deadline_timezone"] = timezone_name
    if tz_confidence:
        rec["deadline_tz_confidence"] = tz_confidence

    rr = rec.get("row_ref") if isinstance(rec.get("row_ref"), dict) else {}
    rr["deadline"] = rec["deadline"]
    if deadline_raw:
        rr["deadline_raw"] = deadline_raw
    rec["row_ref"] = rr

    meta = rec.get("raw_metadata") if isinstance(rec.get("raw_metadata"), dict) else {}
    meta["deadline_extraction_repair"] = {
        "build": BUILD,
        "applied_at": _utc(),
        "source": source,
        "evidence_url": evidence_url,
        "evidence_path": evidence_path,
        "deadline": rec["deadline"],
        "deadline_raw": rec.get("deadline_raw"),
        "timezone": rec.get("deadline_timezone"),
    }
    rec["raw_metadata"] = meta
    rec["deadline_status"] = classify_deadline_status(rec)
    rec["updated_at"] = _utc()
    return {"applied": True, "reason": "recovered", "deadline": rec["deadline"], "source": source}


def run_deadline_repair(*, apply: bool = True, max_title_recoveries: int = 5000) -> dict[str, Any]:
    store = load_store()
    before = snapshot_counts(store)
    _save(BEFORE, before)

    staged = load_staged_recovery_evidence(store)
    applied = []
    skipped = Counter()
    by_source = Counter()

    # 1) Apply staged BidNet recovery evidence (remap old canonical IDs via sol# in URL)
    for oid, patch in staged.items():
        rec = store.get(oid)
        if not isinstance(rec, dict):
            skipped["oid_not_in_store"] += 1
            continue
        result = apply_candidate(
            rec,
            deadline=patch.get("deadline"),
            deadline_raw=patch.get("deadline_raw"),
            timezone_name=patch.get("deadline_timezone"),
            tz_confidence=patch.get("deadline_tz_confidence"),
            source=str(patch.get("source") or "staged"),
            evidence_url=patch.get("evidence_url"),
        )
        if result.get("applied"):
            applied.append(
                {
                    "opportunity_id": oid,
                    **result,
                    "title": (rec.get("title") or "")[:80],
                    "remapped_from": patch.get("remapped_from"),
                    "deadline_raw": patch.get("deadline_raw"),
                }
            )
            by_source[str(patch.get("source") or "staged")] += 1
            store[oid] = rec
        else:
            skipped[str(result.get("reason"))] += 1

    # 2) Structured field promotion for records still missing deadline
    for oid, rec in list(store.items()):
        if not isinstance(rec, dict) or rec.get("deadline"):
            continue
        cands = collect_structured_deadline_candidates(rec)
        # Prefer nested/raw paths; skip bare top-level deadline echo of null
        usable = [
            c
            for c in cands
            if c.get("normalized") and c.get("path") not in {"deadline", "row_ref.deadline"}
        ]
        if not usable:
            continue
        usable.sort(
            key=lambda c: (
                0 if "bidnet_recovery" in c["path"] else 1,
                0 if "deadline_raw" in c["path"] or "closing" in c["path"].lower() else 1,
                0 if c.get("kind") == "structured" else 2,
            )
        )
        best = usable[0]
        result = apply_candidate(
            rec,
            deadline=best.get("normalized"),
            deadline_raw=best.get("raw"),
            timezone_name=best.get("timezone"),
            tz_confidence=best.get("timezone_confidence"),
            source=f"structured:{best.get('path')}",
            evidence_path=best.get("path"),
        )
        if result.get("applied"):
            applied.append({"opportunity_id": oid, **result, "title": (rec.get("title") or "")[:80]})
            by_source["structured_promote"] += 1
            store[oid] = rec
        else:
            skipped[str(result.get("reason"))] += 1

    # 3) Title-embedded Closing: ... (evidence in title — common for LA RAMP / structured)
    title_n = 0
    for oid, rec in list(store.items()):
        if not isinstance(rec, dict) or rec.get("deadline"):
            continue
        if title_n >= max_title_recoveries:
            break
        hit = extract_title_closing(rec)
        if not hit:
            continue
        result = apply_candidate(
            rec,
            deadline=hit.get("normalized"),
            deadline_raw=hit.get("raw"),
            timezone_name=hit.get("timezone"),
            tz_confidence=hit.get("timezone_confidence"),
            source="title_embedded_closing",
            evidence_path=hit.get("path"),
        )
        if result.get("applied"):
            applied.append({"opportunity_id": oid, **result, "title": (rec.get("title") or "")[:100]})
            by_source["title_embedded_closing"] += 1
            store[oid] = rec
            title_n += 1
        else:
            skipped[str(result.get("reason"))] += 1

    # 4) Stamp deadline_status on all records (classification only)
    for rec in store.values():
        if isinstance(rec, dict):
            rec["deadline_status"] = classify_deadline_status(rec)

    if apply:
        save_store(store)

    after = snapshot_counts(store)
    _save(AFTER, after)

    # Manual sample: 25 newly recovered + 25 still missing / mixed platforms
    sample = []
    for row in applied[:25]:
        rec = store.get(row["opportunity_id"]) or {}
        sample.append(
            {
                "kind": "recovered",
                "opportunity_id": row["opportunity_id"],
                "title": row.get("title"),
                "deadline": row.get("deadline"),
                "source": row.get("source"),
                "deadline_status": rec.get("deadline_status"),
                "platform": rec.get("platform"),
                "evidence": (rec.get("raw_metadata") or {}).get("deadline_extraction_repair"),
            }
        )
    missing = [oid for oid, r in store.items() if isinstance(r, dict) and not r.get("deadline")]
    for oid in missing[:15]:
        rec = store[oid]
        sample.append(
            {
                "kind": "still_missing",
                "opportunity_id": oid,
                "title": (rec.get("title") or "")[:100],
                "platform": rec.get("platform"),
                "deadline_status": rec.get("deadline_status"),
            }
        )
    # Prior-had deadline sample
    had = [oid for oid, r in store.items() if isinstance(r, dict) and r.get("deadline")]
    for oid in had[:10]:
        rec = store[oid]
        sample.append(
            {
                "kind": "existing_deadline",
                "opportunity_id": oid,
                "title": (rec.get("title") or "")[:100],
                "deadline": rec.get("deadline"),
                "deadline_status": rec.get("deadline_status"),
                "platform": rec.get("platform"),
            }
        )
    _save(SAMPLES, {"at": _utc(), "count": len(sample), "samples": sample})

    newly_future = int(after["totals"].get("future") or 0) - int(before["totals"].get("future") or 0)
    report = {
        "kind": "m3_deadline_extraction_repair_v1",
        "build": BUILD,
        "built_at": _utc(),
        "applied": apply,
        "newly_recovered": len(applied),
        "newly_confirmed_future_delta": newly_future,
        "by_source": dict(by_source),
        "skipped": dict(skipped),
        "before": before["totals"],
        "after": after["totals"],
        "before_by_platform": before["by_platform"],
        "after_by_platform": after["by_platform"],
        "staged_evidence_oids": len(staged),
        "sample_recovered": applied[:20],
        "repairs": [
            "Applied staged BidNet deadline sidecars (APPLY_TO_PRODUCTION was false)",
            "Promoted structured nested deadline fields into canonical deadline",
            "Recovered title-embedded Closing: dates with provenance",
            "Classified OPEN_CONFIRMED / CLOSED_CONFIRMED / DEADLINE_UNKNOWN / CANCELLED",
        ],
    }
    _save(REPORT, report)
    _save(
        CHECKPOINT,
        {
            "at": _utc(),
            "recovered_ids": [a["opportunity_id"] for a in applied],
            "count": len(applied),
        },
    )
    return report


# --- helpers used by L23 promote / merge ---


def promote_deadline_from_record(row: dict[str, Any]) -> Any:
    """Broader promotion for to_canonical_record — never invents."""
    # Direct normalized fields first
    for key in (
        "response_deadline",
        "deadline",
        "due_date",
        "responseDeadLine",
        "responseDeadline",
        "proposalDeadline",
        "closeDate",
        "closingDate",
        "bidDueDate",
        "submissionDeadline",
        "endDate",
    ):
        val = row.get(key)
        if val not in (None, ""):
            # If already ISO-ish datetime string, keep; else normalize
            parsed = _parse_dt(val)
            if parsed:
                return parsed.isoformat()
    for key in ("deadline_raw", "closing_date_raw", "close_date_raw", "closingDate", "closeDate"):
        raw = row.get(key)
        if raw in (None, ""):
            continue
        nd = normalize_deadline(str(raw).strip())
        if nd.get("not_stated"):
            continue
        norm = nd.get("utc_deadline") or nd.get("parsed_local")
        if norm:
            return norm.isoformat() if isinstance(norm, datetime) else str(norm)
    # Nested
    for nest in ("row_ref", "bidnet_recovery", "sam_raw", "opengov_recovery"):
        bag = row.get(nest)
        if not isinstance(bag, dict):
            continue
        for key in ("deadline", "deadline_raw", "closing_date_raw", "close_date_raw", "responseDeadLine"):
            val = bag.get(key)
            if val in (None, ""):
                continue
            parsed = _parse_dt(val)
            if parsed:
                return parsed.isoformat()
            nd = normalize_deadline(str(val).strip())
            norm = nd.get("utc_deadline") or nd.get("parsed_local")
            if norm:
                return norm.isoformat() if isinstance(norm, datetime) else str(norm)
    return None
