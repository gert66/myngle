"""
promote_run_to_current.py — Make an already-uploaded run folder live
======================================================================
Copies every file from ``gs://<bucket>/<country_folder>/runs/<run_folder>/``
to ``gs://<bucket>/<country_folder>/current/`` — the deliberate, explicit
step that ``rescore_from_gcs.py`` and ``reallocate_callers_from_gcs.py`` both
document but never perform themselves, since a bad re-score/reallocation
should always have a fallback until an operator has reviewed it.

Thin CLI over ``rescore_from_gcs.promote_run_to_current`` — works for a
re-score run, a reallocation run, or any other run folder using the same
three-file layout (``companies.list.json``, ``company-details-*.json``,
manifest). Never touches any run folder other than the one named.

Usage:
    python promote_run_to_current.py --country brazil --run-folder 2026-07-09_reallocate
"""

from __future__ import annotations

import argparse
import sys

from lovable_gcs_upload import DEFAULT_GCS_BUCKET
from rescore_from_gcs import promote_run_to_current


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", default=DEFAULT_GCS_BUCKET)
    ap.add_argument("--country", required=True, help="Country-folder slug, e.g. 'brazil'.")
    ap.add_argument(
        "--run-folder", required=True,
        help="Run folder to promote, e.g. '2026-07-09_reallocate'.")
    args = ap.parse_args()

    print(f"\n{'='*72}")
    print("Promote run folder to current/")
    print(f"  bucket      : {args.bucket}")
    print(f"  country     : {args.country}")
    print(f"  run folder  : {args.run_folder}")
    print(f"{'='*72}\n")

    try:
        result = promote_run_to_current(args.bucket, args.country, args.run_folder)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    n_failed = sum(1 for r in result["results"] if not r["success"])
    for r in result["results"]:
        status = "OK" if r["success"] else "FAILED"
        print(f"  {status:<8} {r['destination']}")

    if n_failed:
        print(f"\n{n_failed} of {len(result['results'])} file(s) failed to promote.",
              file=sys.stderr)
        sys.exit(1)

    print(
        f"\n{len(result['results'])} file(s) promoted to "
        f"gs://{args.bucket}/{args.country}/current/"
    )


if __name__ == "__main__":
    main()
