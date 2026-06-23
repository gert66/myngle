#!/usr/bin/env python3
"""
parse_abn_xml.py
================
Streaming parser for the Australian Business Register (ABR)
ABN Bulk Extract XML files.

Downloads: https://data.gov.au/data/dataset/abn-bulk-extract

Usage
-----
  # Full run (all .xml files in a folder):
  python parse_abn_xml.py --input_folder "C:/ABN" --output_csv "abn.csv"

  # Test on one file, first 10 000 records only:
  python parse_abn_xml.py --input_folder "C:/ABN" --output_csv "abn_test.csv" --limit 10000

  # Inspect raw XML structure of first 3 ABR records (no CSV written):
  python parse_abn_xml.py --input_folder "C:/ABN" --inspect --inspect_count 3

README
------
Installation
  pip install lxml          # optional but ~3x faster; falls back to stdlib
  # No other dependencies beyond the Python standard library.

Running
  python parse_abn_xml.py --input_folder FOLDER --output_csv OUTPUT.csv

  Options:
    --input_folder   Folder containing one or more *.xml ABR extract files.
    --output_csv     Path for the output CSV file (UTF-8).
    --limit N        Stop after N records total (useful for testing). 0 = no limit.
    --inspect        Print raw XML of the first few ABR records and exit.
    --inspect_count  How many records to show with --inspect (default 3).

Output columns
  abn                  11-digit Australian Business Number
  abn_status           ACT = active, CAN = cancelled
  abn_status_date      Date the ABN status last changed (YYYYMMDD)
  entity_type_code     Short code, e.g. PRV, PUB, IND, PTR, TRT …
  entity_type_text     Human-readable entity type, e.g. "Australian Private Company"
  legal_name           Legal name (individual: "GivenName FamilyName"; company: registered name)
  main_name            Main trading/operating name registered against the ABN
  business_names       ASIC-registered business names, pipe-separated if multiple
  trading_names        Other trading names, pipe-separated if multiple
  state                Australian state/territory abbreviation, e.g. NSW, VIC
  postcode             Australian postcode
  gst_status           ACT = registered for GST, CAN = cancelled, NON = never
  gst_status_date      Date GST status last changed (YYYYMMDD)
  acn_arbn             ASIC company/body number (9 digits)
  acn_arbn_type        ACN or ARBN
"""

import argparse
import csv
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------------------------------------------------------------------
# Try lxml for speed; fall back to stdlib iterparse
# ---------------------------------------------------------------------------
try:
    from lxml import etree as _lxml_etree
    _USE_LXML = True
except ImportError:
    _USE_LXML = False


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

FIELDNAMES = [
    "abn",
    "abn_status",
    "abn_status_date",
    "entity_type_code",
    "entity_type_text",
    "legal_name",
    "main_name",
    "business_names",
    "trading_names",
    "state",
    "postcode",
    "gst_status",
    "gst_status_date",
    "acn_arbn",
    "acn_arbn_type",
]


# ---------------------------------------------------------------------------
# Record extractor
# ---------------------------------------------------------------------------

def _txt(elem, path: str) -> str:
    """Return stripped text of a sub-element, or '' if missing."""
    found = elem.find(path)
    if found is None or found.text is None:
        return ""
    return found.text.strip()


