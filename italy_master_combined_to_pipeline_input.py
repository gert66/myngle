"""
italy_master_combined_to_pipeline_input.py
--------------------------------------------
One-off adapter: turns the externally-matched Italy "Master Combined" sheet
(Lusha vs Chamber of Commerce, already deduped/matched by domain+name in
Excel -- see Italy_Companies_Lusha_vs_Chamber_of_Commerce_Master.xlsx) into
two pipeline-ready input files:

  - <out-prefix>_lusha.xlsx         -- rows with Lusha data (In Lusha ==
    Yes, including "Both" rows -- they carry the richer Lusha
    firmographics), shaped for input_cleaner_lusha_edition.py's
    detect_lusha_columns().
  - <out-prefix>_chamber_only.xlsx  -- Chamber-of-Commerce-only rows (no
    Lusha match), shaped for input_cleaner_register_edition.py's
    detect_columns() (Italy/IT_CONFIG path).

Every row in both files gets a `channels` column: a semicolon-joined list
of "lusha" / "chamber_of_commerce" derived from the master sheet's own
"In Lusha" / "In Chamber of Commerce" columns -- not a closed enum, so a
future third channel (e.g. Apollo) just becomes another possible list
entry, no redesign needed. Neither downstream cleaner drops unrecognized
columns (confirmed for the Lusha cleaner via a smoke test, and for the
register cleaner via lead_prioritizer_batch_core.flatten_result_for_excel's
``dict(original_row)`` passthrough further down the pipeline), so
`channels` survives untouched all the way to the Enriched Leads output
and, from there, into the Lovable export
(export_lead_prioritizer_to_lovable_json.py).

This script is deliberately Italy-specific, not a generic pipeline stage
-- the Lusha-vs-Chamber matching itself stays a one-off manual Excel
exercise for now; only the resulting `channels` field is meant to be
reusable pipeline plumbing for a future country/source combination.

Run with:
    python italy_master_combined_to_pipeline_input.py \\
        --master-xlsx "C:\\path\\to\\Italy_Companies_Lusha_vs_Chamber_of_Commerce_Master.xlsx" \\
        --out-prefix italy_input
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

_SHEET_NAME = "Master Combined"
_CHANNEL_LUSHA = "lusha"
_CHANNEL_CHAMBER = "chamber_of_commerce"
_MULTI_VALUE_SEP = " | "  # Master Combined's own convention for Record Count > 1


def _derive_channels(row: pd.Series) -> str:
    channels = []
    if str(row.get("In Lusha", "")).strip().lower() == "yes":
        channels.append(_CHANNEL_LUSHA)
    if str(row.get("In Chamber of Commerce", "")).strip().lower() == "yes":
        channels.append(_CHANNEL_CHAMBER)
    return ";".join(channels)


def _first_value(raw) -> str:
    """Master Combined joins multi-value fields with ' | ' when a company
    has more than one record from a source (e.g. Chamber Record Count > 1)
    -- take the first for a single-valued cleaner input column."""
    s = str(raw if raw is not None else "").strip()
    if not s or s.lower() == "nan":
        return ""
    return s.split(_MULTI_VALUE_SEP)[0].strip()


def build_lusha_shaped(df: pd.DataFrame) -> pd.DataFrame:
    """Rows with Lusha data (In Lusha == Yes, includes "Both" rows) ->
    columns input_cleaner_lusha_edition.detect_lusha_columns() recognizes
    (see its _LUSHA_COLUMN_CANDIDATES)."""
    return pd.DataFrame({
        "Company Name":                 df["Lusha Company Name(s)"].apply(_first_value),
        "Company Domain":               df["Normalized Domain"],
        "Company Description":          df["Lusha Company Description"],
        "Company Number of Employees":  df["Lusha Employee Range(s)"].apply(_first_value),
        "Company Revenue":              df["Lusha Revenue"],
        "Company Main Industry":        df["Lusha Main Industry"],
        "Company Sub Industry":         df["Lusha Sub Industry"],
        "Company Country":              df["Lusha Country"],
        "Company Intent Topics":        df["Lusha Intent Topics"],
        "Company LinkedIn URL":         df["Lusha LinkedIn URL(s)"].apply(_first_value),
        "channels":                     df["channels"],
        "master_id":                    df["Master ID"],
    })


def build_chamber_only_shaped(df: pd.DataFrame) -> pd.DataFrame:
    """Chamber-only rows (no Lusha match) -> columns
    input_cleaner_register_edition.detect_columns() (Italy/IT_CONFIG path)
    recognizes (_REG_COL_COMPANY / _REG_COL_WEBSITE exact-match names, used
    directly here to guarantee detection). No email/city/province/postcode
    exists in the master sheet for these rows -- validate_register_row()
    treats all of those as optional, best-effort inputs, and only spends
    Serper/Haiku budget on the ~6% of Chamber-only rows with no domain at
    all (confirmed empirically, 478 of 8026 in the current master file)."""
    return pd.DataFrame({
        "Company Name": df["Chamber Company Name(s)"].apply(_first_value),
        "Website":      df["Chamber Original Domain(s)"].apply(_first_value),
        "channels":     df["channels"],
        "master_id":    df["Master ID"],
    })


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--master-xlsx", required=True,
                         help="Path to the Master Combined workbook.")
    parser.add_argument("--out-prefix", required=True,
                         help="Output file prefix, e.g. 'italy_input' -> "
                              "italy_input_lusha.xlsx / italy_input_chamber_only.xlsx")
    args = parser.parse_args()

    df = pd.read_excel(args.master_xlsx, sheet_name=_SHEET_NAME)
    df["channels"] = df.apply(_derive_channels, axis=1)

    missing_channels = df[df["channels"] == ""]
    if len(missing_channels):
        sys.exit(
            f"{len(missing_channels)} row(s) have neither 'In Lusha' nor "
            "'In Chamber of Commerce' set to Yes -- fix the source sheet "
            "before proceeding (Master IDs: "
            f"{list(missing_channels['Master ID'].head(10))}"
            f"{'...' if len(missing_channels) > 10 else ''})."
        )

    is_lusha = df["In Lusha"].astype(str).str.strip().str.lower() == "yes"
    lusha_out = build_lusha_shaped(df[is_lusha])
    chamber_out = build_chamber_only_shaped(df[~is_lusha])

    lusha_path = f"{args.out_prefix}_lusha.xlsx"
    chamber_path = f"{args.out_prefix}_chamber_only.xlsx"
    lusha_out.to_excel(lusha_path, index=False)
    chamber_out.to_excel(chamber_path, index=False)

    print(f"Lusha-shaped rows (incl. Both): {len(lusha_out)} -> {lusha_path}")
    print(f"Chamber-only rows:              {len(chamber_out)} -> {chamber_path}")
    print(f"Total:                          {len(df)}")


if __name__ == "__main__":
    main()
