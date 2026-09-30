"""M3 Source Coverage Expansion Sweep #2.

Attacks Sweep #1 blind spots (K-12, special districts, counties, cities, HE,
airports, transit, utilities, housing, libraries) without rebuilding core engines.

Loads Sweep #1 inventories as baseline, visits NEW expansion seeds + UNKNOWN /
DEGRADED revisits, merges with dedupe, and writes extended artifacts.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from application_clock import now_utc
from procurement_source_coverage_discovery import (
    ACCESS_FREE_REG,
    ACCESS_LIMITED_FREE,
    ACCESS_PAID,
    ACCESS_PUBLIC,
    ACCESS_PUBLIC_PLUS_FREE_BID,
    ACCESS_UNKNOWN,
    COVERAGE_EXPANSION_ENTITIES,
    classify_access_level,
    is_paid_aggregator_url,
)
from procurement_source_registry import ProcurementSourceRegistry, bootstrap_registry
from procurement_source_seed_sweep import (
    ARTIFACTS,
    MAX_OPPORTUNITIES_STORE,
    ProcurementSourceSeedSweep,
    _guess_entity_type,
    _norm_url,
    _seed_id,
)

BUILD_TAG = "20260921-m3-source-coverage-expansion-2"
SWEEP1_BASELINE = {
    "seeds_loaded": 172,
    "visits": 252,
    "healthy": 105,
    "degraded": 94,
    "auth_required": 8,
    "blocked_or_unavailable": 37,
    "unique_portals": 249,
    "unique_entities": 320,
    "unique_opportunities": 1862,
    "k12": 10,
    "higher_education": 11,
    "city": 8,
    "county": 11,
    "special_district": 0,
    "airport": 2,
    "transit": 1,
    "utility": 1,
    "housing": 0,
    "library_or_other": 158,
    "cooperative": 13,
    "federal": 55,
    "state": 50,
    "public_access": 195,
    "free_registration": 8,
    "limited_free": 2,
    "unknown_access": 44,
}

MAX_EXPANSION_SEED_ATTEMPTS = 220
MAX_REVISIT_ATTEMPTS = 60
MAX_EXPANSION_SECONDARY = 100


def _utc() -> str:
    return now_utc().isoformat()


def guess_entity_type_expanded(name: str | None) -> str:
    """Richer typing for special districts / education / local entities."""
    low = (name or "").lower()
    if not low:
        return "OTHER_PUBLIC"
    # Special-district / water / fire patterns before generic "metro" → transit
    if re.search(
        r"\b(water|sewer|sanitation|irrigation|drainage|fire|hospital|park|"
        r"conservation|mosquito|abatement|flood|soil|wastewater|"
        r"municipal utility|public utility district|pud|mwd|redevelopment|"
        r"cemetery)\b.*\b(district|authority)\b|"
        r"\b(district|authority)\b.*\b(water|sewer|fire|hospital|park|utility)\b|"
        r"\b(fire protection district|fire authority|hospital district|park district|"
        r"library district|community services district|csd|flood control|"
        r"water district|water authority|utility district)\b",
        low,
    ):
        return "SPECIAL_DISTRICT"
    if "boces" in low or "education service" in low or "regional education" in low:
        return "K12_SCHOOL_DISTRICT"
    if "housing authority" in low or "housing agency" in low:
        return "HOUSING_AUTHORITY"
    if "library" in low:
        return "LIBRARY"
    if "airport" in low:
        return "AIRPORT"
    if "port authority" in low or "port of" in low:
        return "PORT_AUTHORITY"
    if "transit" in low or "metropolitan transportation" in low or "rapid transit" in low:
        return "TRANSIT"
    base = _guess_entity_type(name)
    if base == "UTILITY" and "district" in low:
        return "SPECIAL_DISTRICT"
    if base == "TRANSIT" and ("water" in low or "utility" in low or "district" in low):
        return "SPECIAL_DISTRICT"
    return base


# Imported seed tables live in companion module to keep this file readable.
from procurement_source_expansion_seeds import (  # noqa: E402
    build_expansion_seed_catalog as _build_seed_tables,
)


def build_expansion_seed_catalog() -> list[dict[str, Any]]:
    return _build_seed_tables(
        coverage_expansion_entities=COVERAGE_EXPANSION_ENTITIES,
        guess_entity_type=guess_entity_type_expanded,
        row_to_seed=_row_to_seed,
        seed_id_fn=_seed_id,
        norm_url=_norm_url,
    )


def _row_to_seed(
    name: str,
    state: str | None,
    url: str,
    *,
    entity_type: str,
    platform: str | None = None,
    government_level: str = "LOCAL",
) -> dict[str, Any]:
    return {
        "seed_name": name,
        "url": url,
        "kind": "LOCAL",
        "government_level": government_level,
        "state": state,
        "platform_family": platform,
        "role": "PROCUREMENT_ENTRY",
        "entity_map_only": False,
        "entity_type_hint": entity_type,
        "notes": None,
        "provenance": "expansion_sweep_2",
        "discovery_access": None,
        "document_access": None,
        "bid_submission_access": None,
        "one_account_many_entities": platform
        in {"Bonfire", "OpenGov", "PlanetBids", "PublicPurchase", "IonWave", "BidNet"},
    }


def classify_access_layers(
    health: str, notes: str | None = None, access_level: str | None = None
) -> dict[str, str]:
    """Separate discovery / document / bid-submission access."""
    notes_l = (notes or "").lower()
    al = access_level or ACCESS_UNKNOWN
    if health in {"BLOCKED", "UNAVAILABLE"}:
        return {
            "discovery_access": "UNAVAILABLE" if health == "UNAVAILABLE" else "BLOCKED",
            "document_access": "UNAVAILABLE",
            "bid_submission_access": "UNAVAILABLE",
        }
    discovery = "UNKNOWN"
    documents = "UNKNOWN"
    bid_sub = "UNKNOWN"
    if health == "AUTH_REQUIRED" or al == ACCESS_FREE_REG:
        discovery = documents = bid_sub = "FREE_REGISTRATION"
    elif al == ACCESS_PUBLIC_PLUS_FREE_BID or "register to bid" in notes_l or "register to respond" in notes_l:
        discovery = "PUBLIC"
        documents = "PUBLIC" if "view without login" in notes_l else "PUBLIC_OR_FREE_REG"
        bid_sub = "FREE_REGISTRATION"
    elif al == ACCESS_PUBLIC or health in {"HEALTHY", "DEGRADED"}:
        discovery = "PUBLIC"
        documents = "PUBLIC" if health == "HEALTHY" else "PARTIAL"
        bid_sub = "FREE_REGISTRATION" if "register" in notes_l else "UNKNOWN"
    elif al == ACCESS_LIMITED_FREE:
        discovery = documents = bid_sub = "LIMITED_FREE"
    elif al == ACCESS_PAID:
        discovery = documents = bid_sub = "PAID"
    return {
        "discovery_access": discovery,
        "document_access": documents,
        "bid_submission_access": bid_sub,
    }


def reclassify_unknown_portal(portal: dict[str, Any]) -> dict[str, Any]:
    """Clearer access label for Sweep #1 UNKNOWN rows using health + notes."""
    row = dict(portal)
    health = str(row.get("source_health") or row.get("health_state") or "").upper()
    notes = str(row.get("notes") or "")
    url = row.get("url") or row.get("portal_url") or row.get("discovery_url")
    row["discovery_url"] = url
    row["portal_url"] = url
    row["health_state"] = health
    if row.get("publicly_searchable") is None:
        if health in {"HEALTHY", "DEGRADED"}:
            row["publicly_searchable"] = True
        elif health in {"AUTH_REQUIRED", "BLOCKED", "UNAVAILABLE"}:
            row["publicly_searchable"] = False
    access = classify_access_level(row)
    if access == ACCESS_UNKNOWN:
        if health == "HEALTHY":
            access = ACCESS_PUBLIC
        elif health == "DEGRADED":
            access = (
                ACCESS_PUBLIC_PLUS_FREE_BID
                if "register to bid" in notes.lower() or "register to respond" in notes.lower()
                else ACCESS_PUBLIC
            )
        elif health == "AUTH_REQUIRED":
            access = ACCESS_FREE_REG
        elif is_paid_aggregator_url(url):
            access = ACCESS_PAID
    row["access_level"] = access
    row.update(classify_access_layers(health, notes, access))
    return row


