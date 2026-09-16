from pathlib import Path

import pandas as pd

from lead_list_intake import analyze_lead_list, detect_mapping, normalize_domain


def test_luana_style_list_is_green_and_preserves_call_history(tmp_path: Path):
    path = tmp_path / "luana.csv"
    pd.DataFrame([
        {"Contact name": "Alexandra Cicco", "Work email": "alexandra@viseca.ch", "Direct phone": "+41 58 1 234 567", "Mobile": "+41 79 1 234 567", "Called": "yes", "Reached": "no", "I/NI": "", "Notes ": "", "Email": "yes", "Called 2": "", "Reached 2": "", "Company name": "Viseca", "LinkedIn profile": "https://linkedin.com/in/a"},
        {"Contact name": "Other Person", "Work email": "other@viseca.ch", "Direct phone": "+41 58 2 234 567", "Called": "yes", "Reached": "yes", "I/NI": "maybe", "Notes ": "Call after holiday", "Email": "no", "Company name": "Viseca", "LinkedIn profile": "https://linkedin.com/in/b"},
    ]).to_csv(path, sep=";", index=False, encoding="cp1252")

    result = analyze_lead_list(path, default_country="Switzerland", assigned_caller="Luana")
    assert result.report["quality_status"] == "GREEN"
    assert result.report["decision"] == "READY"
    assert result.report["unique_companies"] == 1
    assert result.report["contact_rows"] == 2
    assert result.mapping["email_sent"] == "Email"
    assert result.normalized_rows.iloc[1]["interest_status"] == "maybe"
    assert result.normalized_rows.iloc[1]["call_notes"] == "Call after holiday"
    assert result.report["assigned_caller"] == "Luana"


def test_xlsx_finds_header_below_banner_and_best_sheet(tmp_path: Path):
    path = tmp_path / "elisabetta.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame([["Read me"], ["Nothing useful"]]).to_excel(writer, sheet_name="Instructions", header=False, index=False)
        pd.DataFrame([
            ["Elisabetta current list", None, None, None, None],
            [None, None, None, None, None],
            ["Company Name", "Website", "Contact name", "Job title", "Country"],
            ["Acme GmbH", "https://www.acme.com/jobs", "Jane Doe", "HR Director", "Germany"],
        ]).to_excel(writer, sheet_name="Leads", header=False, index=False)

    result = analyze_lead_list(path, assigned_caller="Elisabetta")
    assert result.source_sheet == "Leads"
    assert result.header_row == 2
    assert result.report["quality_status"] == "GREEN"
    assert result.companies.iloc[0]["domain"] == "acme.com"


def test_missing_company_name_column_is_red(tmp_path: Path):
    path = tmp_path / "contacts.csv"
    path.write_text("Work email;Mobile\na@example.com;+31123456789\n", encoding="utf-8")
    result = analyze_lead_list(path, default_country="Netherlands", assigned_caller="Carla")
    assert result.report["quality_status"] == "RED"
    assert result.report["ready_for_protection_checks"] is False
    assert any("company-name" in reason for reason in result.report["hard_stops"])


def test_caller_must_be_selected_at_upload(tmp_path: Path):
    path = tmp_path / "contacts.csv"
    pd.DataFrame([{"Company name": "Acme", "Country": "Italy", "Contact name": "Jane Doe"}]).to_csv(path, index=False)
    result = analyze_lead_list(path)
    assert result.report["quality_status"] == "RED"
    assert any("Caller must be selected" in reason for reason in result.report["hard_stops"])


def test_domain_and_vat_identity_precede_name_country(tmp_path: Path):
    path = tmp_path / "mixed.csv"
    pd.DataFrame([
        {"Company": "Acme AG", "Website": "www.acme.ch/about", "VAT Number": "CHE-123", "Contact name": "A", "Country": "Switzerland"},
        {"Company": "ACME", "Website": "https://acme.ch", "VAT Number": "CHE999", "Contact name": "B", "Country": "Switzerland"},
        {"Company": "No Site SA", "Website": "", "VAT Number": "CHE-555", "Contact name": "C", "Country": "Switzerland"},
    ]).to_csv(path, index=False)

    result = analyze_lead_list(path, assigned_caller="Luana")
    assert result.report["unique_companies"] == 2
    keys = set(result.companies["company_key"])
    assert "domain:acme.ch" in keys
    assert "vat:CHE555" in keys
    assert normalize_domain("https://www.acme.ch/about?q=1") == "acme.ch"


def test_detect_mapping_accepts_common_and_lusha_headers():
    df = pd.DataFrame(columns=[
        "User", "First Name", "Last Name", "Direct Email", "Phone 1", "Phone 2",
        "Job Title", "LinkedIn URL", "Company Name", "Company Domain", "Company Country",
        "Company City", "Company Website", "Company Number of Employees", "Company Main Industry",
    ])
    mapping = detect_mapping(df)
    assert mapping["company_name"] == "Company Name"
    assert mapping["domain"] == "Company Domain"
    assert mapping["country"] == "Company Country"
    assert mapping["work_email"] == "Direct Email"
    assert mapping["phone_1"] == "Phone 1"
    assert mapping["phone_2"] == "Phone 2"
    assert mapping["source_assignee_hint"] == "User"
    assert mapping["company_website"] == "Company Website"


