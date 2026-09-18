"""Static HTML consultancy report generator.

No JavaScript charting library and no network fetch: every visual is plain
HTML/CSS (stat tiles, CSS bar charts) using the validated light-mode palette
documented in the project's dataviz skill (categorical hues assigned in
fixed order, status colors always paired with an icon + label, one sequential
hue for magnitude, a stat tile/progress bar instead of a gauge for the single
AI-budget value).
"""
from __future__ import annotations

import html
import os

# --- validated palette (see dataviz skill references/palette.md) ---
SURFACE = "#fcfcfb"
PAGE_PLANE = "#f9f9f7"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"
BORDER = "rgba(11,11,11,0.10)"

CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SEQUENTIAL_BLUE = "#2a78d6"
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
STATUS_ICON = {"good": "✓", "warning": "⚠", "serious": "⚠", "critical": "✗"}


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def stat_tile(label: str, value, sublabel: str = "", status: str | None = None) -> str:
    badge = ""
    if status:
        color = STATUS.get(status, INK_MUTED)
        icon = STATUS_ICON.get(status, "")
        badge = (
            f'<div style="color:{color};font-weight:600;font-size:13px;margin-top:4px;">'
            f'{icon} {_esc(status.upper())}</div>'
        )
    return f"""
    <div style="background:{SURFACE};border:1px solid {BORDER};border-radius:10px;padding:16px 18px;min-width:180px;">
      <div style="color:{INK_MUTED};font-size:12px;text-transform:uppercase;letter-spacing:.04em;">{_esc(label)}</div>
      <div style="color:{INK_PRIMARY};font-size:28px;font-weight:700;margin-top:4px;">{_esc(value)}</div>
      <div style="color:{INK_SECONDARY};font-size:13px;margin-top:2px;">{_esc(sublabel)}</div>
      {badge}
    </div>
    """


def stat_row(tiles: list) -> str:
    return f'<div style="display:flex;flex-wrap:wrap;gap:14px;margin:16px 0 24px;">{"".join(tiles)}</div>'


def bar_row(label: str, value: float, max_value: float, color: str, value_label: str) -> str:
    pct = 0 if max_value <= 0 else max(2, round(100 * value / max_value))
    return f"""
    <div style="display:flex;align-items:center;gap:10px;margin:6px 0;">
      <div style="width:220px;color:{INK_SECONDARY};font-size:13px;flex-shrink:0;">{_esc(label)}</div>
      <div style="flex:1;background:{GRIDLINE};border-radius:4px;height:14px;overflow:hidden;">
        <div style="width:{pct}%;background:{color};height:100%;border-radius:4px;"></div>
      </div>
      <div style="width:90px;text-align:right;color:{INK_PRIMARY};font-size:13px;font-variant-numeric:tabular-nums;">{_esc(value_label)}</div>
    </div>
    """


def bar_chart(title: str, rows: list, color: str | None = None) -> str:
    """rows: list[(label, value)]. Single-hue sequential bars for one series."""
    if not rows:
        return f"<h4>{_esc(title)}</h4><p style='color:{INK_MUTED};'>Geen data.</p>"
    max_value = max(v for _, v in rows) or 1
    bars = "".join(
        bar_row(label, value, max_value, color or SEQUENTIAL_BLUE, f"{value:,}") for label, value in rows
    )
    return f'<h4 style="margin-bottom:8px;">{_esc(title)}</h4><div>{bars}</div>'


def categorical_bar_chart(title: str, rows: list) -> str:
    """rows: list[(label, value)]. Each row gets the next fixed categorical slot."""
    if not rows:
        return f"<h4>{_esc(title)}</h4><p style='color:{INK_MUTED};'>Geen data.</p>"
    max_value = max(v for _, v in rows) or 1
    bars = "".join(
        bar_row(label, value, max_value, CATEGORICAL[i % len(CATEGORICAL)], f"{value:,}")
        for i, (label, value) in enumerate(rows)
    )
    return f'<h4 style="margin-bottom:8px;">{_esc(title)}</h4><div>{bars}</div>'


