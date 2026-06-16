"""
test_lusha_lookup.py
--------------------
CLI test harness for the Lusha contact lookup pipeline.

Default mode is --mock (no Lusha credits consumed).
Pass --live to call the real Lusha API (requires LUSHA_API_KEY env var).

Examples:
    python test_lusha_lookup.py --mock --company-name "Demo Logistics" \\
        --domain "demo-logistics.com" --industry "Logistics"

    python test_lusha_lookup.py --live --company-name "Acme Corp" \\
        --domain "acme.com" --country "NL" --industry "Manufacturing"
"""

import argparse
import json
import sys

import lusha_ranker as lr

# ---------------------------------------------------------------------------
# Mock data — 7 realistic contacts with mixed titles for ranking tests
# ---------------------------------------------------------------------------

_MOCK_CONTACTS = [
    {
        "name":        "Sofia Esposito",
        "jobTitle":    "HR Director",
        "department":  "Human Resources",
        "seniority":   "Director",
        "email":       "",
        "phone":       "",
        "linkedinUrl": "https://www.linkedin.com/in/sofia-esposito",
        "matchReason": "",
        "confidence":  0.0,
        "_lushaId":    "mock-001",
    },
    {
        "name":        "Luca Ferretti",
        "jobTitle":    "Head of Learning & Development",
        "department":  "L&D",
        "seniority":   "Head",
        "email":       "",
        "phone":       "",
        "linkedinUrl": "https://www.linkedin.com/in/luca-ferretti",
        "matchReason": "",
        "confidence":  0.0,
        "_lushaId":    "mock-002",
    },
    {
        "name":        "Marco Visser",
        "jobTitle":    "Operations Manager",
        "department":  "Operations",
        "seniority":   "Manager",
        "email":       "",
        "phone":       "",
        "linkedinUrl": "",
        "matchReason": "",
        "confidence":  0.0,
        "_lushaId":    "mock-003",
    },
    {
        "name":        "Ines Bakker",
        "jobTitle":    "Talent Acquisition Specialist",
        "department":  "HR",
        "seniority":   "Specialist",
        "email":       "",
        "phone":       "",
        "linkedinUrl": "https://www.linkedin.com/in/ines-bakker",
        "matchReason": "",
        "confidence":  0.0,
        "_lushaId":    "mock-004",
    },
    {
        "name":        "Jan de Vries",
        "jobTitle":    "CEO",
        "department":  "General Management",
        "seniority":   "C-Suite",
        "email":       "",
        "phone":       "",
        "linkedinUrl": "https://www.linkedin.com/in/jan-de-vries",
        "matchReason": "",
        "confidence":  0.0,
        "_lushaId":    "mock-005",
    },
    {
        "name":        "Francesca Ricci",
        "jobTitle":    "Marketing Coordinator",
        "department":  "Marketing",
        "seniority":   "Coordinator",
        "email":       "",
        "phone":       "",
        "linkedinUrl": "",
        "matchReason": "",
        "confidence":  0.0,
        "_lushaId":    "mock-006",
    },
    {
        "name":        "Thomas Krause",
        "jobTitle":    "Procurement Director",
        "department":  "Procurement",
        "seniority":   "Director",
        "email":       "",
        "phone":       "",
        "linkedinUrl": "https://www.linkedin.com/in/thomas-krause",
        "matchReason": "",
        "confidence":  0.0,
        "_lushaId":    "mock-007",
    },
]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test the Lusha contact lookup pipeline."
    )
    parser.add_argument("--company-name", default="")
    parser.add_argument("--domain",       default="")
    parser.add_argument("--country",      default=None)
    parser.add_argument("--industry",     default=None)
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use local mock contacts (default; no API calls, no credits used).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Call real Lusha API (requires LUSHA_API_KEY env var).",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    # Default to mock unless --live is explicitly passed
    use_live = args.live and not args.mock

    if use_live:
        # Import here so a missing key only errors in live mode
        import lusha_client as lc  # noqa: PLC0415

        print(f"[live] Calling Lusha for: {args.company_name or args.domain}", file=sys.stderr)
        try:
            raw_contacts = lc.find_contacts(
                company_name=args.company_name,
                domain=args.domain,
                country=args.country,
            )
        except RuntimeError as exc:
            print(f"[error] {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        print("[mock] Using local mock contacts — no Lusha credits consumed.", file=sys.stderr)
        raw_contacts = _MOCK_CONTACTS

    ranked = lr.rank_contacts_for_myngle(raw_contacts, industry=args.industry)

    # Strip internal _lushaId before printing
    clean = [{k: v for k, v in c.items() if not k.startswith("_")} for c in ranked]

    output = {
        "status":   "ok",
        "source":   "mock" if not use_live else "Lusha",
        "company":  args.company_name or args.domain,
        "industry": args.industry,
        "contacts": clean,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
