"""BidNet Direct public acquisition: open-bids enumeration and public abstract pages.

Scope is limited to what BidNet's robots.txt permits for automated agents: the public
``/solicitations/open-bids/pageN`` listing and public abstract pages. ``/private/`` (authenticated
search, member documents) is disallowed there and is never requested. Requests identify M3
honestly, wait at least the published Crawl-delay, and stop on the first refusal (403/429),
challenge page or anomaly; there is no retry-through, identity rotation or challenge solving.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from m3.config import get_settings
from m3.research import evidence_vault
from m3.research.bidnet_parse import (
    next_page_urls_from_html,
    parse_bidnet_abstract,
    parse_search_results_html,
    reported_total_from_html,
)

BASE = "https://www.bidnetdirect.com"
OPEN_BIDS_URL = f"{BASE}/solicitations/open-bids"
USER_AGENT = "M3ProcurementResearch/1.0 (+supplier bid research; respects robots.txt)"
MIN_DELAY_S = 5.0
PAGE_SIZE = 25
MAX_PAGE_BYTES = 3_000_000


class BidNetStop(RuntimeError):
    """Acquisition must stop: refusal, challenge, robots disallow, or anomaly."""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def bidnet_public_allowed() -> dict[str, Any]:
    return {"allowed": bool(get_settings().bidnet_public_enabled),
            "checks": {"bidnet_public_enabled": bool(get_settings().bidnet_public_enabled)}}


def parse_robots(text: str, agent: str = "*") -> dict[str, Any]:
    """Rules for ``agent`` group (falls back to ``*``): disallow prefixes/patterns and crawl delay."""
    groups: dict[str, dict[str, Any]] = {}
    current: list[str] = []
    last_was_agent = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if not last_was_agent:
                current = []
            current.append(v.lower())
            groups.setdefault(v.lower(), {"disallow": [], "allow": [], "crawl_delay": None})
            last_was_agent = True
            continue
        last_was_agent = False
        for a in current:
            g = groups[a]
            if k == "disallow" and v:
                g["disallow"].append(v)
            elif k == "allow" and v:
                g["allow"].append(v)
            elif k == "crawl-delay":
                try:
                    g["crawl_delay"] = float(v)
                except ValueError:
                    pass
    return groups.get(agent.lower()) or groups.get("*") or {"disallow": [], "allow": [], "crawl_delay": None}


def _rule_matches(rule: str, path: str) -> bool:
    pat = "^" + re.escape(rule).replace(r"\*", ".*")
    if pat.endswith(r"\$"):
        pat = pat[:-2] + "$"
    return re.search(pat, path) is not None


def robots_allows(rules: dict[str, Any], url: str) -> bool:
    u = urlparse(url)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    best_allow = max((len(r) for r in rules.get("allow", []) if _rule_matches(r, path)), default=-1)
    best_dis = max((len(r) for r in rules.get("disallow", []) if _rule_matches(r, path)), default=-1)
    return best_allow >= best_dis


_CHALLENGE_PAGE = re.compile(
    r"(<title>\s*(?:403|429|access denied|just a moment|attention required)|awswaf\.com/[^\"']+/captcha"
    r"|class=\"[^\"]*captcha|verify you are human|unusual traffic)", re.I)


class PoliteClient:
    """Single-connection client: robots-checked, Crawl-delay paced, fail-fast."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
                 delay_s: float | None = None):
        self._http = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True,
                                  headers={"User-Agent": USER_AGENT}, transport=transport)
        self._sleep = sleep
        self._last = 0.0
        self.requests = 0
        self.rules = self._load_robots()
        self.delay_s = max(MIN_DELAY_S, float(self.rules.get("crawl_delay") or 0), float(delay_s or 0))

    def _load_robots(self) -> dict[str, Any]:
        r = self._http.get(f"{BASE}/robots.txt")
        self.requests += 1
        if r.status_code != 200:
            raise BidNetStop(f"robots_unavailable:{r.status_code}")
        return parse_robots(r.text, "M3ProcurementResearch")

    def get(self, url: str) -> str:
        if not url.startswith(BASE) or "/private/" in url:
            raise BidNetStop(f"out_of_scope:{url[:80]}")
        if not robots_allows(self.rules, url):
            raise BidNetStop(f"robots_disallow:{urlparse(url).path[:80]}")
        wait = self.delay_s - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        attempt = 0
        while True:
            self._last = time.monotonic()
            self.requests += 1
            try:
                r = self._http.get(url)
                break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                attempt += 1
                if attempt > 1:
                    raise BidNetStop(f"network:{type(exc).__name__}") from exc
                self._sleep(self.delay_s * 2)
        if r.status_code in (403, 429) or r.status_code >= 500:
            raise BidNetStop(f"http_{r.status_code}")
        if r.status_code != 200:
            raise BidNetStop(f"http_{r.status_code}")
        if len(r.content) > MAX_PAGE_BYTES:
            raise BidNetStop("page_too_large")
        text = r.text
        if _CHALLENGE_PAGE.search(text[:20000]):
            raise BidNetStop("challenge_page")
        if "/public/authentication" in str(r.url) or "/login" in urlparse(str(r.url)).path:
            raise BidNetStop("redirected_to_login")
        return text

    def close(self) -> None:
        self._http.close()


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def run_bidnet_public_enumeration(
    *,
    out_root: Path | None = None,
    max_pages: int | None = None,
    resume: bool = True,
    client: PoliteClient | None = None,
    incremental: bool = False,
) -> dict[str, Any]:
    """Enumerate the national open-bids listing into a checkpointed page store.

    ``incremental`` walks newest-first only until pages contain nothing new versus ``snapshot.jsonl``,
    writing to ``incremental_pages/<run>/`` (merged by ``finalize_bidnet_snapshot``).

    Each page's parsed rows land in ``pages/page_NNNNN.jsonl``; ``checkpoint.json`` records
    completed pages, the provider-reported total and the stop reason so an interrupted run resumes.
    """
    gate = bidnet_public_allowed()
    if not gate["allowed"]:
        return {"ok": False, "error": "bidnet_public_disabled", "gate": gate}
    root = (out_root or get_settings().bidnet_snapshot_root()) / "enumeration"
    pages_dir = root / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    ck_path = root / "checkpoint.json"
    ck: dict[str, Any] = json.loads(ck_path.read_text(encoding="utf-8")) if resume and ck_path.exists() else {}
    if not ck or ck.get("complete"):
        ck = {"started_at": _utc(), "completed_pages": [], "reported_total": None, "page_hashes": {},
              "complete": False, "stop_reason": None}
    known: set[str] | None = None
    if incremental:
        snap = (out_root or get_settings().bidnet_snapshot_root()) / "snapshot.jsonl"
        known = {str(json.loads(line).get("provider_record_id")) for line in snap.read_text(encoding="utf-8").splitlines()} if snap.exists() else set()
        ck = {"started_at": _utc(), "completed_pages": [], "reported_total": None, "page_hashes": {}, "complete": False,
              "stop_reason": None, "mode": "incremental"}
        pages_dir = root / "incremental_pages" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ck_path = root / "incremental_checkpoint.json"
    own = client is None
    client = client or PoliteClient()
    try:
        stop_reason = _walk_listing(client, OPEN_BIDS_URL, pages_dir, ck, lambda: _write_json(ck_path, ck), max_pages=max_pages,
                                    known_ids=known, vault=_vault(out_root))
    except BidNetStop as exc:
        stop_reason = str(exc)
    finally:
        if own:
            client.close()
    ck["stop_reason"] = stop_reason
    ck["updated_at"] = _utc()
    _write_json(ck_path, ck)
    return {"ok": True, "pages": len(ck["completed_pages"]), "reported_total": ck.get("reported_total"), "complete": ck.get("complete"),
            "stop_reason": stop_reason, "requests_this_run": client.requests, "checkpoint": str(ck_path)}


