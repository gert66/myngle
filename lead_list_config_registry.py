"""Central caller/country registry for Lead Lists and GCS country targets."""
from __future__ import annotations

import re

DEFAULT_COLD_CALLERS = [
    "Carla", "Luana", "Elisabetta", "Nicole", "Giulio", "Pietro", "Esmirna",
]

COUNTRY_LABELS = [
    "Australia", "Austria", "Brazil", "Germany", "Italy", "Japan",
    "Luxembourg", "Netherlands", "New Zealand", "South Korea", "Spain",
    "Switzerland", "Test", "Uruguay",
]

DISABLED_COUNTRY_LABELS = {"Austria", "Japan", "Luxembourg", "South Korea", "Test"}

_COUNTRY_FOLDER_SLUGS = {
    "new zealand": "newzealand",
    "south korea": "south-korea",
}


def country_folder_slug(country: str) -> str:
    norm = re.sub(r"\s+", " ", str(country or "").strip().lower())
    if norm in _COUNTRY_FOLDER_SLUGS:
        return _COUNTRY_FOLDER_SLUGS[norm]
    slug = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")
    return slug or "unknown"
