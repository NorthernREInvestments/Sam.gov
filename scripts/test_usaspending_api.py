"""Quick USAspending API filter comparison."""
import httpx

BASE = "https://api.usaspending.gov/api/v2/search/spending_by_award/"


def post(filters: dict, label: str) -> None:
    body = {
        "filters": filters,
        "fields": ["Award ID", "Recipient Name", "Award Amount"],
        "limit": 5,
        "page": 1,
    }
    with httpx.Client(timeout=45.0) as client:
        response = client.post(BASE, json=body)
    if response.status_code == 200:
        results = response.json().get("results") or []
        first = results[0].get("Award ID") if results else None
        print(f"{label}: {response.status_code} count={len(results)} first={first}")
    else:
        print(f"{label}: {response.status_code} {response.text[:150]}")


if __name__ == "__main__":
    post(
        {"award_ids": ["W911SA21P3048"], "award_type_codes": ["A", "B", "C", "D"]},
        "award_ids plain",
    )
    post(
        {"award_ids": ['"W911SA21P3048"'], "award_type_codes": ["A", "B", "C", "D"]},
        "award_ids quoted",
    )
    post(
        {"award_ids": ["W911SA21P3048", "W911SA21P3048"], "award_type_codes": ["A", "B", "C", "D"]},
        "award_ids dup",
    )
    post(
        {
            "award_ids": ['"W911SA21P3048"', "W911SA21P3048"],
            "award_type_codes": ["A", "B", "C", "D"],
        },
        "award_ids quoted+plain",
    )