def progress_meter(label: str, value: float | None, budget: float, pct: float | None) -> str:
    if value is None:
        value_text = "onbekend (geen prijs geconfigureerd)"
        pct_display = 0
    else:
        value_text = f"€{value:.2f} van €{budget:.2f}"
        pct_display = min(100, pct or 0)
    return f"""
    <div style="background:{SURFACE};border:1px solid {BORDER};border-radius:10px;padding:16px 18px;max-width:420px;">
      <div style="color:{INK_MUTED};font-size:12px;text-transform:uppercase;letter-spacing:.04em;">{_esc(label)}</div>
      <div style="color:{INK_PRIMARY};font-size:20px;font-weight:700;margin:6px 0;">{value_text}</div>
      <div style="background:{GRIDLINE};border-radius:5px;height:10px;overflow:hidden;">
        <div style="width:{pct_display}%;background:{SEQUENTIAL_BLUE};height:100%;"></div>
      </div>
    </div>
    """


def table(headers: list, rows: list) -> str:
    if not rows:
        return f"<p style='color:{INK_MUTED};'>Geen rijen.</p>"
    thead = "".join(f"<th style='text-align:left;padding:6px 10px;color:{INK_MUTED};font-size:12px;border-bottom:1px solid {GRIDLINE};'>{_esc(h)}</th>" for h in headers)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td style='padding:6px 10px;font-size:13px;color:{INK_PRIMARY};border-bottom:1px solid {GRIDLINE};'>{_esc(c)}</td>" for c in row)
        body_rows.append(f"<tr>{cells}</tr>")
    return f"<table style='border-collapse:collapse;width:100%;margin:10px 0;'><thead><tr>{thead}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def section(anchor: str, title: str, body: str) -> str:
    return f"""
    <section id="{_esc(anchor)}" style="margin:32px 0;padding-top:8px;">
      <h2 style="border-bottom:2px solid {GRIDLINE};padding-bottom:8px;color:{INK_PRIMARY};">{_esc(title)}</h2>
      {body}
    </section>
    """


def _findings_table(findings: list, statuses: tuple) -> str:
    rows = [
        (f["finding_id"], f["title"], f["category"], f["status"], f"{f['confidence']:.2f}")
        for f in findings
        if f["status"] in statuses
    ]
    return table(["ID", "Titel", "Categorie", "Status", "Vertrouwen"], rows)