def _vault(out_root: Path | None) -> Path:
    """Shared evidence vault (``evidence_vault_root``); an explicit ``out_root`` (tests, isolated runs) keeps its own."""
    if out_root is not None:
        return out_root / "evidence_vault"
    return get_settings().evidence_vault_root()


def _walk_listing(client: PoliteClient, list_base: str, pages_dir: Path, ck: dict[str, Any], save: Callable[[], None],
                  *, max_pages: int | None = None, known_ids: set[str] | None = None, stop_after_known_pages: int = 3,
                  vault: Path | None = None) -> str:
    """Walk ``list_base`` (+ ``/pageN``) until the listing ends; ``ck`` holds this listing's progress.

    With ``known_ids`` (incremental mode) the walk stops after ``stop_after_known_pages`` consecutive
    pages containing no new id: the national listing is ordered newest publication first.
    Returns the stop reason. A BidNetStop raised by the client propagates so callers decide whether
    the refusal ends the whole run.
    """
    pages_dir.mkdir(parents=True, exist_ok=True)
    done = set(ck.setdefault("completed_pages", []))
    hashes = ck.setdefault("page_hashes", {})
    seen_fp = set(hashes.values())
    page = max(done) + 1 if done else 1
    known_streak = 0
    while True:
        if max_pages is not None and len(done) >= max_pages:
            return "max_pages"
        reported = ck.get("reported_total")
        if reported is not None and (page - 1) * PAGE_SIZE >= reported:
            ck["complete"] = True
            return "reported_total_reached"
        url = list_base if page == 1 else f"{list_base}/page{page}"
        try:
            html = client.get(url)
        except BidNetStop as exc:
            if str(exc) == "http_404" and page > 1:
                ck["complete"] = True
                return "end_of_listing_404"
            raise
        evidence_vault.put("bidnet", f"listing:{url}", "listing_page", html, url=url, status=200, content_type="text/html",
                           root=vault)
        rows = parse_search_results_html(html, list_url=url, page_number=page)
        if page == 1 or ck.get("reported_total") is None:
            ck["reported_total"] = reported_total_from_html(html)
        fp = hashlib.sha256("|".join(str(r.get("provider_record_id")) for r in rows).encode()).hexdigest()[:16]
        if not rows:
            ck["complete"] = page == 1 or bool(ck.get("reported_total") and (page - 1) * PAGE_SIZE >= ck["reported_total"] - PAGE_SIZE)
            return "empty_page"
        if fp in seen_fp:
            # The provider stops serving new rows past its deep-paging limit (~10,000 results)
            return "pagination_repeat"
        seen_fp.add(fp)
        now = _utc()
        with open(pages_dir / f"page_{page:05d}.jsonl", "w", encoding="utf-8") as fh:
            for r in rows:
                r["retrieved_at"] = now
                fh.write(json.dumps(r, default=str) + "\n")
        done.add(page)
        ck["completed_pages"] = sorted(done)
        hashes[str(page)] = fp
        ck["updated_at"] = now
        ck["requests"] = int(ck.get("requests") or 0) + 1
        save()
        if known_ids is not None:
            new = [r for r in rows if str(r.get("provider_record_id")) not in known_ids]
            known_streak = 0 if new else known_streak + 1
            if known_streak >= stop_after_known_pages:
                return "caught_up_with_known_records"
        if not next_page_urls_from_html(html, list_url=url, current_page=page):
            ck["complete"] = True
            return "no_next_page"
        page += 1


