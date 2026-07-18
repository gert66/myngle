"""Generate the Lovable countries index manifest (``countries.index.json``).

Standalone script, no Streamlit dependency — writes the manifest locally and,
only when explicitly asked with ``--upload``, uploads it to the bucket root
via the existing ``gcloud``/``gsutil`` helper in ``lovable_gcs_upload.py`` (no
new Google Cloud SDK/Python dependency). Lovable reads this manifest to decide
which countries to show; it must never scan the bucket itself, so only
entries with ``"enabled": true`` are meant to be displayed.

Usage:
    python generate_lovable_countries_index.py --output-dir lovable_json_exports/countries_index
    python generate_lovable_countries_index.py --output-dir lovable_json_exports/countries_index --upload
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

from lovable_gcs_upload import (
    COUNTRIES_INDEX_FILENAME,
    CURRENT_CACHE_CONTROL,
    DEFAULT_GCS_BUCKET,
    check_gcloud_available,
    country_folder_slug,
    describe_gcloud_environment,
    gcs_manifest_path,
    public_manifest_url,
    resolve_gcs_upload_tool,
    upload_file,
)

# Countries shown in the Lovable manifest, in display order. Kept in sync with
# SUPPORTED_DEFAULT_INPUT_COUNTRIES in lead_prioritizer_batch_app.py (checked
# by test_generate_lovable_countries_index.py) without importing that module,
# which pulls in the full enrichment stack and is slow to import.
MANIFEST_COUNTRY_LABELS = [
    "Australia", "Brazil", "Germany", "Italy", "Japan", "Luxembourg",
    "Netherlands", "New Zealand", "South Korea", "Spain", "Switzerland",
    "Test", "Uruguay",
]

# Countries present in the manifest but not yet ready for Lovable to show.
DISABLED_COUNTRY_LABELS = {"Japan", "South Korea", "Test"}


def _manifest_id(label: str) -> str:
    """Stable Lovable-facing country id: a plain kebab-case of ``label``.

    Kept independent of ``country_folder_slug`` so a GCS folder-naming quirk
    (e.g. New Zealand's bucket folder being ``newzealand``, not
    ``new-zealand``) never changes the id Lovable already relies on.
    """
    return re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")


def build_countries_manifest(bucket: str = DEFAULT_GCS_BUCKET) -> dict:
    """Build the ``countries.index.json`` manifest dict for ``bucket``.

    Every supported country is included so new ones can be wired up ahead of
    time; ``enabled`` controls whether Lovable should display it.
    """
    countries = []
    for label in MANIFEST_COUNTRY_LABELS:
        slug = country_folder_slug(label)
        countries.append({
            "id": _manifest_id(label),
            "label": label,
            "enabled": label not in DISABLED_COUNTRY_LABELS,
            "baseUrl": f"https://storage.googleapis.com/{bucket}/{slug}/current",
        })
    return {"countries": countries}


def write_manifest(manifest: dict, output_dir: Path) -> Path:
    """Write ``manifest`` as pretty-printed JSON into ``output_dir`` (created
    if missing) and return the written file path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / COUNTRIES_INDEX_FILENAME
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate (and optionally upload) the Lovable countries.index.json manifest.",
    )
    p.add_argument("--output-dir", required=True,
                   help="Local directory to write countries.index.json into.")
    p.add_argument("--bucket", default=DEFAULT_GCS_BUCKET,
                   help=f"GCS bucket name (default: {DEFAULT_GCS_BUCKET}).")
    p.add_argument("--upload", action="store_true",
                   help="Upload countries.index.json to the bucket root after writing it "
                        "locally. Off by default.")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    manifest = build_countries_manifest(args.bucket)
    output_path = write_manifest(manifest, Path(args.output_dir))
    enabled = [c["id"] for c in manifest["countries"] if c["enabled"]]
    disabled = [c["id"] for c in manifest["countries"] if not c["enabled"]]

    print(f"Manifest written  : {output_path}")
    print(f"Enabled countries : {', '.join(enabled)}")
    print(f"Disabled countries: {', '.join(disabled)}")

    if not args.upload:
        print("Upload skipped (pass --upload to upload to GCS).")
        return 0

    tool_info = check_gcloud_available()
    if not tool_info["available"]:
        print(
            "ERROR: neither gcloud nor gsutil was found on PATH. Install or "
            "authenticate the Google Cloud SDK and try again.",
            file=sys.stderr,
        )
        return 2

    env = describe_gcloud_environment()
    print(f"gcloud account    : {env['account'] or '(none active)'}")
    print(f"gcloud project    : {env['project'] or '(none set)'}")

    tool_cmd = resolve_gcs_upload_tool()
    destination = gcs_manifest_path(args.bucket)
    result = upload_file(tool_cmd, str(output_path), destination, cache_control=CURRENT_CACHE_CONTROL)
    if not result["success"]:
        print(f"ERROR: upload failed: {result.get('error') or result.get('stderr')}",
              file=sys.stderr)
        return 2

    print(f"Uploaded to       : {destination}")
    print(f"Public URL        : {public_manifest_url(args.bucket)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
