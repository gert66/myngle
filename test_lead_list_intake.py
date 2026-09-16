from pathlib import Path

import pandas as pd

from lead_list_intake import analyze_lead_list, detect_mapping, normalize_domain


def test_luana_style_semicolon_contact_csv(tmp_path: Path):
    path = tmp_path / "luana.csv"
    pd.DataFrame([
        {"Contact name": "Alexandra Cicco", "Work email": "alexandra@viseca.ch", "Direct phone": "+41 58 1", "Mobile": "+41 79 1", "Company name": "Viseca", "LinkedIn profile": "https://linkedin.com/in/a"},
        {"Contact name": "Other Person", "Work email": "other@viseca.ch", "Direct phone": "+41 58 2", "Company name": "Viseca", "LinkedIn profile": "https://linkedin.com/in/b"},
    ]).to_csv(path, sep=";", index=False, encoding="cp1252")

    result = analyze_lead_list(path, default_country="Switzerland")
    assert result.report["source_rows"] == 2
    assert result.report["unique_companies"] == 1
    assert result.report["duplicate_company_rows"] == 1
    assert result.report["contact_rows"] == 2
    assert result.companies.iloc[0]["company_name"] == "Viseca"
    assert result.companies.iloc[0]["contact_count"] == 2


def test_xlsx_finds_header_below_banner_and_best_sheet(tmp_path: Path):
    path = tmp_path / "elisabetta.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame([["Read me"], ["Nothing useful"]]).to_excel(
            writer, sheet_name="Instructions", header=False, index=False
        )
        pd.DataFrame([
            ["Elisabetta current list", None, None, None],
            [None, None, None, None],
            ["Company Name", "Website", "Contact name", "Job title"],
            ["Acme GmbH", "https://www.acme.com/jobs", "Jane Doe", "HR Director"],
        ]).to_excel(writer, sheet_name="Leads", header=False, index=False)

    result = analyze_lead_list(path, default_country="Germany")
    assert result.source_sheet == "Leads"
    assert result.header_row == 2
    assert result.report["source_rows"] == 1
    assert result.report["unique_companies"] == 1
    assert result.companies.iloc[0]["domain"] == "acme.com"


def test_mapping_requires_company_name_for_automatic_processing(tmp_path: Path):
    path = tmp_path / "contacts.csv"
    path.write_text("Work email;Mobile\na@example.com;+31123\n", encoding="utf-8")
    result = analyze_lead_list(path, default_country="Netherlands")
    assert result.report["ready_for_protection_checks"] is False
    assert result.report["review_rows"] == 1
    assert result.report["unique_companies"] == 0
    assert any("company-name" in warning for warning in result.report["warnings"])


def test_domain_and_vat_identity_precede_name_country(tmp_path: Path):
    path = tmp_path / "mixed.csv"
    pd.DataFrame([
        {"Company": "Acme AG", "Website": "www.acme.ch/about", "VAT Number": "CHE-123"},
        {"Company": "ACME", "Website": "https://acme.ch", "VAT Number": "CHE999"},
        {"Company": "No Site SA", "Website": "", "VAT Number": "CHE-555"},
    ]).to_csv(path, index=False)

    result = analyze_lead_list(path, default_country="Switzerland")
    assert result.report["unique_companies"] == 2
    keys = set(result.companies["company_key"])
    assert "domain:acme.ch" in keys
    assert "vat:CHE555" in keys
    assert normalize_domain("https://www.acme.ch/about?q=1") == "acme.ch"


def test_detect_mapping_accepts_common_contact_headers():
    df = pd.DataFrame(columns=[
        "Company name", "Contact name", "Work email", "Direct phone",
        "Mobile", "Mobile 2", "Job title", "LinkedIn profile",
    ])
    mapping = detect_mapping(df)
    assert mapping["company_name"] == "Company name"
    assert mapping["contact_name"] == "Contact name"
    assert mapping["work_email"] == "Work email"
    assert mapping["direct_phone"] == "Direct phone"
    assert mapping["mobile_2"] == "Mobile 2"
    assert mapping["contact_linkedin_url"] == "LinkedIn profile"


def test_lusha_company_fields_override_contact_fields_and_direct_email():
    df = pd.DataFrame(columns=[
        "Country", "City", "Direct Email", "LinkedIn URL",
        "Company Name", "Company Domain", "Company Country", "Company City",
    ])
    mapping = detect_mapping(df)
    assert mapping["company_name"] == "Company Name"
    assert mapping["domain"] == "Company Domain"
    assert mapping["country"] == "Company Country"
    assert mapping["city"] == "Company City"
    assert mapping["work_email"] == "Direct Email"


def test_domain_only_row_is_kept_and_email_domain_is_hint_only(tmp_path: Path):
    path = tmp_path / "domain_only.csv"
    pd.DataFrame([
        {"Company Name": "", "Company Domain": "acme.example", "Direct Email": "person@acme.example"},
    ]).to_csv(path, index=False)
    result = analyze_lead_list(path, default_country="Italy")
    assert result.report["valid_rows"] == 1
    assert result.report["review_rows"] == 0
    assert result.report["unique_companies"] == 1
    assert result.companies.iloc[0]["domain"] == "acme.example"
    assert result.companies.iloc[0]["needs_domain_resolution"] == False


def test_free_text_in_direct_email_is_not_treated_as_contact_email(tmp_path: Path):
    path = tmp_path / "notes_in_email.csv"
    pd.DataFrame([
        {"Company Name": "Acme", "Company Domain": "acme.it", "Direct Email": "8/09 NO ANSWER"},
    ]).to_csv(path, index=False)
    result = analyze_lead_list(path, default_country="Italy")
    assert result.normalized_rows.iloc[0]["work_email"] == ""
    assert result.companies.iloc[0]["contact_email_count"] == 0


def test_lusha_contact_export_uses_company_location_and_valid_email_fallback(tmp_path: Path):
    path = tmp_path / "lusha_contacts.xlsx"
    pd.DataFrame([{
        "First Name": "Silvia", "Last Name": "Grasso",
        "Direct Email": "8/09 NO ANSWER", "Additional Email 1": "silvia@example.it",
        "Phone 1": "+39 333 111", "Phone 1 Type": "mobile",
        "Phone 2": "+39 02 555", "Phone 2 Type": "work",
        "Country": "Switzerland", "City": "Lugano",
        "Company Name": "Example S.p.A.", "Company Domain": "example.it",
        "Company Country": "Italy", "Company City": "Milan",
        "Company Number of Employees": "201-500", "Company Main Industry": "Manufacturing",
        "Company LinkedIn URL": "https://linkedin.com/company/example",
    }]).to_excel(path, index=False)

    result = analyze_lead_list(path)
    row = result.normalized_rows.iloc[0]
    assert row["country"] == "Italy"
    assert row["city"] == "Milan"
    assert row["work_email"] == "silvia@example.it"
    assert row["mobile"] == "+39 333 111"
    assert row["direct_phone"] == "+39 02 555"
    assert result.companies.iloc[0]["employee_range"] == "201-500"