COMPLETE_COVERAGE_RATIO = 0.99

US_STATE_SLUGS = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "district-of-columbia",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new-hampshire", "new-jersey", "new-mexico", "new-york", "north-carolina", "north-dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode-island", "south-carolina", "south-dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west-virginia", "wisconsin", "wyoming",
)


def run_bidnet_state_enumeration(
    *,
    out_root: Path | None = None,
    states: tuple[str, ...] = US_STATE_SLUGS,
    max_pages_per_state: int | None = None,
    resume: bool = True,
    client: PoliteClient | None = None,
) -> dict[str, Any]:
    """Enumerate the public per-state open-bids listings (``/{state}/solicitations/open-bids``).

    The national listing stops serving rows past ~10,000 results; each state's listing is far smaller,
    so walking states covers the full open universe. A missing state listing (404) is recorded and
    skipped; any refusal or challenge (403/429/5xx/challenge/login) stops the whole run.
    """
    gate = bidnet_public_allowed()
    if not gate["allowed"]:
        return {"ok": False, "error": "bidnet_public_disabled", "gate": gate}
    root = (out_root or get_settings().bidnet_snapshot_root()) / "enumeration"
    ck_path = root / "state_checkpoint.json"
    ck: dict[str, Any] = json.loads(ck_path.read_text(encoding="utf-8")) if resume and ck_path.exists() else {}
    ck.setdefault("started_at", _utc())
    parts: dict[str, dict[str, Any]] = ck.setdefault("partitions", {})
    own = client is None
    client = client or PoliteClient()
    run_stop = None
    try:
        for slug in states:
            part = parts.setdefault(slug, {})
            if part.get("complete") or part.get("stop_reason") == "http_404":
                continue
            try:
                part["stop_reason"] = _walk_listing(client, f"{BASE}/{slug}/solicitations/open-bids", root / "state_pages" / slug,
                                                    part, lambda: _write_json(ck_path, ck), max_pages=max_pages_per_state,
                                                    vault=_vault(out_root))
            except BidNetStop as exc:
                part["stop_reason"] = str(exc)
                if str(exc) != "http_404":
                    run_stop = str(exc)
                    break
            finally:
                _write_json(ck_path, ck)
    finally:
        if own:
            client.close()
    ck["run_stop_reason"] = run_stop
    ck["updated_at"] = _utc()
    _write_json(ck_path, ck)
    incomplete = [s for s in states if not (parts.get(s) or {}).get("complete") and (parts.get(s) or {}).get("stop_reason") != "http_404"]
    return {"ok": run_stop is None, "states_complete": sum(1 for s in states if (parts.get(s) or {}).get("complete")),
            "states_missing": [s for s in states if (parts.get(s) or {}).get("stop_reason") == "http_404"],
            "states_incomplete": incomplete, "pages": sum(len((parts.get(s) or {}).get("completed_pages") or []) for s in states),
            "run_stop_reason": run_stop, "requests_this_run": client.requests, "checkpoint": str(ck_path)}


