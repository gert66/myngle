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


def _pct(value) -> str:
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return _esc(value)


def _month_profiles_table(cohort_analysis: dict) -> str:
    blocks = []
    for object_type in ("companies", "contacts"):
        analysis = cohort_analysis.get(object_type)
        if not analysis:
            continue
        rows = []
        for month_key, profile in analysis.get("month_profiles", {}).items():
            share = _pct(profile["share_of_population"])
            presence_rate = _pct(profile["presence_rate"])
            untouched_rate = _pct(profile["untouched_since_creation_rate"])
            rows.append(
                "<tr>"
                f"<td>{_esc(month_key)}</td>"
                f"<td>{_esc(profile['count'])}</td>"
                f"<td>{_esc(share)}</td>"
                f"<td>{_esc(profile['presence_field'])}</td>"
                f"<td>{_esc(presence_rate)}</td>"
                f"<td>{_esc(untouched_rate)}</td>"
                "</tr>"
            )
        bursts = analysis.get("timestamp_bursts", {})
        sep_focus = analysis.get("sep_2023_focus", {})
        sep_share = _pct(sep_focus.get("share_of_population", 0))
        blocks.append(
            f"<h3>{_esc(object_type)}</h3>"
            f"<p>Unique records: {_esc(analysis.get('unique_id_count'))} &middot; "
            f"unknown createdate: {_esc(analysis.get('unknown_createdate_count'))} &middot; "
            f"reference time: {_esc(analysis.get('reference_time'))}</p>"
            "<table border='1' cellpadding='4' cellspacing='0'>"
            "<thead><tr><th>month</th><th>count</th><th>share</th>"
            "<th>presence field</th><th>presence rate</th><th>untouched rate</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
            f"<p><strong>Timestamp bursts</strong> (&ge;{_esc(bursts.get('threshold'))} records at the same "
            f"createdate minute): {_esc(len(bursts.get('minutes', [])))} minute(s) flagged, "
            f"{_esc(bursts.get('total_records_in_bursts'))} record(s) total.</p>"
            f"<p><strong>Sep 2023 focus:</strong> {_esc(sep_focus.get('count'))} record(s), "
            f"{_esc(sep_share)} of the population, "
            f"detected as a wave: {'yes' if sep_focus.get('detected_as_wave') else 'no'}.</p>"
        )
    return "".join(blocks) or "<p>No cohort analysis available.</p>"


def _waves_block(cohort_analysis: dict) -> str:
    blocks = []
    for object_type in ("companies", "contacts"):
        analysis = cohort_analysis.get(object_type)
        if not analysis:
            continue
        waves = analysis.get("waves", [])
        if not waves:
            blocks.append(f"<h3>{_esc(object_type)}</h3><p>No wave candidates flagged at the parameters below.</p>")
            continue
        rows = []
        for wave in waves:
            share = _pct(wave["share_of_population"])
            baseline_used = f"{wave['baseline_used']:.1f}"
            explanations = "; ".join(_esc(x) for x in wave["alternative_explanations"])
            rows.append(
                "<tr>"
                f"<td>{_esc(wave['wave_id'])}</td>"
                f"<td>{_esc(wave['start'])}</td>"
                f"<td>{_esc(wave['end'])}</td>"
                f"<td>{_esc(wave['total_records'])}</td>"
                f"<td>{_esc(share)}</td>"
                f"<td>{_esc(baseline_used)}</td>"
                f"<td>{_esc(wave['peak_day'])} ({_esc(wave['peak_count'])})</td>"
                f"<td>{explanations}</td>"
                "</tr>"
            )
        blocks.append(
            f"<h3>{_esc(object_type)}</h3>"
            "<table border='1' cellpadding='4' cellspacing='0'>"
            "<thead><tr><th>wave id</th><th>start</th><th>end</th><th>total records</th>"
            "<th>share</th><th>baseline used</th><th>peak day (count)</th>"
            "<th>alternative explanations to test</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    parameters = cohort_analysis.get("parameters", {})
    params_line = ", ".join(f"{_esc(k)}={_esc(v)}" for k, v in parameters.items())
    return f"<p>Heuristic parameters: {params_line}</p>" + "".join(blocks)


def generate_html_report(context: dict, output_path: str) -> None:
    reconciliation = context.get("reconciliation", {})
    cohort_analysis = context.get("cohort_analysis", {}) or {}
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
<h2><span class="badge observed">Observed facts</span> Creation cohorts</h2>
<p>Recomputed independently from the raw snapshot: creation counts bucketed
by day/ISO-week/month, per-month presence/recency profiles, and exact
createdate-minute timestamp bursts. Records with a missing or unparsable
createdate are counted under the explicit <code>unknown</code> cohort,
never dropped.</p>
{_month_profiles_table(cohort_analysis)}
</section>

<section>
<h2><span class="badge inferred">Inferred classifications</span></h2>
<p>Population bucketing (operational_customer, operational_prospect,
active_other, historical_import, enrichment_or_bulk,
legacy_or_obsolete_candidate, uncertain) is not yet implemented; see
<code>population_map.json</code> for the current placeholder status.</p>
<h3>Bulk-import wave candidates (heuristic)</h3>
<p>Days whose creation count clears <code>max(wave_abs_min, wave_factor
&times; baseline)</code>, where the baseline is the median of non-zero
daily counts over the preceding window, are flagged and merged into waves.
This is a heuristic signal, not a conclusion -- each wave lists alternative
explanations that a later batch must test before any classification is
assigned.</p>
{_waves_block(cohort_analysis)}
</section>

<section>
<h2><span class="badge uncertain">Uncertainty</span></h2>
<p>Every object type without an independently recorded portal total is
reported as <strong>unreconciled</strong> rather than assumed correct (see
the notes above). Bulk-import wave candidates above are explicitly labelled
inferred/heuristic, not conclusions. No classification confidence claims
are made until the classification phase is implemented.</p>
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
