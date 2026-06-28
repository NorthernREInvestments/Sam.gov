"""Google Places API (New) — Text Search for subcontractor discovery."""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.addressComponents",
        "places.nationalPhoneNumber",
        "places.rating",
        "places.userRatingCount",
        "places.websiteUri",
        "places.googleMapsUri",
        "places.location",
    ]
)


class PlacesApiError(Exception):
    pass


def _api_key() -> str:
    key = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    if not key:
        raise PlacesApiError("GOOGLE_PLACES_API_KEY is missing from .env")
    return key


def _parse_address_components(components: list[dict[str, Any]] | None) -> dict[str, str | None]:
    city = state = zip_code = None
    for comp in components or []:
        types = comp.get("types") or []
        short = comp.get("shortText") or comp.get("short_name") or ""
        long = comp.get("longText") or comp.get("long_name") or ""
        if "locality" in types:
            city = long or short
        elif "administrative_area_level_1" in types:
            state = short.upper() if short else None
        elif "postal_code" in types:
            zip_code = short or long
    return {"city": city, "state": state, "zip": zip_code}


def _parse_address_fallback(formatted: str) -> dict[str, str | None]:
    city = state = zip_code = None
    if formatted:
        zip_match = re.search(r"\b(\d{5})(?:-\d{4})?\b", formatted)
        if zip_match:
            zip_code = zip_match.group(1)
        parts = [p.strip() for p in formatted.split(",") if p.strip()]
        if len(parts) >= 2:
            state_zip = parts[-1]
            m = re.match(r"([A-Z]{2})\s+(\d{5})?", state_zip)
            if m:
                state = m.group(1)
            if len(parts) >= 3:
                city = parts[-2]
    return {"city": city, "state": state, "zip": zip_code}


def normalize_place(raw: dict[str, Any], *, distance_miles: float | None = None) -> dict[str, Any]:
    display = raw.get("displayName") or {}
    name = display.get("text") if isinstance(display, dict) else str(display or "")
    formatted = raw.get("formattedAddress") or ""
    parsed = _parse_address_components(raw.get("addressComponents"))
    if not parsed.get("city") and formatted:
        parsed = _parse_address_fallback(formatted)

    place_id = raw.get("id") or raw.get("name", "").replace("places/", "")
    maps_url = raw.get("googleMapsUri")
    if not maps_url and place_id:
        maps_url = f"https://www.google.com/maps/place/?q=place_id:{place_id}"

    loc = raw.get("location") or {}
    return {
        "place_id": place_id,
        "business_name": name,
        "phone": raw.get("nationalPhoneNumber"),
        "rating": float(raw["rating"]) if raw.get("rating") is not None else None,
        "review_count": int(raw["userRatingCount"]) if raw.get("userRatingCount") is not None else None,
        "address": formatted,
        "city": parsed.get("city"),
        "state": parsed.get("state"),
        "zip": parsed.get("zip"),
        "website": raw.get("websiteUri"),
        "google_maps_url": maps_url,
        "latitude": loc.get("latitude"),
        "longitude": loc.get("longitude"),
        "distance_miles": round(distance_miles, 1) if distance_miles is not None else None,
    }


def search_text(
    text_query: str,
    *,
    latitude: float,
    longitude: float,
    radius_miles: float,
    max_results: int = 20,
) -> list[dict[str, Any]]:
    """Run Places Text Search (New) biased to a circle around lat/lng."""
    radius_m = max(500.0, radius_miles * 1609.34)
    payload = {
        "textQuery": text_query,
        "maxResultCount": max(1, min(20, max_results)),
        "locationBias": {
            "circle": {
                "center": {"latitude": latitude, "longitude": longitude},
                "radius": radius_m,
            }
        },
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": _api_key(),
        "X-Goog-FieldMask": FIELD_MASK,
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.post(TEXT_SEARCH_URL, json=payload, headers=headers)
        if response.status_code >= 400:
            detail = response.text[:500]
            raise PlacesApiError(f"Google Places error {response.status_code}: {detail}")
        data = response.json()

    from geo import haversine_miles

    places: list[dict[str, Any]] = []
    for raw in data.get("places") or []:
        loc = raw.get("location") or {}
        lat = loc.get("latitude")
        lng = loc.get("longitude")
        dist = None
        if lat is not None and lng is not None:
            dist = haversine_miles(latitude, longitude, float(lat), float(lng))
        places.append(normalize_place(raw, distance_miles=dist))
    return places