def finalize_bidnet_snapshot(*, out_root: Path | None = None) -> dict[str, Any]:
    """Dedupe enumerated pages into ``snapshot.jsonl`` plus a coverage report against the provider total."""
    root = out_root or get_settings().bidnet_snapshot_root()
    enum_root = root / "enumeration"
    ck_p, sck_p = enum_root / "checkpoint.json", enum_root / "state_checkpoint.json"
    ck = json.loads(ck_p.read_text(encoding="utf-8")) if ck_p.exists() else {}
    sck = json.loads(sck_p.read_text(encoding="utf-8")) if sck_p.exists() else {}
    by_id: dict[str, dict[str, Any]] = {}
    rows_total = no_id = 0
    # Newest incremental pages first so a re-listed record keeps its latest retrieval
    page_files = (sorted((enum_root / "incremental_pages").glob("*/page_*.jsonl"), reverse=True)
                  + sorted((enum_root / "pages").glob("page_*.jsonl"))
                  + sorted((enum_root / "state_pages").glob("*/page_*.jsonl")))
    for p in page_files:
        for line in p.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            rows_total += 1
            key = r.get("provider_record_id") or r.get("official_url")
            if not r.get("provider_record_id"):
                no_id += 1
            if key and key not in by_id:
                by_id[key] = r
    snap = root / "snapshot.jsonl"
    tmp = snap.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in by_id.values():
            fh.write(json.dumps(r, default=str) + "\n")
    tmp.replace(snap)
    reported = ck.get("reported_total")
    parts = sck.get("partitions") or {}
    states_done = [s for s, p in parts.items() if p.get("complete") or p.get("stop_reason") == "http_404"]
    states_complete = bool(parts) and len(states_done) == len(US_STATE_SLUGS) and not sck.get("run_stop_reason")
    ratio = round(len(by_id) / reported, 4) if reported else None
    walked = bool(ck.get("complete")) or states_complete
    coverage = {
        "built_at": _utc(), "reported_total": reported, "rows_retrieved": rows_total, "unique_records": len(by_id),
        "duplicate_rows": rows_total - len(by_id), "rows_without_provider_id": no_id,
        "pages": len(ck.get("completed_pages") or []) + sum(len(p.get("completed_pages") or []) for p in parts.values()),
        "national_listing_complete": bool(ck.get("complete")), "national_stop_reason": ck.get("stop_reason"),
        "state_listings_complete": states_complete, "states_walked": len(states_done),
        "states_missing": sorted(s for s, p in parts.items() if p.get("stop_reason") == "http_404"),
        "state_run_stop_reason": sck.get("run_stop_reason"),
        "listings_walked": walked,
        # Complete only when the snapshot holds the provider's reported open total; a partial snapshot
        # must never be treated as the whole open universe (it would retire active legacy records)
        "enumeration_complete": walked and ratio is not None and ratio >= COMPLETE_COVERAGE_RATIO,
        "complete_coverage_threshold": COMPLETE_COVERAGE_RATIO,
        "stop_reason": sck.get("run_stop_reason") if parts else ck.get("stop_reason"),
        "coverage_ratio": ratio,
        "with_deadline": sum(1 for r in by_id.values() if r.get("deadline_at")),
        "snapshot": str(snap),
    }
    _write_json(root / "coverage.json", coverage)
    return coverage


