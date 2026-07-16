"""register_new_country.py — Add a new country to every place the pipeline
hardcodes the supported-country list, in one command.

Adding a country is deliberately NOT self-service from a running app (see
country_visibility_app.py's docstring and
test_country_visibility_app.py::test_unknown_country_in_live_manifest_is_dropped):
the dispatch dropdown (SUPPORTED_DEFAULT_INPUT_COUNTRIES) gates which country
an enrichment run's search/localization uses, so it stays a reviewed code
change. This script automates that reviewed change instead of hand-editing
four files, and never touches GCS/current data — a brand new country's GCS
folder is created automatically by the first real export (GCS folders are
just object-name prefixes, nothing to pre-create).

Patches, in order:
  1. SUPPORTED_DEFAULT_INPUT_COUNTRIES  in lead_prioritizer_batch_app.py
  2. MANIFEST_COUNTRY_LABELS            in generate_lovable_countries_index.py
  3. DISABLED_COUNTRY_LABELS            in generate_lovable_countries_index.py (opt-in via --enabled)
  4. _COUNTRY_FOLDER_SLUGS              in lovable_gcs_upload.py
  5. test_exact_list's hardcoded list   in test_lead_prioritizer_batch_app.py

All-or-nothing: every file is validated (syntax-parseable) before any file is
written, and the affected pytest files are run afterwards; a failure prints
the pytest output and leaves the edits in place for inspection rather than
silently reverting, since a human should look at a failure here.

Usage:
    python register_new_country.py --label Luxembourg
    python register_new_country.py --label Luxembourg --enabled
    python register_new_country.py --label Luxembourg --dry-run
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).parent

BATCH_APP = REPO_ROOT / "lead_prioritizer_batch_app.py"
BATCH_APP_TEST = REPO_ROOT / "test_lead_prioritizer_batch_app.py"
MANIFEST_GEN = REPO_ROOT / "generate_lovable_countries_index.py"
GCS_UPLOAD = REPO_ROOT / "lovable_gcs_upload.py"


def insert_alphabetically(items: list[str], new_item: str) -> list[str]:
    """Return a new, alphabetically sorted list with ``new_item`` added.

    Case-insensitively de-duplicates: if ``new_item`` (or a case-variant of
    it) is already present, the original list comes back unchanged."""
    if any(existing.lower() == new_item.lower() for existing in items):
        return list(items)
    return sorted([*items, new_item])


def render_wrapped_list(items: list[str], indent: str = "    ", width: int = 78) -> str:
    """Render a comma-separated, quoted string list wrapped across lines,
    matching this codebase's existing formatting style for these lists."""
    quoted = [f'"{item}"' for item in items]
    lines: list[str] = []
    current = indent
    for i, token in enumerate(quoted):
        piece = token + ("," if i < len(quoted) - 1 else ",")
        candidate = f"{current} {piece}" if current != indent else f"{current}{piece}"
        if len(candidate) > width and current != indent:
            lines.append(current)
            current = f"{indent}{piece}"
        else:
            current = candidate
    if current != indent:
        lines.append(current)
    return "\n".join(lines)


def patch_list_literal(
    text: str, var_name: str, new_items: list[str], assign_op: str = "=",
    indent: str = "    ", closing_indent: str = "",
) -> str:
    """Replace ``var_name <assign_op> [...]``'s contents with ``new_items``,
    rendered via :func:`render_wrapped_list`. ``assign_op`` is ``"="`` for a
    plain assignment or ``"=="`` for an ``assert x == [...]`` literal.
    ``indent``/``closing_indent`` match the literal's actual nesting (a
    module-level list starts its items and closing ``]`` at column 0; a list
    nested inside a function body needs both shifted to match). Raises
    ``ValueError`` if the literal can't be found, so a format change is never
    silently ignored."""
    pattern = re.compile(
        rf"({re.escape(var_name)}\s*{re.escape(assign_op)}\s*)\[.*?\]", re.DOTALL)
    if not pattern.search(text):
        raise ValueError(f"Could not find list literal for {var_name!r}.")
    replacement = f"[\n{render_wrapped_list(new_items, indent=indent)}\n{closing_indent}]"
    return pattern.sub(lambda m: m.group(1) + replacement.replace("\\", "\\\\"), text, count=1)


def patch_set_literal(text: str, var_name: str, new_items: set[str]) -> str:
    """Replace a one-line ``var_name = {...}`` set literal's contents."""
    pattern = re.compile(rf"({re.escape(var_name)}\s*=\s*)\{{.*?\}}", re.DOTALL)
    if not pattern.search(text):
        raise ValueError(f"Could not find set literal for {var_name!r}.")
    rendered = ", ".join(f'"{item}"' for item in sorted(new_items))
    replacement = "{" + rendered + "}"
    return pattern.sub(lambda m: m.group(1) + replacement.replace("\\", "\\\\"), text, count=1)