def parse_abr_record(elem) -> dict:
    """
    Extract all required fields from a single <ABR> element.

    Handles both individual (IndividualName) and non-individual
    (NonIndividualName) legal entities.
    """
    record = {f: "" for f in FIELDNAMES}

    # ── ABN ──────────────────────────────────────────────────────────────────
    abn_el = elem.find("ABN")
    if abn_el is not None:
        record["abn"]             = (abn_el.text or "").strip()
        record["abn_status"]      = abn_el.get("status", "")
        record["abn_status_date"] = abn_el.get("ABNStatusFromDate", "")

    # ── Entity type ───────────────────────────────────────────────────────────
    et = elem.find("EntityType")
    if et is not None:
        record["entity_type_code"] = _txt(et, "EntityTypeInd")
        record["entity_type_text"] = _txt(et, "EntityTypeText")

    # ── Main entity (trading name + address) ──────────────────────────────────
    me = elem.find("MainEntity")
    if me is not None:
        # Main name — NonIndividualName type="MN"
        for nin in me.findall("NonIndividualName"):
            if nin.get("type") == "MN":
                record["main_name"] = _txt(nin, "NonIndividualNameText")
                break
        # Address
        addr = me.find(".//AddressDetails")
        if addr is not None:
            record["state"]    = _txt(addr, "State")
            record["postcode"] = _txt(addr, "Postcode")

    # ── Legal entity ──────────────────────────────────────────────────────────
    le = elem.find("LegalEntity")
    if le is not None:
        # Individual (sole trader, etc.)
        ind = None
        for name_el in le.findall("IndividualName"):
            if name_el.get("type") == "LGL":
                ind = name_el
                break
        if ind is not None:
            parts = []
            for tag in ("GivenName", "OtherGivenName", "FamilyName"):
                v = _txt(ind, tag)
                if v:
                    parts.append(v)
            record["legal_name"] = " ".join(parts)
        else:
            # Company / organisation
            for nin in le.findall("NonIndividualName"):
                if nin.get("type") == "LGL":
                    record["legal_name"] = _txt(nin, "NonIndividualNameText")
                    break

    # ── GST ───────────────────────────────────────────────────────────────────
    gst = elem.find("GST")
    if gst is not None:
        record["gst_status"]      = _txt(gst, "GSTstatus")
        record["gst_status_date"] = _txt(gst, "GSTStatusFromDate")

    # ── ACN / ARBN ────────────────────────────────────────────────────────────
    asic = elem.find("ASICNumber")
    if asic is not None:
        record["acn_arbn"]      = (asic.text or "").strip()
        record["acn_arbn_type"] = asic.get("type", "")

    # ── Trading names (OtherEntity with type TRD / OTN) ──────────────────────
    trading: list[str] = []
    for oe in elem.findall("OtherEntity"):
        for nin in oe.findall("NonIndividualName"):
            if nin.get("type") in ("TRD", "OTN"):
                name = _txt(nin, "NonIndividualNameText")
                if name:
                    trading.append(name)
    # Also catch trading names directly under the ABR element
    for nin in elem.findall("OtherEntity/NonIndividualName"):
        pass  # already covered above

    record["trading_names"] = " | ".join(trading)

    # ── Registered business names (BusinessName elements) ────────────────────
    bnames: list[str] = []
    for bn in elem.findall("BusinessName"):
        # ABR schema uses <OrganisationName> inside <BusinessName>
        org = bn.find("OrganisationName")
        if org is not None and org.text:
            bnames.append(org.text.strip())
    record["business_names"] = " | ".join(bnames)

    return record


# ---------------------------------------------------------------------------
# Streaming iterparse (stdlib or lxml)
# ---------------------------------------------------------------------------

def _iterparse_file(xml_path: str):
    """
    Yield (event, elem) pairs using lxml if available, else stdlib.
    Requests 'end' events only — caller must call elem.clear() after use.
    """
    if _USE_LXML:
        context = _lxml_etree.iterparse(xml_path, events=("end",), recover=True)
    else:
        context = ET.iterparse(xml_path, events=("start", "end"))
    return context


def process_xml_file(
    xml_path: str,
    writer: csv.DictWriter,
    processed_total: int,
    limit: int = 0,
) -> int:
    """
    Stream-parse one XML file, extract ABR records, write to CSV.
    Returns updated processed_total count.
    """
    print(f"\nProcessing: {xml_path}")
    file_count = 0

    if _USE_LXML:
        # lxml iterparse — fast, recovers from minor XML errors
        context = _lxml_etree.iterparse(xml_path, events=("end",), recover=True)
        for event, elem in context:
            tag = elem.tag
            # Strip namespace prefix if present: {ns}ABR → ABR
            if "}" in tag:
                tag = tag.split("}", 1)[1]
            if tag == "ABR":
                record = parse_abr_record(elem)
                writer.writerow(record)
                file_count += 1
                processed_total += 1
                elem.clear()
                # Free ancestors to avoid memory buildup
                while elem.getparent() is not None:
                    parent = elem.getparent()
                    if parent.getparent() is not None:
                        parent.getparent().remove(parent)
                    break
                if processed_total % 100_000 == 0:
                    print(f"  ... {processed_total:,} records processed")
                if limit > 0 and processed_total >= limit:
                    break
    else:
        # stdlib iterparse — get root from first start event to enable root.clear()
        context = ET.iterparse(xml_path, events=("start", "end"))
        context_iter = iter(context)
        try:
            _, root = next(context_iter)
        except StopIteration:
            print("  Warning: empty or unreadable file.")
            return processed_total

        for event, elem in context_iter:
            if event == "end" and elem.tag == "ABR":
                record = parse_abr_record(elem)
                writer.writerow(record)
                file_count += 1
                processed_total += 1
                root.clear()  # free memory — clears all children including current ABR
                if processed_total % 100_000 == 0:
                    print(f"  ... {processed_total:,} records processed")
                if limit > 0 and processed_total >= limit:
                    break

    print(f"  Done: {file_count:,} records from this file.")
    return processed_total


