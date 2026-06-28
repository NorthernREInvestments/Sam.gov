"""Distance helpers for comparable contract locations."""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import quote

import httpx

ZIP_GEOCODER = "https://api.zippopotam.us/us"
_COORD_CACHE: dict[str, tuple[float, float] | None] = {}

# Approximate geographic centers — used when ZIP geocoding is unavailable.
STATE_CENTROIDS: dict[str, tuple[float, float]] = {
    "AL": (32.806671, -86.791130),
    "AK": (61.370716, -152.404419),
    "AZ": (33.729759, -111.431221),
    "AR": (34.969704, -92.373123),
    "CA": (36.116203, -119.681564),
    "CO": (39.059811, -105.311104),
    "CT": (41.597782, -72.755371),
    "DC": (38.905985, -77.017052),
    "DE": (39.318523, -75.507141),
    "FL": (27.766279, -81.686783),
    "GA": (33.040619, -83.643074),
    "HI": (21.094318, -157.498337),
    "ID": (44.240459, -114.478828),
    "IL": (40.349457, -88.986137),
    "IN": (39.849426, -86.258278),
    "IA": (42.011539, -93.210526),
    "KS": (38.526600, -96.726486),
    "KY": (37.668140, -84.670067),
    "LA": (31.169546, -91.867805),
    "ME": (44.693947, -69.381927),
    "MD": (39.063946, -76.802101),
    "MA": (42.230171, -71.530106),
    "MI": (43.326618, -84.536095),
    "MN": (45.694454, -93.900192),
    "MS": (32.741646, -89.678696),
    "MO": (38.572954, -92.189283),
    "MT": (46.921925, -110.454353),
    "NE": (41.125370, -98.268082),
    "NV": (38.313515, -117.055374),
    "NH": (43.452492, -71.563896),
    "NJ": (40.298904, -74.521011),
    "NM": (34.840515, -106.248482),
    "NY": (42.165726, -74.948051),
    "NC": (35.630066, -79.806419),
    "ND": (47.528912, -99.784012),
    "OH": (40.388783, -82.764915),
    "OK": (35.565342, -96.928917),
    "OR": (44.572021, -122.070938),
    "PA": (40.590752, -77.209755),
    "RI": (41.680893, -71.511780),
    "SC": (33.856892, -80.945007),
    "SD": (44.299782, -99.438828),
    "TN": (35.747845, -86.692345),
    "TX": (31.054487, -97.563461),
    "UT": (40.150032, -111.862434),
    "VT": (44.045876, -72.710686),
    "VA": (37.769337, -78.169968),
    "WA": (47.400902, -121.490494),
    "WV": (38.491226, -80.954453),
    "WI": (44.268543, -89.616508),
    "WY": (42.755966, -107.302490),
}


def _normalize_city(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value.strip().lower())
    return cleaned or None


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_miles = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return radius_miles * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _cache_key(city: str | None, state_code: str | None, zip_code: str | None) -> str:
    return "|".join(
        [
            _normalize_city(city) or "",
            (state_code or "").upper(),
            (zip_code or "")[:5],
        ]
    )


def _coords_from_zippopotam(path: str) -> tuple[float, float] | None:
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(f"{ZIP_GEOCODER}/{path}")
            if response.status_code != 200:
                return None
            places = response.json().get("places") or []
            if not places:
                return None
            place = places[0]
            return float(place["latitude"]), float(place["longitude"])
    except Exception:
        return None


def resolve_coordinates(
    *,
    city: str | None = None,
    state_code: str | None = None,
    zip_code: str | None = None,
) -> tuple[float, float] | None:
    """Resolve lat/lon from ZIP (preferred) or city + state."""
    key = _cache_key(city, state_code, zip_code)
    if key in _COORD_CACHE:
        return _COORD_CACHE[key]

    coords: tuple[float, float] | None = None
    zip_clean = (zip_code or "")[:5]
    if zip_clean.isdigit() and len(zip_clean) == 5:
        coords = _coords_from_zippopotam(zip_clean)

    if coords is None and city and state_code:
        coords = _coords_from_zippopotam(f"{state_code.lower()}/{quote(city.strip())}")

    _COORD_CACHE[key] = coords
    return coords


def format_distance_label(
    *,
    miles: float | None,
    same_state: bool,
    target_state: str | None,
    target_state_name: str | None = None,
    approximate: bool = False,
) -> str:
    if miles is not None:
        rounded = max(0, int(round(miles)))
        prefix = "~" if approximate else ""
        if rounded == 0:
            return "Same area"
        if same_state:
            return f"{prefix}{rounded} mi"
        state_label = target_state_name or target_state or "Other state"
        return f"{state_label} · {prefix}{rounded} mi"
    if same_state:
        return "Same state"
    if target_state:
        return f"Different state ({target_state_name or target_state})"
    return "Distance unknown"


def annotate_award_distances(
    awards: list[dict[str, Any]],
    origin: dict[str, Any],
    *,
    state_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Add distance fields to each award and sort closest first."""
    state_names = state_names or {}
    origin_state = origin.get("state_code")
    origin_city = _normalize_city(origin.get("city"))
    origin_coords = resolve_coordinates(
        city=origin.get("city"),
        state_code=origin_state,
        zip_code=origin.get("zip"),
    )

    for award in awards:
        target_state = award.get("performance_state") or origin_state
        target_city = _normalize_city(award.get("performance_city"))
        target_coords = resolve_coordinates(
            city=award.get("performance_city"),
            state_code=target_state,
            zip_code=award.get("performance_zip"),
        )
        same_state = bool(origin_state and target_state and origin_state == target_state)
        same_city = bool(same_state and origin_city and target_city and origin_city == target_city)
        approximate = False
        miles: float | None = None

        if same_city:
            miles = 0.0
        elif origin_coords and target_coords:
            miles = haversine_miles(origin_coords[0], origin_coords[1], target_coords[0], target_coords[1])
            approximate = not award.get("performance_zip") and not origin.get("zip")
        elif origin_state and target_state and origin_state != target_state:
            origin_centroid = origin_coords or STATE_CENTROIDS.get(origin_state)
            target_centroid = target_coords or STATE_CENTROIDS.get(target_state)
            if origin_centroid and target_centroid:
                miles = haversine_miles(
                    origin_centroid[0],
                    origin_centroid[1],
                    target_centroid[0],
                    target_centroid[1],
                )
                approximate = True

        target_state_name = state_names.get(target_state, target_state) if target_state else None
        award["distance_miles"] = round(miles, 1) if miles is not None else None
        award["distance_same_state"] = same_state
        award["distance_approximate"] = approximate
        award["distance_label"] = format_distance_label(
            miles=miles,
            same_state=same_state,
            target_state=target_state,
            target_state_name=target_state_name,
            approximate=approximate,
        )

    awards.sort(
        key=lambda item: (
            item.get("distance_miles") is None,
            item.get("distance_miles") if item.get("distance_miles") is not None else float("inf"),
            item.get("award_date") or "",
        )
    )
    return awards