def opportunity_identity_key(opp: dict[str, Any]) -> str:
    sid = (opp.get("solicitation_number") or "").strip().lower()
    title = (opp.get("title") or "").strip().lower()[:80]
    url = _norm_url(opp.get("detail_url") or opp.get("discovered_from_url") or "").lower()
    agency = (opp.get("agency") or "").strip().lower()[:60]
    return hashlib.sha1(f"{sid}|{title}|{url}|{agency}".encode()).hexdigest()[:16]


def load_sweep1_baseline(artifacts_dir: Path) -> dict[str, Any]:
    """Load Sweep #1 inventories if present; fall back to hardcoded baseline counts."""
    out: dict[str, Any] = {
        "baseline_counts": dict(SWEEP1_BASELINE),
        "portals": {},
        "entities": {},
        "relationships": [],
        "opportunities": [],
        "unknown_portals": [],
        "degraded_portals": [],
        "opportunity_count_baseline": SWEEP1_BASELINE["unique_opportunities"],
    }
    cov_path = artifacts_dir / "source_coverage_report.json"
    inv_path = artifacts_dir / "procurement_source_inventory.json"
    ent_path = artifacts_dir / "procurement_entity_inventory.json"
    rel_path = artifacts_dir / "source_entity_relationships.json"
    run_path = artifacts_dir / "source_discovery_run.json"

    if cov_path.exists():
        cov = json.loads(cov_path.read_text(encoding="utf-8"))
        et = cov.get("entities_by_type") or {}
        ab = cov.get("access_breakdown") or {}
        out["baseline_counts"] = {
            "seeds_loaded": cov.get("seeds_loaded", SWEEP1_BASELINE["seeds_loaded"]),
            "visits": cov.get("visits", SWEEP1_BASELINE["visits"]),
            "healthy": cov.get("successful_healthy", SWEEP1_BASELINE["healthy"]),
            "degraded": cov.get("degraded", SWEEP1_BASELINE["degraded"]),
            "auth_required": cov.get("auth_required", SWEEP1_BASELINE["auth_required"]),
            "blocked_or_unavailable": cov.get(
                "blocked_or_unavailable", SWEEP1_BASELINE["blocked_or_unavailable"]
            ),
            "unique_portals": cov.get("unique_procurement_portals", SWEEP1_BASELINE["unique_portals"]),
            "unique_entities": cov.get(
                "unique_government_entities", SWEEP1_BASELINE["unique_entities"]
            ),
            "unique_opportunities": cov.get(
                "unique_opportunities_discovered", SWEEP1_BASELINE["unique_opportunities"]
            ),
            "k12": et.get("k12", SWEEP1_BASELINE["k12"]),
            "higher_education": et.get("higher_education", SWEEP1_BASELINE["higher_education"]),
            "city": et.get("city", SWEEP1_BASELINE["city"]),
            "county": et.get("county", SWEEP1_BASELINE["county"]),
            "special_district": et.get("special_district", SWEEP1_BASELINE["special_district"]),
            "airport": et.get("airport", SWEEP1_BASELINE["airport"]),
            "transit": et.get("transit", SWEEP1_BASELINE["transit"]),
            "utility": et.get("utility", SWEEP1_BASELINE["utility"]),
            "housing": et.get("housing", SWEEP1_BASELINE["housing"]),
            "library_or_other": et.get("library_or_other", SWEEP1_BASELINE["library_or_other"]),
            "cooperative": et.get("cooperative", SWEEP1_BASELINE["cooperative"]),
            "federal": et.get("federal", SWEEP1_BASELINE["federal"]),
            "state": et.get("state", SWEEP1_BASELINE["state"]),
            "public_access": ab.get("PUBLIC", 195),
            "free_registration": ab.get("FREE_REGISTRATION", 8),
            "limited_free": ab.get("LIMITED_FREE", 2),
            "unknown_access": ab.get("UNKNOWN", 44),
        }
        out["opportunity_count_baseline"] = out["baseline_counts"]["unique_opportunities"]

    if inv_path.exists():
        inv = json.loads(inv_path.read_text(encoding="utf-8"))
        for p in inv.get("portals") or []:
            url = _norm_url(p.get("url") or p.get("portal_url"))
            if not url:
                continue
            key = url.lower()
            out["portals"][key] = p
            if (p.get("access_level") or "") == ACCESS_UNKNOWN:
                out["unknown_portals"].append(p)
            if (p.get("source_health") or "") == "DEGRADED":
                out["degraded_portals"].append(p)

    if ent_path.exists():
        ent = json.loads(ent_path.read_text(encoding="utf-8"))
        for e in ent.get("entities") or []:
            eid = e.get("entity_id")
            if eid:
                out["entities"][eid] = e

    if rel_path.exists():
        rel = json.loads(rel_path.read_text(encoding="utf-8"))
        out["relationships"] = list(rel.get("relationships") or [])

    if run_path.exists():
        run = json.loads(run_path.read_text(encoding="utf-8"))
        seen_opp: set[str] = set()
        for o in run.get("opportunities_sample") or []:
            k = opportunity_identity_key(o)
            if k not in seen_opp:
                seen_opp.add(k)
                out["opportunities"].append(o)

    return out


