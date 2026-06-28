"""Plain-English labels for NAICS codes used by GovTracker."""

NAICS_LABELS: dict[str, str] = {
    "561720": "Janitorial Services",
    "561210": "Facilities Support Services",
    "561730": "Landscaping Services",
    "561710": "Pest Control Services",
    "561790": "Building Maintenance (Other)",
    "561612": "Security Guard Services",
}


def naics_label(code: str | None) -> str:
    if not code:
        return "Unknown"
    return NAICS_LABELS.get(str(code).strip(), "Other Services")


def naics_display(code: str | None) -> str:
    if not code:
        return "Unknown"
    label = naics_label(code)
    return f"{code} — {label}"