# ---------------------------------------------------------------------------
# Inspect mode — show raw XML of first N ABR records
# ---------------------------------------------------------------------------

def inspect_xml(xml_path: str, count: int = 3) -> None:
    """Print pretty-printed raw XML for the first `count` <ABR> elements."""
    print(f"\n=== Inspecting: {xml_path} (first {count} ABR records) ===\n")
    seen = 0

    if _USE_LXML:
        context = _lxml_etree.iterparse(xml_path, events=("end",), recover=True)
        for event, elem in context:
            tag = elem.tag
            if "}" in tag:
                tag = tag.split("}", 1)[1]
            if tag == "ABR":
                print(_lxml_etree.tostring(elem, pretty_print=True).decode())
                seen += 1
                elem.clear()
                if seen >= count:
                    break
    else:
        context = ET.iterparse(xml_path, events=("start", "end"))
        context_iter = iter(context)
        _, root = next(context_iter)
        for event, elem in context_iter:
            if event == "end" and elem.tag == "ABR":
                ET.indent(elem)   # requires Python 3.9+
                print(ET.tostring(elem, encoding="unicode"))
                print()
                seen += 1
                root.clear()
                if seen >= count:
                    break

    if seen == 0:
        print("No <ABR> records found. Check that the file uses the ABR Bulk Extract schema.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Parse ABN Bulk Extract XML files to CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--input_folder", required=True,
        help="Folder containing one or more *.xml ABR extract files.",
    )
    ap.add_argument(
        "--output_csv", default="",
        help="Output CSV file path (UTF-8). Required unless --inspect is set.",
    )
    ap.add_argument(
        "--limit", type=int, default=0,
        help="Stop after this many records total (0 = no limit). Useful for testing.",
    )
    ap.add_argument(
        "--inspect", action="store_true",
        help="Print raw XML of the first few ABR records and exit (no CSV written).",
    )
    ap.add_argument(
        "--inspect_count", type=int, default=3,
        help="Number of records to show with --inspect (default 3).",
    )
    args = ap.parse_args()

    input_folder = Path(args.input_folder)
    xml_files = sorted(input_folder.glob("*.xml"))
    if not xml_files:
        print(f"ERROR: No .xml files found in: {input_folder}")
        sys.exit(1)

    print(f"Parser engine : {'lxml (fast)' if _USE_LXML else 'stdlib xml.etree.ElementTree'}")
    print(f"Found {len(xml_files)} XML file(s) in {input_folder}")

    # ── Inspect mode ──────────────────────────────────────────────────────────
    if args.inspect:
        inspect_xml(str(xml_files[0]), count=args.inspect_count)
        sys.exit(0)

    # ── CSV export mode ───────────────────────────────────────────────────────
    if not args.output_csv:
        print("ERROR: --output_csv is required unless --inspect is set.")
        sys.exit(1)

    output_path = Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()

        for xml_file in xml_files:
            total = process_xml_file(
                str(xml_file), writer, total, limit=args.limit,
            )
            if args.limit > 0 and total >= args.limit:
                print(f"\nLimit of {args.limit:,} records reached.")
                break

    print(f"\n{'='*60}")
    print(f"Total records written : {total:,}")
    print(f"Output file           : {output_path}")
    print(f"Encoding              : UTF-8")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