class CoverageExpansionSweep(ProcurementSourceSeedSweep):
    """Sweep #2 — expand local/K-12/special coverage; revisit UNKNOWN/DEGRADED."""

    def __init__(
        self,
        *,
        registry: ProcurementSourceRegistry | None = None,
        artifacts_dir: Path | None = None,
        max_seed_attempts: int = MAX_EXPANSION_SEED_ATTEMPTS,
        max_secondary_visits: int = MAX_EXPANSION_SECONDARY,
        max_revisit_attempts: int = MAX_REVISIT_ATTEMPTS,
    ) -> None:
        super().__init__(
            registry=registry or bootstrap_registry(),
            artifacts_dir=artifacts_dir or ARTIFACTS,
            max_seed_attempts=max_seed_attempts,
            max_secondary_visits=max_secondary_visits,
        )
        self.max_revisit_attempts = max_revisit_attempts
        self.baseline: dict[str, Any] = {}
        self.sweep2_new_portals: set[str] = set()
        self.sweep2_new_entities: set[str] = set()
        self.sweep2_new_opportunities: list[dict[str, Any]] = []
        self.revisit_results: list[dict[str, Any]] = []
        self._opp_keys: set[str] = set()

    def _hydrate_from_baseline(self) -> None:
        self.baseline = load_sweep1_baseline(self.artifacts_dir)
        for key, portal in self.baseline["portals"].items():
            if portal.get("access_level") == ACCESS_UNKNOWN:
                portal = reclassify_unknown_portal(portal)
            self.portals[key] = portal
        for eid, ent in self.baseline["entities"].items():
            if ent.get("entity_type") in (None, "", "OTHER_PUBLIC"):
                ent = dict(ent)
                ent["entity_type"] = guess_entity_type_expanded(ent.get("name"))
            self.entities[eid] = ent
        self.relationships = list(self.baseline.get("relationships") or [])
        for o in self.baseline.get("opportunities") or []:
            k = opportunity_identity_key(o)
            self._opp_keys.add(k)
            self.opportunities.append(o)
        for key in self.portals:
            self._visited.add(key)

    def _register_entity(self, ent: dict[str, Any]) -> str:
        if ent.get("entity_type") in (None, "", "OTHER_PUBLIC"):
            ent = dict(ent)
            ent["entity_type"] = guess_entity_type_expanded(ent.get("name"))
        return super()._register_entity(ent)

    def visit(self, seed: dict[str, Any], *, is_secondary: bool = False) -> dict[str, Any]:
        before_portals = set(self.portals.keys())
        before_entities = set(self.entities.keys())

        result = super().visit(seed, is_secondary=is_secondary)

        hint = seed.get("entity_type_hint")
        if hint and seed.get("seed_name"):
            for ent in self.entities.values():
                if ent.get("name") == seed.get("seed_name"):
                    ent["entity_type"] = hint
                    ent["provenance"] = ent.get("provenance") or "expansion_sweep_2"

        url = _norm_url(seed.get("url")).lower()
        if url in self.portals:
            p = self.portals[url]
            p.update(
                classify_access_layers(
                    str(p.get("source_health") or ""),
                    p.get("notes"),
                    p.get("access_level"),
                )
            )
            if seed.get("one_account_many_entities") is not None:
                p["one_account_many_entities"] = seed.get("one_account_many_entities")

        for k in self.portals.keys() - before_portals:
            self.sweep2_new_portals.add(k)
        for k in self.entities.keys() - before_entities:
            self.sweep2_new_entities.add(k)

        uniq: dict[str, dict[str, Any]] = {}
        for o in self.opportunities:
            key = opportunity_identity_key(o)
            if key not in self._opp_keys:
                self.sweep2_new_opportunities.append(o)
                self._opp_keys.add(key)
            uniq[key] = o
        self.opportunities = list(uniq.values())[:MAX_OPPORTUNITIES_STORE]
        return result

    def revisit_unknown_and_degraded(self) -> None:
        candidates: list[dict[str, Any]] = []
        for p in self.baseline.get("unknown_portals") or []:
            candidates.append({**p, "_revisit_reason": "UNKNOWN_ACCESS"})
        scored = []
        for p in self.baseline.get("degraded_portals") or []:
            url = (p.get("url") or "").lower()
            name = (p.get("source_name") or p.get("seed_name") or "").lower()
            score = 0
            if any(x in url for x in (".gov", "sciquest", "bonfire", "opengov", "planetbids", "bidnet")):
                score += 2
            if any(x in name or x in url for x in ("bid", "rfp", "procurement", "purchasing", "solicitation")):
                score += 2
            if p.get("government_level") in {"STATE", "LOCAL", "FEDERAL"}:
                score += 1
            if p.get("role") in {"PLATFORM_MARKETING"}:
                score -= 3
            scored.append((score, p))
        scored.sort(key=lambda x: -x[0])
        for score, p in scored:
            if score >= 1:
                candidates.append({**p, "_revisit_reason": "DEGRADED_PROMISING"})

        seen: set[str] = set()
        attempted = 0
        for p in candidates:
            if attempted >= self.max_revisit_attempts:
                break
            url = _norm_url(p.get("url") or p.get("portal_url"))
            if not url or url.lower() in seen:
                continue
            seen.add(url.lower())
            self._visited.discard(url.lower())
            seed = {
                "seed_id": p.get("source_id") or _seed_id("revisit", url, p.get("source_name") or ""),
                "seed_name": p.get("source_name") or p.get("seed_name") or url,
                "url": url,
                "kind": p.get("kind") or "REVISIT",
                "government_level": p.get("government_level") or "LOCAL",
                "state": p.get("state"),
                "platform_family": p.get("platform_family"),
                "role": "REVISIT",
                "notes": p.get("notes"),
                "provenance": f"sweep2_revisit:{p.get('_revisit_reason')}",
                "entity_map_only": bool(p.get("entity_map_only")),
            }
            visit = self.visit(seed, is_secondary=False)
            self.revisit_results.append(
                {
                    "url": url,
                    "reason": p.get("_revisit_reason"),
                    "prior_health": p.get("source_health"),
                    "prior_access": p.get("access_level"),
                    "new_health": visit.get("health"),
                    "new_access": visit.get("access_level"),
                    "opportunities_parsed": visit.get("opportunities_parsed"),
                    "at": _utc(),
                }
            )
            attempted += 1

    def run(self) -> dict[str, Any]:
        self._hydrate_from_baseline()
        expansion_seeds = build_expansion_seed_catalog()
        fresh: list[dict[str, Any]] = []
        for s in expansion_seeds:
            key = _norm_url(s["url"]).lower()
            if key in self.portals and key in self._visited:
                hint = s.get("entity_type_hint")
                if hint and s.get("seed_name"):
                    eid = self._register_entity(
                        {
                            "name": s["seed_name"],
                            "entity_type": hint,
                            "state": s.get("state"),
                            "government_level": s.get("government_level") or "LOCAL",
                            "provenance": "expansion_sweep_2_seed_catalog",
                            "procurement_portals": [s["url"]],
                        }
                    )
                    if eid not in self.baseline["entities"]:
                        self.sweep2_new_entities.add(eid)
                    pid = self.portals[key].get("source_id") or key
                    self._link(eid, pid, url=s["url"], platform=s.get("platform_family"))
                continue
            fresh.append(s)

        self.seeds = fresh
        attempted = 0
        for seed in fresh:
            if attempted >= self.max_seed_attempts:
                break
            self._visited.discard(_norm_url(seed["url"]).lower())
            self.visit(seed, is_secondary=False)
            attempted += 1

        self.revisit_unknown_and_degraded()

        reports = self.build_reports(
            seeds_loaded=len(expansion_seeds),
            seeds_attempted=attempted + len(self.revisit_results),
        )
        # Opportunity sample from Sweep #1 is truncated; preserve baseline total + new unique
        baseline_opp = int(
            self.baseline.get("opportunity_count_baseline")
            or self.baseline["baseline_counts"].get("unique_opportunities")
            or 0
        )
        reports["coverage"]["unique_opportunities_discovered"] = baseline_opp + len(
            self.sweep2_new_opportunities
        )
        reports["run"]["opportunity_count"] = reports["coverage"]["unique_opportunities_discovered"]
        # Prefer explicit LIBRARY typing in entities_by_type
        et = reports["coverage"].setdefault("entities_by_type", {})
        et["library"] = sum(
            1 for e in self.entities.values() if e.get("entity_type") == "LIBRARY"
        )
        et["port"] = sum(
            1 for e in self.entities.values() if e.get("entity_type") == "PORT_AUTHORITY"
        )
        reports["coverage"]["sweep"] = "2"
        reports["coverage"]["build"] = BUILD_TAG
        reports["coverage"]["baseline_sweep1"] = self.baseline["baseline_counts"]
        reports["coverage"]["sweep2_new_portals"] = len(self.sweep2_new_portals)
        reports["coverage"]["sweep2_new_entities"] = len(self.sweep2_new_entities)
        reports["coverage"]["sweep2_new_opportunities"] = len(self.sweep2_new_opportunities)
        reports["coverage"]["revisits"] = len(self.revisit_results)
        reports["coverage"]["revisit_results_sample"] = self.revisit_results[:40]
        reports["run"]["sweep"] = "2"
        reports["run"]["build"] = BUILD_TAG
        reports["run"]["expansion_seeds_catalog_size"] = len(expansion_seeds)
        reports["run"]["expansion_seeds_fresh"] = len(fresh)
        reports["run"]["revisit_results"] = self.revisit_results

        paths = self.write_artifacts(reports)
        paths.update(self.write_expansion_report(reports))
        try:
            self.registry.save()
        except Exception:
            pass
        return {"reports": reports, "artifact_paths": paths, "build": BUILD_TAG}

    def write_expansion_report(self, reports: dict[str, Any]) -> dict[str, str]:
        cov = reports["coverage"]
        before = cov.get("baseline_sweep1") or SWEEP1_BASELINE
        after_et = cov.get("entities_by_type") or {}

        def row(label: str, b: Any, a: Any) -> str:
            try:
                delta = int(a) - int(b)
                sign = f"+{delta}" if delta >= 0 else str(delta)
            except Exception:
                sign = "n/a"
            return f"| {label} | {b} | {a} | {sign} |"

        lines = [
            "# M3 Source Coverage Expansion Report — Sweep #2",
            "",
            f"Build: `{BUILD_TAG}`",
            f"Generated: {cov.get('generated_at')}",
            "",
            "## FIRST SWEEP (baseline)",
            "",
            f"- Portals: {before.get('unique_portals')}",
            f"- Entities: {before.get('unique_entities')}",
            f"- Opportunities: {before.get('unique_opportunities')}",
            f"- K-12: {before.get('k12')} · Counties: {before.get('county')} · "
            f"Cities: {before.get('city')} · Special districts: {before.get('special_district')}",
            "",
            "## NEWLY DISCOVERED IN SWEEP #2",
            "",
            f"- New portals: {cov.get('sweep2_new_portals')}",
            f"- New entities: {cov.get('sweep2_new_entities')}",
            f"- New opportunities (deduped): {cov.get('sweep2_new_opportunities')}",
            f"- UNKNOWN/DEGRADED revisits: {cov.get('revisits')}",
            "",
            "## BEFORE vs AFTER",
            "",
            "| Metric | Sweep #1 | After Sweep #2 | Delta |",
            "|--------|----------|----------------|-------|",
            row("Unique portals", before.get("unique_portals"), cov.get("unique_procurement_portals")),
            row("Unique entities", before.get("unique_entities"), cov.get("unique_government_entities")),
            row("Opportunities", before.get("unique_opportunities"), cov.get("unique_opportunities_discovered")),
            row("K-12", before.get("k12"), after_et.get("k12")),
            row("Special districts", before.get("special_district"), after_et.get("special_district")),
            row("Counties", before.get("county"), after_et.get("county")),
            row("Cities", before.get("city"), after_et.get("city")),
            row("Higher-ed", before.get("higher_education"), after_et.get("higher_education")),
            row("Airports", before.get("airport"), after_et.get("airport")),
            row("Transit", before.get("transit"), after_et.get("transit")),
            row("Utilities", before.get("utility"), after_et.get("utility")),
            row("Housing", before.get("housing"), after_et.get("housing")),
            row("Cooperatives", before.get("cooperative"), after_et.get("cooperative")),
            "",
            "## Access after Sweep #2",
            "",
        ]
        for k, v in sorted((cov.get("access_breakdown") or {}).items(), key=lambda x: str(x[0])):
            lines.append(f"- {k}: {v}")
        lines += ["", "## Blind spots remaining", ""]
        for g in cov.get("biggest_blind_spots") or []:
            lines.append(f"- **{g['gap']}**: {g['detail']}")
        lines += [
            "",
            "## Honesty",
            "",
            "- Sweep #2 expands coverage from seeds — not a complete US census.",
            "- Paid aggregators are not primary M3 sources.",
            "- Entity-map directories are not portal coverage until an official portal is resolved.",
            "",
        ]
        path = self.artifacts_dir / "source_coverage_expansion_report.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return {"source_coverage_expansion_report.md": str(path)}


def run_coverage_expansion(**kwargs: Any) -> dict[str, Any]:
    return CoverageExpansionSweep(**kwargs).run()
