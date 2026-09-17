"""Pure intake/normalization layer for self-service Sales Cockpit lead lists.

This module does no enrichment, no HubSpot writes, and no Cockpit publication.
It turns an arbitrary XLSX/CSV contact/company list into two deterministic
worksets: normalized source rows and unique companies ready for protection
checks and enrichment.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
import tempfile
import unicodedata
import zipfile
from typing import Any, Mapping

import pandas as pd

SUPPORTED_SUFFIXES = {".xlsx", ".csv"}
MAX_HEADER_SCAN_ROWS = 15


def _norm_header(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode().lower().strip()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()

ALIASES: dict[str, tuple[str, ...]] = {
    "company_name": (
        "company name", "company", "account name", "account", "organization",
        "organisation", "business name", "employer", "company_name",
    ),
    "domain": (
        "domain", "company domain", "website", "website url", "company website",
        "company url", "homepage", "web site", "url",
    ),
    "country": ("country", "company country", "country name", "hq country"),
    "city": ("city", "company city", "location city", "town"),
    "vat": ("vat", "vat number", "vat id", "tax id", "company vat"),
    "contact_name": (
        "contact name", "full name", "person name", "contact", "name",
    ),
    "first_name": ("first name", "firstname", "given name"),
    "last_name": ("last name", "lastname", "surname", "family name"),
    "work_email": (
        "work email", "direct email", "business email", "email address", "contact email",
    ),
    "additional_email_1": ("additional email 1", "alternate email 1"),
    "additional_email_2": ("additional email 2", "alternate email 2"),
    "direct_phone": ("direct phone", "direct phone number", "phone", "phone number"),
    "mobile": ("mobile", "mobile phone", "cell", "cell phone", "mobile 1"),
    "mobile_2": ("mobile 2", "second mobile", "alternate mobile"),
    "phone_1": ("phone 1",),
    "phone_1_type": ("phone 1 type",),
    "phone_2": ("phone 2",),
    "phone_2_type": ("phone 2 type",),
    "job_title": ("job title", "title", "position", "role"),
    "contact_linkedin_url": (
        "linkedin profile", "linkedin profile url", "contact linkedin",
        "contact linkedin url", "linkedin url", "linkedin",
    ),
    "company_linkedin_url": ("company linkedin url", "company linkedin", "company linkedin profile"),
    "company_website": ("company website", "website url", "website"),
    "employee_range": ("company number of employees", "employee range", "employees", "company size"),
    "industry": ("company main industry", "industry", "main industry"),
    "company_revenue": ("company revenue", "revenue"),
    "sub_industry": ("company sub industry", "sub industry", "sub-industry"),
    "company_description": ("company description", "description"),
    "source_assignee_hint": ("user", "caller", "owner", "assigned caller", "assigned to"),
    "called_1": ("called", "called 1", "call 1"),
    "reached_1": ("reached", "reached 1"),
    "interest_status": ("i/ni", "i ni", "interest status", "interest"),
    "call_notes": ("notes", "call notes", "notes 1"),
    "email_sent": ("email sent",),
    "called_2": ("called 2", "call 2"),
    "reached_2": ("reached 2",),
}

_ALIAS_LOOKUP = {
    _norm_header(alias): canonical
    for canonical, aliases in ALIASES.items()
    for alias in aliases
}

@dataclass(frozen=True)
class LoadedTable:
    dataframe: pd.DataFrame
    source_sheet: str
    header_row: int
    file_sha256: str


@dataclass(frozen=True)
class IntakeResult:
    mapping: dict[str, str]
    normalized_rows: pd.DataFrame
    companies: pd.DataFrame
    report: dict[str, Any]
    source_sheet: str
    header_row: int
    file_sha256: str


def _clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def normalize_domain(value: Any) -> str:
    text = _clean(value).lower()
    text = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", text)
    text = text.split("/")[0].split("?")[0].split("#")[0].split(":")[0]
    text = text.removeprefix("www.").strip(".")
    if not text or " " in text or "." not in text:
        return ""
    return text

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(bv|nv|gmbh|ag|sa|sas|sarl|spa|srl|ltd|limited|llc|inc|corp|corporation|"
    r"plc|pty|pte|kg|kgaa|oy|ab|as|holding|holdings|group|company|co)\b\.?,?",
    re.IGNORECASE,
)


def normalize_company_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _clean(value)).encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ")
    text = _LEGAL_SUFFIX_RE.sub(" ", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def normalize_vat(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", _clean(value).upper())


def normalize_country(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _clean(value)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", text.lower())


_GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "icloud.com",
    "live.com", "protonmail.com", "proton.me", "mail.com", "gmx.com", "gmx.ch",
}


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_work_email(value: Any) -> str:
    """Keep only a field that is itself an email address, not free-text call notes."""
    email = _clean(value).lower()
    return email if _EMAIL_RE.fullmatch(email) else ""


def work_email_domain_hint(value: Any) -> str:
    """Return a non-generic email domain as a resolver hint, never as proof."""
    email = normalize_work_email(value)
    if not email:
        return ""
    domain = normalize_domain(email.rsplit("@", 1)[1])
    return "" if domain in _GENERIC_EMAIL_DOMAINS else domain


def _first_valid_email(row: pd.Series, mapping: dict[str, str]) -> str:
    for key in ("work_email", "additional_email_1", "additional_email_2"):
        email = normalize_work_email(_column_value(row, mapping, key))
        if email:
            return email
    return ""


def _phones(row: pd.Series, mapping: dict[str, str]) -> tuple[str, str, str]:
    """Return direct, mobile and second mobile from explicit or Lusha Phone 1/2 fields."""
    direct = _column_value(row, mapping, "direct_phone")
    mobile = _column_value(row, mapping, "mobile")
    mobile_2 = _column_value(row, mapping, "mobile_2")
    extras: list[tuple[str, str]] = []
    for n in (1, 2):
        value = _column_value(row, mapping, f"phone_{n}")
        kind = _column_value(row, mapping, f"phone_{n}_type").lower()
        if value:
            extras.append((value, kind))
    for idx, (value, kind) in enumerate(extras, start=1):
        is_mobile = "mobile" in kind or "cell" in kind
        if is_mobile:
            if not mobile:
                mobile = value
            elif not mobile_2 and value != mobile:
                mobile_2 = value
        elif kind:
            if not direct:
                direct = value
        elif idx == 1 and not direct:
            # Historical Lusha exports use Phone 1 as direct and Phone 2 as mobile.
            direct = value
        elif not mobile:
            mobile = value
        elif not mobile_2 and value != mobile:
            mobile_2 = value
    return direct, mobile, mobile_2


def _header_score(values: list[Any]) -> tuple[int, int]:
    seen: set[str] = set()
    exact = 0
    for value in values:
        canonical = _ALIAS_LOOKUP.get(_norm_header(value))
        if canonical and canonical not in seen:
            seen.add(canonical)
            exact += 1
    company_bonus = 3 if "company_name" in seen else 0
    return exact + company_bonus, exact


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _best_header_row(preview: pd.DataFrame) -> tuple[int, tuple[int, int]]:
    best_row = 0
    best_score = (-1, -1)
    for row_idx in range(min(len(preview), MAX_HEADER_SCAN_ROWS)):
        score = _header_score(preview.iloc[row_idx].tolist())
        if score > best_score:
            best_row, best_score = row_idx, score
    return best_row, best_score


def _read_csv(path: Path) -> tuple[pd.DataFrame, str, int]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            preview = pd.read_csv(
                path, sep=None, engine="python", header=None, nrows=MAX_HEADER_SCAN_ROWS,
                encoding=encoding, dtype=str,
            )
            header_row, _ = _best_header_row(preview)
            df = pd.read_csv(
                path, sep=None, engine="python", header=header_row,
                encoding=encoding, dtype=str,
            )
            return df, "CSV", header_row
        except Exception as exc:  # pragma: no cover - exercised by encoding fallbacks
            last_error = exc
    raise ValueError(f"Could not read CSV: {last_error}")


_MINIMAL_STYLES_XML = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="0"/>
  <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>
'''


def _sanitize_xlsx_styles(source: Path, target: Path) -> None:
    """Copy workbook data while neutralising a malformed Excel stylesheet.

    Some Lusha/legacy exports contain perfectly readable cells but invalid
    styles.xml. Browser XLSX readers ignore it; openpyxl rejects the whole
    workbook. This fallback changes only a temporary copy: all cell style
    references become style 0 and a minimal valid stylesheet is substituted.
    """
    cell_style = re.compile(rb'(<c\b[^>]*?)\s+s="[0-9]+"')
    with zipfile.ZipFile(source, "r") as src, zipfile.ZipFile(target, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "xl/styles.xml":
                data = _MINIMAL_STYLES_XML
            elif info.filename.startswith("xl/worksheets/") and info.filename.endswith(".xml"):
                data = cell_style.sub(rb'\1 s="0"', data)
            dst.writestr(info, data)


def _read_xlsx_core(path: Path) -> tuple[pd.DataFrame, str, int]:
    book = pd.ExcelFile(path)
    best: tuple[tuple[int, int, int], str, int] | None = None
    for sheet in book.sheet_names:
        preview = pd.read_excel(path, sheet_name=sheet, header=None, nrows=MAX_HEADER_SCAN_ROWS, dtype=str)
        header_row, score = _best_header_row(preview)
        populated = int(preview.notna().sum().sum())
        candidate = ((score[0], score[1], populated), sheet, header_row)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        raise ValueError("Workbook contains no readable sheets.")
    _, sheet, header_row = best
    return pd.read_excel(path, sheet_name=sheet, header=header_row, dtype=str), sheet, header_row


def _read_xlsx(path: Path) -> tuple[pd.DataFrame, str, int]:
    try:
        return _read_xlsx_core(path)
    except Exception as original:
        message = str(original).lower()
        if not any(term in message for term in ("stylesheet", "styles.xml", "invalid xml")):
            raise
        with tempfile.TemporaryDirectory(prefix="lead-list-xlsx-clean-") as td:
            clean = Path(td) / "style-sanitized.xlsx"
            try:
                _sanitize_xlsx_styles(path, clean)
                return _read_xlsx_core(clean)
            except Exception:
                raise original

def load_table(
    path: str | Path, *, source_sheet: str | None = None, header_row: int | None = None,
    delimiter: str | None = None,
) -> LoadedTable:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"Unsupported lead-list format {suffix!r}; expected XLSX or CSV.")
    if suffix == ".csv" and header_row is not None:
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
            try:
                df = pd.read_csv(
                    source, sep=delimiter or None, engine="python", header=int(header_row),
                    encoding=encoding, dtype=str,
                )
                sheet = "CSV"
                break
            except Exception as exc:
                last_error = exc
        else:
            raise ValueError(f"Could not read CSV: {last_error}")
    elif suffix == ".csv":
        df, sheet, header_row = _read_csv(source)
    elif source_sheet is not None or header_row is not None:
        sheet = source_sheet or pd.ExcelFile(source).sheet_names[0]
        chosen_header = int(header_row or 0)
        df = pd.read_excel(source, sheet_name=sheet, header=chosen_header, dtype=str)
        header_row = chosen_header
    else:
        df, sheet, header_row = _read_xlsx(source)
    df = df.dropna(axis=0, how="all").dropna(axis=1, how="all").reset_index(drop=True)
    df.columns = [str(c).strip() for c in df.columns]
    return LoadedTable(df, sheet, int(header_row or 0), _sha256(source))


_COLUMN_PRIORITIES: dict[str, tuple[str, ...]] = {
    "company_name": ("company name", "account name", "business name", "company", "account"),
    "domain": ("company domain", "company website", "website url", "website", "domain"),
    "country": ("company country", "hq country", "country"),
    "city": ("company city", "location city", "city", "town"),
    "work_email": ("work email", "direct email", "business email", "email address", "contact email"),
    "company_website": ("company website", "website url", "website"),
}


def detect_mapping(df: pd.DataFrame) -> dict[str, str]:
    """Resolve canonical fields with company-specific headers taking precedence.

    Lusha exports contain both contact ``Country``/``City`` and company
    ``Company Country``/``Company City``.  Company identity must use the latter.
    """
    by_normalized = {_norm_header(column): column for column in df.columns}
    mapping: dict[str, str] = {}
    for canonical, aliases in ALIASES.items():
        priorities = _COLUMN_PRIORITIES.get(canonical, aliases)
        for alias in priorities:
            column = by_normalized.get(_norm_header(alias))
            if column is not None:
                mapping[canonical] = column
                break

    # A bare "Email" column is ambiguous. If a more specific work-email
    # column exists, keep that as the address and treat a separate yes/no
    # Email column as the historical "email sent" flag.
    bare_email = by_normalized.get("email")
    if bare_email and mapping.get("work_email") != bare_email:
        values = df[bare_email].dropna().astype(str).str.strip().str.lower()
        values = values[values != ""]
        if len(values) and float(values.isin({"yes", "no", "y", "n", "true", "false"}).mean()) >= 0.8:
            mapping["email_sent"] = bare_email
    return mapping


UI_TO_CANONICAL = {
    "company": "company_name",
    "domain": "domain",
    "country": "country",
    "city": "city",
    "contact_name": "contact_name",
    "job_title": "job_title",
    "cold_caller": "source_assignee_hint",
}


def mapping_from_import_plan(df: pd.DataFrame, plan: Mapping[str, Any]) -> dict[str, str]:
    """Translate the browser-confirmed column mapping into intake canonical fields.

    The upload UI may deliberately override our automatic guess, and may map
    several source columns to Email or Phone. Preserve those choices instead
    of silently re-detecting the file on the VM.
    """
    rows = plan.get("mapping") if isinstance(plan, Mapping) else None
    if not isinstance(rows, list):
        return {}
    mapping: dict[str, str] = {}
    email_targets = iter(("work_email", "additional_email_1", "additional_email_2"))
    phone_targets = iter(("direct_phone", "mobile", "mobile_2", "phone_1", "phone_2"))
    cols = list(df.columns)
    normalized = [_norm_header(c) for c in cols]
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        field = str(item.get("field") or "").strip()
        if field in {"", "ignore", "source"}:
            continue
        column = None
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            idx = -1
        wanted = _norm_header(item.get("header"))
        if 0 <= idx < len(cols) and (not wanted or normalized[idx] == wanted):
            column = cols[idx]
        elif wanted:
            for i, norm in enumerate(normalized):
                if norm == wanted:
                    column = cols[i]
                    break
        if column is None:
            continue
        if field == "email":
            target = next(email_targets, None)
        elif field == "phone":
            target = next(phone_targets, None)
        else:
            target = UI_TO_CANONICAL.get(field)
        if target and target not in mapping:
            mapping[target] = column
    return mapping


def _column_value(row: pd.Series, mapping: dict[str, str], key: str) -> str:
    column = mapping.get(key)
    return _clean(row.get(column)) if column else ""


def _contact_name(row: pd.Series, mapping: dict[str, str]) -> str:
    direct = _column_value(row, mapping, "contact_name")
    if direct:
        return direct
    return " ".join(
        part for part in (
            _column_value(row, mapping, "first_name"),
            _column_value(row, mapping, "last_name"),
        ) if part
    ).strip()

def _company_key(company_name: str, domain: str, vat: str, country: str) -> tuple[str, str]:
    norm_domain = normalize_domain(domain)
    if norm_domain:
        return f"domain:{norm_domain}", "domain"
    norm_vat = normalize_vat(vat)
    if norm_vat:
        return f"vat:{norm_vat}", "vat"
    norm_name = normalize_company_name(company_name)
    norm_country = normalize_country(country)
    if norm_name:
        return f"name:{norm_name}|country:{norm_country}", "name_country"
    return "", "unresolved"



_PHONE_DIGITS_RE = re.compile(r"\d")
_CONFIDENCE_RE = re.compile(r"^(?:a\+?|b\+?|c\+?|high|medium|low|verified|unverified|unknown|\d{1,3}%?)$", re.I)
_YES_NO = {"yes", "no", "y", "n", "true", "false"}


def _plausible_phone(value: Any) -> bool:
    text = _clean(value)
    return len(_PHONE_DIGITS_RE.findall(text)) >= 7


def _legacy_notes_from_source_row(row: pd.Series, mapping: dict[str, str]) -> str:
    """Preserve free text found in email-shaped source columns as legacy notes.

    This is deliberately lossless for messy Lusha exports. It does not attempt
    to interpret the note as a call outcome; it merely prevents the text from
    being discarded while keeping it out of normalized email fields.
    """
    email_sent_col = mapping.get("email_sent")
    notes: list[str] = []
    for column in row.index:
        header = _norm_header(column)
        if "email" not in header:
            continue
        value = _clean(row.get(column))
        if not value:
            continue
        if email_sent_col == column and value.lower() in _YES_NO:
            continue
        if _EMAIL_RE.fullmatch(value.lower()):
            continue
        if "confidence" in header and _CONFIDENCE_RE.fullmatch(value):
            continue
        notes.append(f"{column}: {value}")
    return " | ".join(notes)


def semantic_column_issues(df: pd.DataFrame, mapping: dict[str, str]) -> list[dict[str, Any]]:
    """Find columns whose contents do not match their declared semantics."""
    issues: list[dict[str, Any]] = []
    email_sent_col = mapping.get("email_sent")
    for column in df.columns:
        header = _norm_header(column)
        if "email" not in header:
            continue
        values = df[column].dropna().astype(str).str.strip()
        values = values[values != ""]
        if values.empty:
            continue
        if email_sent_col == column and float(values.str.lower().isin(_YES_NO).mean()) >= 0.8:
            continue
        if "confidence" in header:
            good = values.map(lambda v: bool(_CONFIDENCE_RE.fullmatch(v)) or bool(_EMAIL_RE.fullmatch(v.lower())))
        else:
            good = values.map(lambda v: bool(_EMAIL_RE.fullmatch(v.lower())))
        bad = values[~good]
        if bad.empty:
            continue
        ratio = float(len(bad) / len(values))
        # A stray malformed address is repairable. Repeated free text in an
        # email-shaped field indicates a structurally contaminated source.
        severity = "review" if len(bad) >= 3 or ratio >= 0.10 else "warning"
        issues.append({
            "code": "email_field_contamination",
            "severity": severity,
            "column": str(column),
            "affected_values": int(len(bad)),
            "nonempty_values": int(len(values)),
            "affected_ratio": round(ratio, 4),
            "examples": [str(v)[:160] for v in bad.head(5).tolist()],
            "message": f"{column} contains non-email/free-text values.",
        })

    domain_col = mapping.get("domain")
    if domain_col:
        values = df[domain_col].dropna().astype(str).str.strip()
        values = values[values != ""]
        bad = values[values.map(lambda v: not bool(normalize_domain(v)))]
        if len(bad):
            issues.append({
                "code": "domain_field_contamination",
                "severity": "review" if len(bad) >= 3 else "warning",
                "column": domain_col,
                "affected_values": int(len(bad)),
                "nonempty_values": int(len(values)),
                "examples": [str(v)[:160] for v in bad.head(5).tolist()],
                "message": f"{domain_col} contains values that are not usable domains/URLs.",
            })
    return issues

def normalize_rows(
    df: pd.DataFrame,
    mapping: dict[str, str],
    *,
    default_country: str = "",
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for idx, source_row in df.iterrows():
        company_name = _column_value(source_row, mapping, "company_name")
        domain = normalize_domain(_column_value(source_row, mapping, "domain"))
        country = _column_value(source_row, mapping, "country") or default_country
        vat = normalize_vat(_column_value(source_row, mapping, "vat"))
        key, basis = _company_key(company_name, domain, vat, country)
        work_email = _first_valid_email(source_row, mapping)
        contact_name = _contact_name(source_row, mapping)
        direct_phone, mobile, mobile_2 = _phones(source_row, mapping)
        linkedin = _column_value(source_row, mapping, "contact_linkedin_url")
        has_contact_identifier = bool(
            contact_name or work_email or linkedin or
            _plausible_phone(direct_phone) or _plausible_phone(mobile) or _plausible_phone(mobile_2)
        )
        reasons: list[str] = []
        if not key:
            reasons.append("missing company identifier")
        if not country:
            reasons.append("missing country / sales market")
        if not has_contact_identifier:
            reasons.append("missing contact identifier")
        if not company_name and (domain or vat):
            reasons.append("company name requires resolution")
        blocking = {"missing company identifier", "missing country / sales market", "missing contact identifier"}
        if any(reason in blocking for reason in reasons):
            status = "blocked"
        elif reasons:
            status = "review_required"
        else:
            status = "valid"
        reason = "; ".join(reasons)
        rows.append({
            "source_row_number": int(idx) + 1,
            "company_name": company_name,
            "normalized_company_name": normalize_company_name(company_name),
            "domain": domain,
            "country": country,
            "city": _column_value(source_row, mapping, "city"),
            "vat": vat,
            "company_key": key,
            "company_identity_basis": basis,
            "contact_name": contact_name,
            "work_email": work_email,
            "email_domain_hint": work_email_domain_hint(work_email),
            "direct_phone": direct_phone,
            "mobile": mobile,
            "mobile_2": mobile_2,
            "job_title": _column_value(source_row, mapping, "job_title"),
            "contact_linkedin_url": linkedin,
            "company_website": _column_value(source_row, mapping, "company_website"),
            "company_linkedin_url": _column_value(source_row, mapping, "company_linkedin_url"),
            "employee_range": _column_value(source_row, mapping, "employee_range"),
            "industry": _column_value(source_row, mapping, "industry"),
            "company_description": _column_value(source_row, mapping, "company_description"),
            "company_revenue": _column_value(source_row, mapping, "company_revenue"),
            "sub_industry": _column_value(source_row, mapping, "sub_industry"),
            "source_assignee_hint": _column_value(source_row, mapping, "source_assignee_hint"),
            "called_1": _column_value(source_row, mapping, "called_1"),
            "reached_1": _column_value(source_row, mapping, "reached_1"),
            "interest_status": _column_value(source_row, mapping, "interest_status"),
            "call_notes": _column_value(source_row, mapping, "call_notes"),
            "email_sent": _column_value(source_row, mapping, "email_sent"),
            "called_2": _column_value(source_row, mapping, "called_2"),
            "reached_2": _column_value(source_row, mapping, "reached_2"),
            "legacy_call_notes": _legacy_notes_from_source_row(source_row, mapping),
            "has_contact_identifier": has_contact_identifier,
            "intake_status": status,
            "intake_reason": reason,
        })
    return pd.DataFrame(rows)

def _filled_count(row: pd.Series, fields: tuple[str, ...]) -> int:
    return sum(1 for field in fields if _clean(row.get(field)))


def build_company_workset(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return pd.DataFrame()
    valid = rows[(rows["intake_status"] != "blocked") & (rows["company_key"] != "")].copy()
    if valid.empty:
        return pd.DataFrame()

    company_fields = ("company_name", "domain", "country", "city", "vat")
    valid["_completeness"] = valid.apply(lambda r: _filled_count(r, company_fields), axis=1)
    valid["_order"] = range(len(valid))

    companies: list[dict[str, Any]] = []
    for company_key, group in valid.groupby("company_key", sort=False):
        winner = group.sort_values(
            ["_completeness", "_order"], ascending=[False, True], kind="stable"
        ).iloc[0]
        contacts = group["contact_name"].fillna("").astype(str).str.strip()
        emails = group["work_email"].fillna("").astype(str).str.strip()
        email_domain_hints = sorted({
            str(v).strip().lower() for v in group["email_domain_hint"].tolist() if str(v).strip()
        })
        source_rows = [int(v) for v in group["source_row_number"].tolist()]
        companies.append({
            "company_key": company_key,
            "company_identity_basis": winner["company_identity_basis"],
            "company_name": winner["company_name"],
            "domain": winner["domain"],
            "country": winner["country"],
            "city": winner["city"],
            "vat": winner["vat"],
            "contact_count": int(group["has_contact_identifier"].fillna(False).astype(bool).sum()),
            "contact_email_count": int(sum(bool(v) for v in emails)),
            "source_row_count": int(len(group)),
            "source_row_numbers": ",".join(str(v) for v in source_rows),
            "email_domain_hint": email_domain_hints[0] if len(email_domain_hints) == 1 else "",
            "email_domain_hint_conflict": len(email_domain_hints) > 1,
            "needs_domain_resolution": not bool(winner["domain"]),
            "protection_status": "pending",
            "protection_reason": "",
            "enrichment_status": "not_started",
            "company_website": winner.get("company_website", ""),
            "company_linkedin_url": winner.get("company_linkedin_url", ""),
            "employee_range": winner.get("employee_range", ""),
            "industry": winner.get("industry", ""),
            "company_description": winner.get("company_description", ""),
            "company_revenue": winner.get("company_revenue", ""),
            "sub_industry": winner.get("sub_industry", ""),
        })
    return pd.DataFrame(companies)

def _exact_contact_duplicate_count(rows: pd.DataFrame) -> int:
    if rows.empty:
        return 0
    keys: list[str] = []
    for _, row in rows.iterrows():
        company = _clean(row.get("company_key"))
        if not company:
            continue
        email = normalize_work_email(row.get("work_email"))
        linkedin = _clean(row.get("contact_linkedin_url")).lower().rstrip("/")
        phone = re.sub(r"\D", "", _clean(row.get("mobile")) or _clean(row.get("direct_phone")))
        name = normalize_company_name(row.get("contact_name"))
        identity = email or linkedin or phone or name
        if identity:
            keys.append(f"{company}|{identity}")
    return max(0, len(keys) - len(set(keys)))


def analyze_lead_list(
    path: str | Path,
    *,
    default_country: str = "",
    assigned_caller: str = "",
    list_name: str = "",
    import_plan: Mapping[str, Any] | None = None,
) -> IntakeResult:
    """Analyze a lead list without enrichment or publication.

    `assigned_caller` is an upload decision and is authoritative. Any caller/user
    column in the source is preserved only as an audit hint and never controls
    ownership.
    """
    plan = import_plan if isinstance(import_plan, Mapping) else {}
    selected_sheet = str(plan.get("sheet") or "").strip() or None
    selected_header = plan.get("header_row")
    try:
        selected_header = int(selected_header) if selected_header is not None else None
    except (TypeError, ValueError):
        selected_header = None
    selected_delimiter = str(plan.get("delimiter") or "").strip() or None
    loaded = load_table(
        path, source_sheet=selected_sheet, header_row=selected_header, delimiter=selected_delimiter,
    )
    confirmed_mapping = mapping_from_import_plan(loaded.dataframe, plan)
    mapping = confirmed_mapping or detect_mapping(loaded.dataframe)
    normalized = normalize_rows(loaded.dataframe, mapping, default_country=default_country)
    companies = build_company_workset(normalized)
    semantic_issues = semantic_column_issues(loaded.dataframe, mapping)

    row_count = int(len(normalized))
    valid_rows = int((normalized["intake_status"] == "valid").sum()) if row_count else 0
    review_rows = int((normalized["intake_status"] == "review_required").sum()) if row_count else 0
    blocked_rows = int((normalized["intake_status"] == "blocked").sum()) if row_count else 0
    unique_companies = int(len(companies))
    domain_missing = int(companies["needs_domain_resolution"].sum()) if unique_companies else 0
    email_hint_count = int((companies["email_domain_hint"].fillna("") != "").sum()) if unique_companies else 0
    email_hint_conflicts = int(companies["email_domain_hint_conflict"].sum()) if unique_companies else 0
    exact_contact_duplicates = _exact_contact_duplicate_count(normalized)
    legacy_note_rows = int((normalized["legacy_call_notes"].fillna("") != "").sum()) if row_count else 0

    hard_stops: list[str] = []
    review_reasons: list[str] = []
    auto_repairs: list[str] = []
    warnings: list[str] = []

    if row_count == 0:
        hard_stops.append("The file contains no lead rows.")
    if not _clean(assigned_caller):
        hard_stops.append("Caller must be selected during upload; source caller fields are not trusted.")
    if "company_name" not in mapping:
        hard_stops.append("No company-name column was detected.")
    if "country" not in mapping and not _clean(default_country):
        hard_stops.append("No country/market column or upload-level default market was supplied.")

    blocked_ratio = (blocked_rows / row_count) if row_count else 1.0
    if blocked_rows:
        msg = f"{blocked_rows} row(s) fail a minimum identity/market requirement."
        if blocked_rows >= 10 or blocked_ratio > 0.05:
            hard_stops.append(msg)
        else:
            review_reasons.append(msg + " These rows can be quarantined or corrected.")
    if review_rows:
        auto_repairs.append(f"{review_rows} row(s) need company-name or identity repair before publication.")

    review_semantic = [i for i in semantic_issues if i["severity"] == "review"]
    warning_semantic = [i for i in semantic_issues if i["severity"] == "warning"]
    if review_semantic:
        columns = ", ".join(str(i["column"]) for i in review_semantic)
        review_reasons.append(f"Semantic contamination detected in source column(s): {columns}.")
    if warning_semantic:
        warnings.append(f"Minor malformed values found in {len(warning_semantic)} source field(s).")

    if "domain" not in mapping:
        auto_repairs.append("No domain column detected; domains will be inferred from work email or resolved during enrichment.")
    elif domain_missing:
        auto_repairs.append(f"{domain_missing} company/companies still need domain resolution.")
    if exact_contact_duplicates:
        auto_repairs.append(f"{exact_contact_duplicates} exact duplicate contact row(s) can be deduplicated automatically.")

    source_assignees = sorted({
        _clean(v) for v in normalized.get("source_assignee_hint", pd.Series(dtype=str)).tolist() if _clean(v)
    })
    caller = _clean(assigned_caller)
    mismatched_source_assignees = [v for v in source_assignees if caller and v.casefold() != caller.casefold()]
    if mismatched_source_assignees:
        warnings.append(
            "Source assignee field differs from upload assignment and will be ignored: "
            + ", ".join(mismatched_source_assignees[:5])
        )

    if hard_stops:
        quality_status = "RED"
        decision = "REJECTED"
    elif review_reasons:
        quality_status = "AMBER"
        decision = "REVIEW_REQUIRED"
    elif review_rows or warning_semantic or exact_contact_duplicates:
        quality_status = "AMBER"
        decision = "AUTO_REPAIR"
    else:
        quality_status = "GREEN"
        decision = "READY"

    report = {
        "quality_status": quality_status,
        "decision": decision,
        "list_name": _clean(list_name) or Path(path).stem,
        "assigned_caller": caller,
        "source_rows": row_count,
        "valid_rows": valid_rows,
        "review_rows": review_rows,
        "blocked_rows": blocked_rows,
        "unique_companies": unique_companies,
        "company_rows_collapsed": max(0, (valid_rows + review_rows) - unique_companies),
        "exact_duplicate_contacts": exact_contact_duplicates,
        "companies_needing_domain_resolution": domain_missing,
        "companies_with_email_domain_hint": email_hint_count,
        "email_domain_hint_conflicts": email_hint_conflicts,
        "contact_rows": int(normalized["has_contact_identifier"].fillna(False).astype(bool).sum()) if row_count else 0,
        "legacy_note_rows_preserved": legacy_note_rows,
        "detected_columns": sorted(mapping.keys()),
        "mapping_source": "confirmed_by_user" if confirmed_mapping else "auto_detected",
        "semantic_issues": semantic_issues,
        "hard_stops": hard_stops,
        "review_reasons": review_reasons,
        "auto_repairs": auto_repairs,
        "warnings": warnings,
        "source_assignee_values": source_assignees,
        "source_assignee_ignored": bool(source_assignees),
        "ready_for_protection_checks": quality_status == "GREEN" and bool(unique_companies),
        "ready_for_enrichment": quality_status == "GREEN" and bool(unique_companies),
        "publish_eligible": quality_status == "GREEN" and bool(unique_companies),
    }
    return IntakeResult(
        mapping=mapping,
        normalized_rows=normalized,
        companies=companies,
        report=report,
        source_sheet=loaded.source_sheet,
        header_row=loaded.header_row,
        file_sha256=loaded.file_sha256,
    )