def patch_dict_entry(text: str, dict_name: str, key: str, value: str) -> str:
    """Insert ``"key": "value",`` as a new line just before ``dict_name``'s
    closing ``}``, unless ``key`` is already present in the dict body."""
    pattern = re.compile(rf"({re.escape(dict_name)}\s*=\s*\{{)(.*?)(\n\}})", re.DOTALL)
    match = pattern.search(text)
    if not match:
        raise ValueError(f"Could not find dict literal for {dict_name!r}.")
    body = match.group(2)
    if re.search(rf'"{re.escape(key)}"\s*:', body):
        return text
    new_body = f'{body.rstrip(chr(10))}\n    "{key}": "{value}",'
    return text[: match.start()] + match.group(1) + new_body + match.group(3) + text[match.end():]


def country_folder_slug_local(label: str) -> str:
    """Same slugging rule as lovable_gcs_upload.country_folder_slug, kept
    local so this script has no import-time dependency on the pipeline's
    heavy modules for the plain string logic."""
    norm = re.sub(r"\s+", " ", label.strip().lower())
    slug = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")
    return slug or "unknown"


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--label", required=True, help='Country label, e.g. "Luxembourg".')
    p.add_argument("--enabled", action="store_true",
                    help="Show it in the Lovable country picker immediately "
                         "(default: added disabled, matching how every other "
                         "new country has been rolled out so far).")
    p.add_argument("--dry-run", action="store_true",
                    help="Print what would change without writing any file.")
    p.add_argument("--skip-tests", action="store_true",
                    help="Skip running the affected pytest files afterwards.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    label = args.label.strip()
    if not label:
        print("ERROR: --label must not be blank.", file=sys.stderr)
        return 2

    sys.path.insert(0, str(REPO_ROOT))
    from lead_prioritizer_batch_app import SUPPORTED_DEFAULT_INPUT_COUNTRIES
    from generate_lovable_countries_index import MANIFEST_COUNTRY_LABELS, DISABLED_COUNTRY_LABELS
    from lovable_gcs_upload import _COUNTRY_FOLDER_SLUGS

    if any(existing.lower() == label.lower() for existing in SUPPORTED_DEFAULT_INPUT_COUNTRIES):
        print(f"{label!r} is already a supported country — nothing to do.")
        return 0

    new_batch_list = insert_alphabetically(SUPPORTED_DEFAULT_INPUT_COUNTRIES, label)
    new_manifest_list = insert_alphabetically(MANIFEST_COUNTRY_LABELS, label)
    new_disabled = set(DISABLED_COUNTRY_LABELS) | ({label} if not args.enabled else set())
    slug = country_folder_slug_local(label)

    edits = [
        (BATCH_APP, lambda t: patch_list_literal(t, "SUPPORTED_DEFAULT_INPUT_COUNTRIES", new_batch_list)),
        (MANIFEST_GEN, lambda t: patch_list_literal(t, "MANIFEST_COUNTRY_LABELS", new_manifest_list)),
        (MANIFEST_GEN, lambda t: patch_set_literal(t, "DISABLED_COUNTRY_LABELS", new_disabled)),
        (GCS_UPLOAD, lambda t: patch_dict_entry(t, "_COUNTRY_FOLDER_SLUGS", label.lower(), slug)),
        (BATCH_APP_TEST, lambda t: patch_list_literal(
            t, "assert SUPPORTED_DEFAULT_INPUT_COUNTRIES", new_batch_list, assign_op="==",
            indent=" " * 12, closing_indent=" " * 8)),
    ]

    new_contents: dict[Path, str] = {}
    for path, transform in edits:
        current = new_contents.get(path, path.read_text(encoding="utf-8"))
        try:
            new_contents[path] = transform(current)
        except ValueError as exc:
            print(f"ERROR patching {path.name}: {exc}", file=sys.stderr)
            return 2

    for path, content in new_contents.items():
        try:
            ast.parse(content)
        except SyntaxError as exc:
            print(f"ERROR: patched {path.name} would not be valid Python: {exc}", file=sys.stderr)
            return 2

    print(f"Registering {label!r} (slug: {slug!r}, "
          f"{'enabled' if args.enabled else 'disabled'} in Lovable):")
    for path in new_contents:
        print(f"  - {path.relative_to(REPO_ROOT)}")

    if args.dry_run:
        print("\n--dry-run: no files written.")
        return 0

    for path, content in new_contents.items():
        path.write_text(content, encoding="utf-8")

    print(
        f"\nDone. GCS folder gs://.../{slug}/current/ needs no manual creation "
        f"-- it appears automatically the first time a run for {label!r} is "
        f"exported."
    )

    if args.skip_tests:
        return 0

    test_files = [
        "test_lead_prioritizer_batch_app.py",
        "test_generate_lovable_countries_index.py",
        "test_lovable_gcs_upload.py",
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *test_files, "-q"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        print("WARNING: tests failed after the edit -- inspect the diff "
              "(files were still written; nothing was auto-reverted).",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
