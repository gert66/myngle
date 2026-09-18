"""Minimal HTML report generator.

The report is deliberately structured into four clearly separated
sections -- observed facts, inferred classifications, uncertainty, and
inaccessible data -- per the audit's evidence-hierarchy contract. Analyses
not yet implemented in this batch are listed under "inaccessible data /
not yet implemented" rather than silently omitted.
"""
from __future__ import annotations

import html


def _esc(value) -> str:
    return html.escape(str(value))


def _reconciliation_table(reconciliation: dict) -> str:
    rows = []
    for object_type, result in reconciliation.items():
        rows.append(
            "<tr>"
            f"<td>{_esc(object_type)}</td>"
            f"<td>{_esc(result['recorded_portal_total'])}</td>"
            f"<td>{_esc(result['raw_record_count'])}</td>"
            f"<td>{_esc(result['unique_id_count'])}</td>"
            f"<td>{_esc(result['duplicate_count'])}</td>"
            f"<td>{_esc(result['delta'])}</td>"
            f"<td>{'yes' if result['reconciled'] else 'no'}</td>"
            "</tr>"
        )
    return (
        "<table border='1' cellpadding='4' cellspacing='0'>"
        "<thead><tr><th>object type</th><th>recorded portal total</th>"
        "<th>raw record count</th><th>unique id count</th>"
        "<th>duplicate count</th><th>delta</th><th>reconciled</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _notes_list(reconciliation: dict) -> str:
    items = []
    for object_type, result in reconciliation.items():
        for note in result.get("notes", []):
            items.append(f"<li><strong>{_esc(object_type)}:</strong> {_esc(note)}</li>")
    if not items:
        return "<p>No reconciliation notes.</p>"
    return f"<ul>{''.join(items)}</ul>"


def generate_html_report(context: dict, output_path: str) -> None:
    reconciliation = context.get("reconciliation", {})
    not_yet_implemented = context.get("not_yet_implemented", [])
    gaps = context.get("gaps", [])

    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>HubSpot Population Audit</title>
<style>
body {{ font-family: sans-serif; margin: 2rem; color: #1a1a1a; }}
section {{ margin-bottom: 2rem; }}
h1 {{ font-size: 1.4rem; }}
h2 {{ font-size: 1.1rem; border-bottom: 1px solid #ccc; padding-bottom: 0.25rem; }}
table {{ border-collapse: collapse; }}
.badge {{ display: inline-block; padding: 0.1rem 0.5rem; border-radius: 0.3rem; font-size: 0.8rem; }}
.observed {{ background: #d7f0d7; }}
.inferred {{ background: #fff3cd; }}
.uncertain {{ background: #f8d7da; }}
.inaccessible {{ background: #e2e3e5; }}
</style>
</head>
<body>
<h1>HubSpot Population Audit</h1>
<p>Run: {_esc(context.get('run_id'))} &middot; generated {_esc(context.get('generated_at'))} &middot; snapshot {_esc(context.get('snapshot'))}</p>

<section>
<h2><span class="badge observed">Observed facts</span> Portal / unique-ID reconciliation</h2>
<p>Recomputed independently from the raw snapshot for each object type: raw record
count, unique ID count (duplicates removed), and -- where an independently
recorded portal total was found -- the delta against that total.</p>
{_reconciliation_table(reconciliation)}
{_notes_list(reconciliation)}
</section>

<section>
<h2><span class="badge inferred">Inferred classifications</span></h2>
<p>Not yet implemented in this build. Population bucketing
(operational_customer, operational_prospect, active_other, historical_import,
enrichment_or_bulk, legacy_or_obsolete_candidate, uncertain) will be added in
a follow-up batch; see <code>population_map.json</code> and
<code>cohort_analysis.json</code> for the current placeholder status.</p>
</section>

<section>
<h2><span class="badge uncertain">Uncertainty</span></h2>
<p>Every object type without an independently recorded portal total is
reported as <strong>unreconciled</strong> rather than assumed correct (see
the notes above). No classification confidence claims are made until the
classification phase is implemented.</p>
</section>

<section>
<h2><span class="badge inaccessible">Inaccessible data / not yet implemented</span></h2>
<ul>
{''.join(f'<li>{_esc(item)}</li>' for item in not_yet_implemented)}
</ul>
<h3>Known gaps</h3>
<ul>
{''.join(f'<li>{_esc(gap)}</li>' for gap in gaps)}
</ul>
</section>

</body>
</html>
"""
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(body)