def test_domain_only_company_name_gap_is_amber_auto_repair(tmp_path: Path):
    path = tmp_path / "domain_only.csv"
    pd.DataFrame([
        {"Company Name": "Known Co", "Company Domain": "known.example", "Country": "Italy", "Direct Email": "known@known.example", "LinkedIn URL": "https://linkedin.com/in/k"},
        {"Company Name": "", "Company Domain": "acme.example", "Country": "Italy", "Direct Email": "person@acme.example", "LinkedIn URL": "https://linkedin.com/in/a"},
    ]).to_csv(path, index=False)
    result = analyze_lead_list(path, assigned_caller="Elisabetta")
    assert result.report["quality_status"] == "AMBER"
    assert result.report["decision"] == "AUTO_REPAIR"
    assert result.report["review_rows"] == 1
    assert result.report["unique_companies"] == 2


def test_free_text_in_email_fields_is_amber_review_and_preserved(tmp_path: Path):
    path = tmp_path / "messy_lusha.csv"
    pd.DataFrame([
        {"User": "Pietro Rivalta", "Company Name": "Acme", "Company Domain": "acme.it", "Company Country": "Italy", "First Name": "Jane", "Last Name": "Doe", "Direct Email": "8/09 NO ANSWER", "Work Email Confidence": "13/04 no answer", "Phone 1": "+39 333 1234567"},
        {"User": "Pietro Rivalta", "Company Name": "Beta", "Company Domain": "beta.it", "Company Country": "Italy", "First Name": "John", "Last Name": "Doe", "Direct Email": "email mandata 28.01", "Work Email Confidence": "A+", "Phone 1": "+39 333 7654321"},
        {"User": "Pietro Rivalta", "Company Name": "Gamma", "Company Domain": "gamma.it", "Company Country": "Italy", "First Name": "Jo", "Last Name": "Smith", "Direct Email": "BUDGET TAGLIATO", "Work Email Confidence": "No persona di riferimento", "Phone 1": "+39 333 1111111"},
    ]).to_csv(path, index=False)
    result = analyze_lead_list(path, assigned_caller="Elisabetta")
    assert result.report["quality_status"] == "AMBER"
    assert result.report["decision"] == "REVIEW_REQUIRED"
    assert result.report["assigned_caller"] == "Elisabetta"
    assert result.report["source_assignee_ignored"] is True
    assert any("Pietro Rivalta" in warning for warning in result.report["warnings"])
    assert result.normalized_rows.iloc[0]["work_email"] == ""
    assert "NO ANSWER" in result.normalized_rows.iloc[0]["legacy_call_notes"]
    assert any(i["code"] == "email_field_contamination" for i in result.report["semantic_issues"])


def test_small_bad_tail_is_amber_but_large_bad_share_is_red(tmp_path: Path):
    amber = tmp_path / "amber.csv"
    rows = [{"Company name": f"Co{i}", "Country": "Italy", "Contact name": f"Person {i}"} for i in range(30)]
    rows.append({"Company name": "", "Country": "Italy", "Contact name": "Bad"})
    pd.DataFrame(rows).to_csv(amber, index=False)
    a = analyze_lead_list(amber, assigned_caller="Carla")
    assert a.report["quality_status"] == "AMBER"

    red = tmp_path / "red.csv"
    rows = [{"Company name": f"Co{i}", "Country": "Italy", "Contact name": f"Person {i}"} for i in range(10)]
    rows += [{"Company name": "", "Country": "Italy", "Contact name": f"Bad {i}"} for i in range(3)]
    pd.DataFrame(rows).to_csv(red, index=False)
    r = analyze_lead_list(red, assigned_caller="Carla")
    assert r.report["quality_status"] == "RED"


def test_lusha_fallback_email_and_typed_phones(tmp_path: Path):
    path = tmp_path / "lusha.csv"
    pd.DataFrame([{
        "Company Name": "Acme",
        "Company Domain": "acme.example",
        "Company Country": "Italy",
        "First Name": "Jane",
        "Last Name": "Doe",
        "Direct Email": "12/09 no answer",
        "Additional Email 1": "jane@acme.example",
        "Phone 1": "+39 02 1234567",
        "Phone 1 Type": "Work",
        "Phone 2": "+39 333 1234567",
        "Phone 2 Type": "Mobile",
    }]).to_csv(path, index=False)
    result = analyze_lead_list(path, assigned_caller="Elisabetta")
    row = result.normalized_rows.iloc[0]
    assert row["work_email"] == "jane@acme.example"
    assert row["direct_phone"] == "+39 02 1234567"
    assert row["mobile"] == "+39 333 1234567"
    assert "no answer" in row["legacy_call_notes"].lower()