def generate_html_report(context: dict, output_path: str) -> None:
    """``context`` is the fully assembled report context built by runner.py:
    metrics per analysis, capability matrix, findings, investigations, gaps,
    cost summary and control-center-style plain language summaries.
    """
    m = context["metrics"]
    cap = context["capability_matrix"]
    findings = context["findings"]
    investigations = context["investigations"]
    cost = context["cost_summary"]
    gaps = context["gaps"]

    kpi_tiles = [
        stat_tile("Companies", f"{m['companies']['total_companies']:,}", "totaal geëxtraheerd"),
        stat_tile("Contacts", f"{m['contacts']['total_contacts']:,}", "totaal geëxtraheerd"),
        stat_tile("Deals", f"{m['deals']['total_deals']:,}", "totaal geëxtraheerd"),
        stat_tile(
            "Companies zonder domein",
            f"{m['companies']['missing_domain']:,}",
            f"{_pct(m['companies']['missing_domain'], m['companies']['total_companies'])}% van companies",
            status=_health_status(_pct(m['companies']['missing_domain'], m['companies']['total_companies']), 40, 65),
        ),
        stat_tile(
            "Contacts zonder email",
            f"{m['contacts']['missing_email']:,}",
            f"{_pct(m['contacts']['missing_email'], m['contacts']['total_contacts'])}% van contacts",
            status=_health_status(_pct(m['contacts']['missing_email'], m['contacts']['total_contacts']), 40, 65),
        ),
        stat_tile(
            "Deals zonder company",
            f"{m['deals']['missing_company_association']:,}",
            f"{_pct(m['deals']['missing_company_association'], m['deals']['total_deals'])}% van deals",
            status=_health_status(_pct(m['deals']['missing_company_association'], m['deals']['total_deals']), 5, 20),
        ),
    ]

    coverage_rows = [
        (rec["object_name"], rec["status"], rec["detail"], rec["downstream_impact"]) for rec in cap.values()
    ]

    company_body = f"""
    {bar_chart("Top duplicate-domeingroepen (aantal companies)",
                [(s['key'], s['count']) for s in context['company_signals']['duplicate_domains'][:10]])}
    <p style="color:{INK_SECONDARY};">Stale companies (&gt; drempel niet gewijzigd): <b>{m['companies']['stale_companies']:,}</b>.
    Creation-date bursts gedetecteerd: <b>{m['companies']['creation_burst_days']}</b> dagen.</p>
    """

    contact_body = f"""
    {bar_chart("Top duplicate-emailgroepen (aantal contacts)",
                [(s['key'], s['count']) for s in context['contact_signals']['duplicate_emails'][:10]])}
    <p style="color:{INK_SECONDARY};">Contacts zonder company-koppeling: <b>{m['contacts']['missing_company_association']:,}</b>.
    Contacts gekoppeld aan meerdere companies: <b>{m['contacts']['multi_company_contacts']:,}</b>.
    Stale contacts: <b>{m['contacts']['stale_contacts']:,}</b>.</p>
    """

    deal_body = f"""
    {stat_row([
        stat_tile("Open deals", f"{m['deals']['open_deals']:,}"),
        stat_tile("Stale open deals", f"{m['deals']['stale_open_deals']:,}", status=_health_status(_pct(m['deals']['stale_open_deals'], max(m['deals']['open_deals'],1)), 20, 50)),
        stat_tile("Open deals zonder recente activiteit", f"{m['cross_object']['open_deals_without_recent_activity']:,}"),
        stat_tile("Pipelines in gebruik", f"{m['deals']['pipelines_in_use']:,}"),
    ])}
    {categorical_bar_chart("Stuck pipeline-stages (open deals zonder recente actie)",
                [(r['pipeline_stage'], r['stuck_count']) for r in context['pipeline_signals']['stuck_pipeline_stages'][:10]])}
    """

    activity_body = f"""
    {categorical_bar_chart("Activiteitsvolume per type (totaal)", list(m['activities']['volume_by_kind'].items()))}
    {categorical_bar_chart("Activiteitsvolume per type (laatste 30 dagen)", list(m['activities']['last_30_days_by_kind'].items()))}
    <p style="color:{INK_SECONDARY};">Onbeheerde/overdue tasks: <b>{m['activities']['overdue_tasks']:,}</b>.
    Activiteiten zonder eigenaar per type: {", ".join(f"{k}: {v}" for k, v in m['activities']['unassigned_by_kind'].items())}.</p>
    """

    association_body = table(
        ["Check", "Aantal"],
        [
            ("Companies zonder domein", m['companies']['missing_domain']),
            ("Contacts zonder company-associatie", m['contacts']['missing_company_association']),
            ("Deals zonder company-associatie", m['deals']['missing_company_association']),
            ("Deals zonder contact-associatie", m['deals']['missing_contact_association']),
            ("Deals zonder owner", m['deals']['missing_owner']),
            ("Companies met wees-parent-referentie", m['companies']['orphan_parent_references']),
        ],
    )

    property_sections = []
    for obj_type, pm in m["properties"].items():
        property_sections.append(
            f"<h4>{_esc(obj_type)}</h4>" + table(
                ["Metric", "Aantal"],
                [
                    ("Totaal properties", pm["total_properties"]),
                    ("Ongebruikt (0% fill)", pm["unused_properties"]),
                    ("Bijna ongebruikt", pm["near_unused_properties"]),
                    ("Legacy Salesforce-achtige velden", pm["legacy_salesforce_properties"]),
                    ("Migratie/Apollo-kandidaten", pm["migration_candidate_properties"]),
                ],
            )
        )
    property_body = "".join(property_sections)

    historical_sections = []
    for obj_type, hm in m["historical_imports"].items():
        historical_sections.append(
            f"<h4>{_esc(obj_type)}</h4>" + table(
                ["Metric", "Aantal"],
                [
                    ("Creation-date bursts", hm["creation_burst_days"]),
                    ("Migratie-cohort kandidaten (ongeverifieerd)", hm["migration_cohort_candidates"]),
                    ("Distincte source-waarden", hm["distinct_source_values"]),
                ],
            )
        )
    historical_body = "".join(historical_sections) + f"""
    <p style="color:{INK_SECONDARY};font-size:13px;">Migratie-cohorten zijn kandidaten op basis van creation-date bursts
    gecombineerd met een dominante source-waarde. Dit is geen bevestigde vaststelling; zie de investigation queue
    en evidence-appendix voor de onderliggende status.</p>
    """

    ownership_body = ""
    for obj_type, rows in m["ownership"]["ownership_by_object"].items():
        ownership_body += f"<h4>{_esc(obj_type)}</h4>" + categorical_bar_chart(
            f"Top owners ({obj_type})", [(r["owner_name"] or r["owner_id"], r["count"]) for r in rows[:8]]
        )

    cross_object_body = table(
        ["Finding", "Categorie", "Status", "Vertrouwen"],
        [(f["title"], f["category"], f["status"], f"{f['confidence']:.2f}") for f in findings if f["category"] == "cross_object"],
    )

    gaps_body = table(
        ["Object", "Status", "Detail", "Impact op analyse"],
        [(g["object_name"], g["status"], g["detail"], g["downstream_impact"]) for g in gaps],
    )

    investigations_body = table(
        ["ID", "Vraag", "Status", "Verwachte informatiewaarde", "Diepte"],
        [
            (inv["investigation_id"], inv["question"], inv["status"], f"{inv['expected_information_value']:.2f}", inv["depth"])
            for inv in investigations
        ],
    )

    findings_appendix = "".join(
        f"""
        <details style="margin:8px 0;border:1px solid {BORDER};border-radius:8px;padding:10px 14px;">
          <summary style="cursor:pointer;color:{INK_PRIMARY};font-weight:600;">{_esc(f['finding_id'])} — {_esc(f['title'])} ({_esc(f['status'])})</summary>
          <p style="color:{INK_SECONDARY};font-size:13px;"><b>Hypothese:</b> {_esc(f.get('hypothesis') or '-')}</p>
          <p style="color:{INK_SECONDARY};font-size:13px;"><b>Analyse-versie:</b> {_esc(f['analysis_version'])} · <b>Vertrouwen:</b> {f['confidence']:.2f}</p>
          <p style="color:{INK_SECONDARY};font-size:13px;"><b>Evidence:</b> {len(f['evidence'])} item(s) · <b>Counter-evidence:</b> {len(f['counter_evidence'])} item(s)</p>
        </details>
        """
        for f in findings
    )

    html_doc = f"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<title>mYngle HubSpot CRM Audit Report</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; background:{PAGE_PLANE}; color:{INK_PRIMARY}; margin:0; }}
  .wrap {{ max-width: 980px; margin: 0 auto; padding: 24px; }}
  a {{ color:{SEQUENTIAL_BLUE}; }}
  nav a {{ display:inline-block; margin-right:14px; font-size:13px; color:{INK_SECONDARY}; text-decoration:none; }}
  h1 {{ font-size: 26px; }}
  h4 {{ margin-bottom: 4px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>mYngle HubSpot CRM Audit</h1>
  <p style="color:{INK_SECONDARY};">Run <code>{_esc(context['run_id'])}</code> · gegenereerd {_esc(context['generated_at'])} · modus: {_esc(context['mode'])}</p>
  <nav>
    <a href="#overview">Overzicht</a><a href="#coverage">Dekking</a><a href="#kpis">KPI's</a>
    <a href="#companies">Companies</a><a href="#contacts">Contacts</a><a href="#deals">Deals/Pipeline</a>
    <a href="#activities">Activiteit</a><a href="#associations">Associaties</a><a href="#properties">Properties</a>
    <a href="#historical">Historische imports</a><a href="#ownership">Ownership</a><a href="#crossobject">Cross-object</a>
    <a href="#gaps">Data gaps</a><a href="#principles">CRM 2.0</a><a href="#roadmap">Roadmap</a><a href="#appendix">Appendix</a>
  </nav>

  {section("overview", "Management samenvatting", f"""
    <p>Dit rapport is gegenereerd door een read-only audit-framework dat de HubSpot CRM-data van mYngle heeft
    doorgelicht als een externe CRM-consultant: data ophalen, feiten meten, afwijkingen detecteren, en waar nuttig
    lichte AI-interpretatie toepassen. Er zijn geen wijzigingen aangebracht in HubSpot.</p>
    <p><b>{len(findings)}</b> bevindingen gedetecteerd, waarvan <b>{len([f for f in findings if f['status']=='confirmed'])}</b>
    bevestigd, <b>{len([f for f in findings if f['status']=='needs_human_context'])}</b> met menselijke context nodig, en
    <b>{len(investigations)}</b> vervolgvragen in de investigation queue.</p>
  """)}

  {section("coverage", "Dekking en beperkingen", table(["Object", "Status", "Detail", "Impact"], coverage_rows))}

  {section("kpis", "Kerncijfers (KPI's)", stat_row(kpi_tiles) + progress_meter("AI-uitgaven", cost['ai_cost_estimate_eur'], cost['ai_budget_eur'] or 0.0, cost['pct_budget_consumed']))}

  {section("companies", "Company health", company_body)}
  {section("contacts", "Contact health", contact_body)}
  {section("deals", "Deal- en pipeline-health", deal_body)}
  {section("activities", "Activiteit-health", activity_body)}
  {section("associations", "Associatie-integriteit", association_body)}
  {section("properties", "Property- en schema-health", property_body)}
  {section("historical", "Historische imports", historical_body)}
  {section("ownership", "Ownership en gebruik", ownership_body)}
  {section("crossobject", "Cross-object bevindingen", cross_object_body)}
  {section("gaps", "Data gaps en MCP-kandidaten", gaps_body + f"<p style='color:{INK_SECONDARY};font-size:13px;'>Deze gaps zijn kandidaten om later via de HubSpot MCP-toegang aan te vullen.</p>")}

  {section("principles", "CRM 2.0-principes", """
    <ul>
      <li>Eén brondefinitie per gegeven (geen dubbele/legacy velden met overlappende betekenis).</li>
      <li>Elke deal, contact en company heeft een duidelijke owner en minimaal één actieve associatie.</li>
      <li>Elk open deal heeft een aantoonbare recente actie of een expliciete reden waarom niet.</li>
      <li>Property-inventaris wordt periodiek opgeschoond; ongebruikte/legacy velden worden gearchiveerd, niet aangevuld.</li>
      <li>Migratiesporen (Salesforce/Apollo) worden geëxpliciteerd in plaats van stilzwijgend meegesleept.</li>
    </ul>
  """)}

  {section("roadmap", "Remediation roadmap", f"""
    <ol>
      <li>Valideer de bevestigde bevindingen ({len([f for f in findings if f['status']=='confirmed'])}) met de business owner.</li>
      <li>Los de {m['deals']['missing_company_association'] + m['deals']['missing_contact_association']} deals met ontbrekende
      company/contact-associaties op — hoogste directe impact op pipeline-rapportage.</li>
      <li>Rond de {len(investigations)} openstaande investigations af, te beginnen bij de hoogste verwachte informatiewaarde.</li>
      <li>Plan opschoning van ongebruikte/legacy properties per object type.</li>
      <li>Vul geïdentificeerde data-gaps aan zodra HubSpot MCP-toegang tot de ontbrekende objecten beschikbaar is.</li>
    </ol>
  """)}

  {section("appendix", "Methoden en evidence-appendix", f"<p style='color:{INK_SECONDARY};font-size:13px;'>Analyse-versie: zie per bevinding. Elke bevinding is herleidbaar naar bron, tellingen en (indien van toepassing) een beperkte steekproef van record-ID's — nooit volledige recordinhoud.</p>{findings_appendix}")}

</div>
</body>
</html>
"""

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(html_doc)


def _pct(part: int, total: int) -> float:
    if not total:
        return 0.0
    return round(100 * part / total, 1)


def _health_status(pct_value: float, warn_at: float, critical_at: float) -> str:
    if pct_value >= critical_at:
        return "critical"
    if pct_value >= warn_at:
        return "warning"
    return "good"