def fetch_public_abstracts(
    records: list[dict[str, Any]], *, out_root: Path | None = None, max_requests: int = 200,
    client: PoliteClient | None = None, vault: Path | None = None,
) -> dict[str, Any]:
    """Public abstract pages for selected records (product candidates only). Resumable; never touches /private/."""
    gate = bidnet_public_allowed()
    if not gate["allowed"]:
        return {"ok": False, "error": "bidnet_public_disabled", "gate": gate}
    root = (out_root or get_settings().bidnet_snapshot_root()) / "abstracts"
    root.mkdir(parents=True, exist_ok=True)
    store = root / "abstracts.jsonl"
    have = set()
    if store.exists():
        have = {json.loads(l)["provider_record_id"] for l in store.read_text(encoding="utf-8").splitlines() if l.strip()}
    vault = vault or _vault(out_root)
    own = client is None
    client = client or PoliteClient()
    fetched = 0
    stop_reason = None
    try:
        with open(store, "a", encoding="utf-8") as fh:
            for rec in records:
                pid, url = rec.get("provider_record_id"), rec.get("official_url")
                if not pid or not url or pid in have:
                    continue
                if fetched >= max_requests:
                    stop_reason = "max_requests"
                    break
                try:
                    html = client.get(url)
                except BidNetStop as exc:
                    stop_reason = str(exc)
                    if not stop_reason.startswith(("robots_disallow", "out_of_scope")):
                        break
                    continue
                fetched += 1
                ab = parse_bidnet_abstract(html, detail_url=url)
                stored = evidence_vault.put("bidnet", f"bidnet:{pid}", "solicitation_page", html, url=url, status=200,
                                            content_type="text/html", root=vault,
                                            meta={"deadline_at": ab.get("deadline_at") or rec.get("deadline_at"),
                                                  "listing_fingerprint": rec.get("listing_fingerprint")})
                fh.write(json.dumps({"provider_record_id": pid, "official_url": url, "retrieved_at": _utc(),
                                     "evidence_sha256": stored["sha256"], "evidence_version": stored["version"], **ab},
                                    default=str) + "\n")
                fh.flush()
                have.add(pid)
    finally:
        if own:
            client.close()
    return {"ok": True, "fetched": fetched, "stop_reason": stop_reason, "stored": len(have), "path": str(store)}
