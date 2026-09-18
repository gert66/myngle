"""Audit controller: runs every phase in order, writes every required output
artifact, and keeps the Control Center status contract up to date after each
phase so the UI stays useful even while the backend is busy.
"""
from __future__ import annotations

import json
import os
import time

from .ai_interpreter import build_interpreter
from .analyses.activities import analyze_activities
from .analyses.companies import analyze_companies
from .analyses.contacts import analyze_contacts
from .analyses.cross_object import open_deals_without_recent_activity, ownership_usage
from .analyses.deals import analyze_deals
from .analyses.historical_imports import analyze_historical_imports
from .analyses.pipelines import analyze_pipelines
from .analyses.properties import analyze_properties
from .capability_probe import capability_matrix_to_dict, run_capability_probe
from .control_center import ControlCenterState, build_control_center_payload, write_control_center
from .cost_tracker import CostTracker
from .evidence_ledger import EvidenceLedger, make_evidence
from .extraction import extract_all, read_jsonl
from .hubspot_client import build_client
from .investigation_queue import InvestigationQueue
from .models import CapabilityStatus, FindingStatus, now_iso
from .report.html_report import generate_html_report

OUTPUT_SUBDIRS = ["raw", "normalized", "analyses", "investigations", "evidence", "reports", "logs"]


def _mkdirs(output_dir: str) -> dict:
    paths = {"root": output_dir}
    for name in OUTPUT_SUBDIRS:
        path = os.path.join(output_dir, name)
        os.makedirs(path, exist_ok=True)
        paths[name] = path
    return paths


def _log(logs_dir: str, message: str) -> None:
    with open(os.path.join(logs_dir, "run.log"), "a", encoding="utf-8") as fh:
        fh.write(f"{now_iso()} {message}\n")


def _write_json(path: str, payload) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)


def run_audit(config) -> dict:
    paths = _mkdirs(config.output_dir)
    state = ControlCenterState(run_id=config.run_id)
    ledger = EvidenceLedger()
    queue = InvestigationQueue(
        max_depth=config.ai.max_investigation_depth,
        low_value_threshold=config.low_information_value_threshold,
        max_size=config.max_investigation_queue_size,
    )
    interpreter = build_interpreter(config.ai)
    control_center_path = os.path.join(paths["root"], "control_center.json")

    def heartbeat() -> None:
        payload = build_control_center_payload(state, ledger, queue, interpreter.budget_summary())
        write_control_center(payload, control_center_path)

    run_status = {"run_id": config.run_id, "mode": config.mode, "started_at": now_iso(), "phases": {}}
    _log(paths["logs"], f"Run {config.run_id} started in mode={config.mode}")
    heartbeat()

    # --- phase: capability probe ---
    state.start_phase("capability_probe")
    client = build_client(config)
    capability_matrix = run_capability_probe(client)
    gaps = [
        rec.to_dict()
        for rec in capability_matrix.values()
        if rec.status != CapabilityStatus.AVAILABLE
    ]
    unavailable = [g["object_name"] for g in gaps]
    state.data_gaps_count = len(gaps)
    plain = (
        "Alle beoogde HubSpot-objecten zijn leesbaar."
        if not unavailable
        else f"Sommige objecten zijn niet volledig leesbaar ({', '.join(unavailable)}); dit wordt als data gap vastgelegd."
    )
    state.complete_phase("capability_probe", plain)
    run_status["phases"]["capability_probe"] = {"gaps": len(gaps)}
    heartbeat()

    # --- phase: extraction ---
    state.start_phase("extraction")
    extraction_summary = extract_all(client, paths["raw"], config.hubspot.page_size, capability_matrix)
    state.complete_phase(
        "extraction",
        f"Ruwe data opgehaald voor {len([k for k, v in extraction_summary.items() if not v.get('skipped')])} objecttypen.",
    )
    run_status["phases"]["extraction"] = extraction_summary
    heartbeat()

    # --- phase: normalization ---
    state.start_phase("normalization")
    normalization_summary = normalize_all_wrapper(paths["raw"], paths["normalized"])
    state.complete_phase("normalization", "Data schoongemaakt en gestandaardiseerd voor analyse.")
    run_status["phases"]["normalization"] = normalization_summary
    heartbeat()
    state.data_volumes = {
        "companies": normalization_summary.get("companies", 0),
        "contacts": normalization_summary.get("contacts", 0),
        "deals": normalization_summary.get("deals", 0),
    }

    # --- phase: company analysis ---
    state.start_phase("company_analysis")
    companies_result = analyze_companies(paths["normalized"], config.thresholds)
    _register_company_findings(ledger, queue, companies_result)
    state.complete_phase(
        "company_analysis",
        f"Companies gecontroleerd: {companies_result['metrics']['duplicate_domain_groups']} mogelijke duplicate-domeingroepen gevonden en in de investigation queue gezet.",
    )
    heartbeat()

    # --- phase: contact analysis ---
    state.start_phase("contact_analysis")
    contacts_result = analyze_contacts(paths["normalized"], config.thresholds)
    _register_contact_findings(ledger, queue, contacts_result)
    state.complete_phase(
        "contact_analysis",
        f"Contacts gecontroleerd: {contacts_result['metrics']['missing_email']} contacts zonder e-mailadres.",
    )
    heartbeat()

    # --- phase: deal analysis ---
    state.start_phase("deal_analysis")
    deals_result = analyze_deals(paths["normalized"], config.thresholds)
    _register_deal_findings(ledger, deals_result)
    state.complete_phase(
        "deal_analysis",
        f"Deals gecontroleerd: {deals_result['metrics']['missing_company_association']} zonder company-koppeling.",
    )
    heartbeat()

    # --- phase: activity analysis ---
    state.start_phase("activity_analysis")
    activities_result = analyze_activities(paths["normalized"], config.thresholds)
    state.complete_phase(
        "activity_analysis",
        f"Activiteiten gecontroleerd: {activities_result['metrics']['overdue_tasks']} openstaande/overdue taken.",
    )
    heartbeat()

    # --- phase: property analysis ---
    state.start_phase("property_analysis")
    properties_result = analyze_properties(paths["normalized"], paths["raw"], config.thresholds)
    state.complete_phase("property_analysis", "CRM-velden geïnventariseerd op gebruik en vulgraad.")
    heartbeat()

    # --- phase: pipeline analysis ---
    state.start_phase("pipeline_analysis")
    pipelines_result = analyze_pipelines(
        paths["normalized"], config.thresholds, activities_result["most_recent_activity_by_deal"]
    )
    state.complete_phase(
        "pipeline_analysis",
        f"{pipelines_result['metrics']['pipeline_stages_with_stuck_deals']} pipeline-stages met vastzittende deals gevonden.",
    )
    heartbeat()

    # --- phase: historical imports ---
    state.start_phase("historical_imports")
    historical_result = analyze_historical_imports(paths["normalized"], config.thresholds)
    _register_historical_findings(ledger, queue, historical_result)
    state.complete_phase("historical_imports", "Onderzocht op sporen van historische bulk-imports.")
    heartbeat()

    # --- phase: cross object ---
    state.start_phase("cross_object")
    owners = [e["record"] for e in read_jsonl(os.path.join(paths["raw"], "owners.jsonl"))]
    no_activity_result = open_deals_without_recent_activity(deals_result, activities_result, config.thresholds)
    ownership_result = ownership_usage(paths["normalized"], owners)
    _register_cross_object_findings(ledger, no_activity_result)
    state.complete_phase(
        "cross_object",
        f"{no_activity_result['metrics']['open_deals_without_recent_activity']} open deals zonder recente activiteit gevonden.",
    )
    heartbeat()

    # --- phase: AI interpretation (works fully with AI disabled) ---
    state.start_phase("ai_interpretation")
    ai_notes = _run_ai_interpretation(interpreter, ledger, queue, config)
    if config.ai.enabled:
        state.add_decision(f"AI-interpretatie uitgevoerd voor {ai_notes['interpreted']} onderzoeksvragen binnen budget.")
        state.complete_phase("ai_interpretation", f"AI heeft {ai_notes['interpreted']} ambiguë clusters geïnterpreteerd.")
    else:
        state.add_decision("AI-interpretatie stond uit; alle bevindingen zijn volledig deterministisch bepaald.")
        state.complete_phase("ai_interpretation", "AI-interpretatie was uitgeschakeld voor deze run.")
    heartbeat()

    # --- phase: report generation ---
    state.start_phase("report_generation")
    metrics = {
        "companies": companies_result["metrics"],
        "contacts": contacts_result["metrics"],
        "deals": deals_result["metrics"],
        "activities": activities_result["metrics"],
        "properties": {k: v["metrics"] for k, v in properties_result.items()},
        "historical_imports": {k: v["metrics"] for k, v in historical_result.items()},
        "ownership": {**ownership_result["metrics"], "ownership_by_object": ownership_result["signals"]["ownership_by_object"]},
        "cross_object": no_activity_result["metrics"],
    }
    report_context = {
        "run_id": config.run_id,
        "generated_at": now_iso(),
        "mode": config.mode,
        "metrics": metrics,
        "capability_matrix": capability_matrix_to_dict(capability_matrix),
        "findings": ledger.to_findings_list(),
        "investigations": queue.to_list(),
        "cost_summary": interpreter.budget_summary(),
        "gaps": gaps,
        "company_signals": companies_result["signals"],
        "contact_signals": contacts_result["signals"],
        "pipeline_signals": pipelines_result["signals"],
    }
    report_path = os.path.join(paths["reports"], "index.html")
    generate_html_report(report_context, report_path)
    state.complete_phase("report_generation", "HTML-auditrapport geschreven.")
    heartbeat()

    # --- write required output artifacts ---
    next_actions = _build_next_actions(ledger, queue)
    analyses_summary = {
        "companies": companies_result["metrics"],
        "contacts": contacts_result["metrics"],
        "deals": deals_result["metrics"],
        "activities": activities_result["metrics"],
        "properties": properties_result,
        "pipelines": pipelines_result,
        "historical_imports": historical_result,
        "ownership": ownership_result,
        "cross_object": no_activity_result,
    }
    _write_json(os.path.join(paths["analyses"], "analyses.json"), analyses_summary)
    _write_json(os.path.join(paths["investigations"], "investigations.json"), queue.to_list())
    _write_json(os.path.join(paths["evidence"], "evidence.json"), ledger.to_evidence_list())
    _write_json(os.path.join(paths["evidence"], "findings.json"), ledger.to_findings_list())

    _write_json(os.path.join(paths["root"], "capability_matrix.json"), capability_matrix_to_dict(capability_matrix))
    _write_json(os.path.join(paths["root"], "gaps.json"), gaps)
    _write_json(os.path.join(paths["root"], "findings.json"), ledger.to_findings_list())
    _write_json(os.path.join(paths["root"], "evidence.json"), ledger.to_evidence_list())
    _write_json(os.path.join(paths["root"], "investigations.json"), queue.to_list())
    _write_json(os.path.join(paths["root"], "next_actions.json"), next_actions)

    state.status = "done"
    state.start_phase("done")
    state.complete_phase("done", "Audit afgerond. Rapport en Control Center-status zijn bijgewerkt.")
    heartbeat()

    run_status["finished_at"] = now_iso()
    run_status["findings_count"] = len(ledger.all())
    run_status["investigations_count"] = len(queue.all())
    run_status["gaps_count"] = len(gaps)
    _write_json(os.path.join(paths["root"], "run_status.json"), run_status)
    _log(paths["logs"], f"Run {config.run_id} finished")

    return {
        "run_status": run_status,
        "control_center_path": control_center_path,
        "report_path": report_path,
    }


def normalize_all_wrapper(raw_dir: str, normalized_dir: str) -> dict:
    from .normalization import normalize_all

    return normalize_all(raw_dir, normalized_dir)


def _register_company_findings(ledger: EvidenceLedger, queue: InvestigationQueue, result: dict) -> None:
    m, s = result["metrics"], result["signals"]
    ledger.add_finding(
        finding_id="company-missing-domain",
        title="Companies zonder domein",
        category="companies",
        evidence=[make_evidence("companies", "Companies zonder domeinveld", {"count": m["missing_domain"], "total": m["total_companies"]})],
        confidence=1.0,
        status=FindingStatus.CONFIRMED,
        reasoning_summary="Directe telling op genormaliseerde data; geen interpretatie nodig.",
    )
    for i, group in enumerate(s["duplicate_domains"][:50]):
        finding_id = f"company-dup-domain-{i:03d}"
        ledger.add_finding(
            finding_id=finding_id,
            title=f"Mogelijke duplicate companies op domein {group['key']}",
            category="companies",
            evidence=[make_evidence("companies", f"{group['count']} companies delen domein {group['key']}", {"count": group["count"]}, group["ids"])],
            confidence=0.4,
            status=FindingStatus.DETECTED,
            hypothesis="Deze companies zijn mogelijk duplicaten van dezelfde organisatie.",
            recommended_next_investigation="Zijn dit dezelfde juridische entiteit of afzonderlijke vestigingen/dochters?",
        )
        queue.push(
            question=f"Zijn de {group['count']} companies met domein {group['key']} duplicaten of losstaande entiteiten?",
            trigger_finding=finding_id,
            required_data=["company records", "parent/child hierarchy"],
            expected_information_value=min(0.9, 0.2 + 0.05 * group["count"]),
            depth=0,
        )
    if m["orphan_parent_references"] > 0:
        ledger.add_finding(
            finding_id="company-orphan-parent-refs",
            title="Companies verwijzen naar een niet-gevonden parent company",
            category="companies",
            evidence=[make_evidence("companies", "Parent-company referentie wijst niet naar een bekende company-ID in deze extractie", {"count": m["orphan_parent_references"]}, s["orphan_parent_reference_ids"])],
            confidence=0.6,
            status=FindingStatus.NEEDS_HUMAN_CONTEXT,
            hypothesis="Parent company bestaat niet meer, is buiten scope van deze extractie, of het veld is fout ingevuld.",
        )


def _register_contact_findings(ledger: EvidenceLedger, queue: InvestigationQueue, result: dict) -> None:
    m, s = result["metrics"], result["signals"]
    ledger.add_finding(
        finding_id="contact-missing-email",
        title="Contacts zonder e-mailadres",
        category="contacts",
        evidence=[make_evidence("contacts", "Contacts zonder e-mailveld", {"count": m["missing_email"], "total": m["total_contacts"]})],
        confidence=1.0,
        status=FindingStatus.CONFIRMED,
        reasoning_summary="Directe telling; geen interpretatie nodig.",
    )
    ledger.add_finding(
        finding_id="contact-missing-company",
        title="Contacts zonder company-associatie",
        category="contacts",
        evidence=[make_evidence("contacts", "Contacts zonder gekoppelde company", {"count": m["missing_company_association"], "total": m["total_contacts"]})],
        confidence=1.0,
        status=FindingStatus.CONFIRMED,
    )
    for i, group in enumerate(s["duplicate_emails"][:50]):
        finding_id = f"contact-dup-email-{i:03d}"
        ledger.add_finding(
            finding_id=finding_id,
            title=f"Duplicate contacts op e-mailadres {group['key']}",
            category="contacts",
            evidence=[make_evidence("contacts", f"{group['count']} contact-records met hetzelfde e-mailadres", {"count": group["count"]}, group["ids"])],
            confidence=0.7,
            status=FindingStatus.DETECTED,
            hypothesis="Dit is zeer waarschijnlijk dezelfde persoon meerdere keren geregistreerd.",
            recommended_next_investigation="Zijn dit dezelfde persoon (merge-kandidaat) of gedeeld functioneel mailadres?",
        )
        queue.push(
            question=f"Is e-mailadres {group['key']} ({group['count']} contacten) een echte duplicate-persoon of een gedeeld mailbox-adres?",
            trigger_finding=finding_id,
            required_data=["contact records"],
            expected_information_value=min(0.85, 0.3 + 0.05 * group["count"]),
            depth=0,
        )


def _register_deal_findings(ledger: EvidenceLedger, result: dict) -> None:
    m, s = result["metrics"], result["signals"]
    for finding_id, title, count in (
        ("deal-missing-company", "Deals zonder company-associatie", m["missing_company_association"]),
        ("deal-missing-contact", "Deals zonder contact-associatie", m["missing_contact_association"]),
        ("deal-missing-owner", "Deals zonder owner", m["missing_owner"]),
    ):
        ledger.add_finding(
            finding_id=finding_id,
            title=title,
            category="deals",
            evidence=[make_evidence("deals", title, {"count": count, "total": m["total_deals"]})],
            confidence=1.0,
            status=FindingStatus.CONFIRMED,
        )

    if m["suspicious_dates"] > 0:
        ledger.add_finding(
            finding_id="deal-suspicious-dates",
            title="Deals met closedate vóór createdate",
            category="deals",
            evidence=[make_evidence("deals", "Sluitingsdatum ligt vóór aanmaakdatum", {"count": m["suspicious_dates"]}, s["suspicious_date_ids"])],
            confidence=0.9,
            status=FindingStatus.CONFIRMED,
            reasoning_summary="Logisch onmogelijke datumcombinatie; feitelijke datafout.",
        )
    if m["stale_open_deals"] > 0:
        ledger.add_finding(
            finding_id="deal-stale-open",
            title="Langdurig niet-bijgewerkte open deals",
            category="deals",
            evidence=[make_evidence("deals", "Open deal niet bijgewerkt binnen de stale-drempel", {"count": m["stale_open_deals"]}, s["stale_open_deal_ids"])],
            confidence=0.8,
            status=FindingStatus.CONFIRMED,
        )


def _register_historical_findings(ledger: EvidenceLedger, queue: InvestigationQueue, result: dict) -> None:
    for object_type, data in result.items():
        for i, cohort in enumerate(data["signals"]["migration_cohort_candidates"][:20]):
            finding_id = f"{object_type}-migration-cohort-{i:03d}"
            ledger.add_finding(
                finding_id=finding_id,
                title=f"Mogelijk migratie-cohort in {object_type} op {cohort['date']}",
                category="historical_imports",
                evidence=[make_evidence(
                    object_type,
                    f"{cohort['record_count']} records aangemaakt op {cohort['date']}, {int(cohort['dominant_source_share']*100)}% met source {cohort['dominant_source']}",
                    {"count": cohort["record_count"]},
                )],
                confidence=0.3,
                status=FindingStatus.DETECTED,
                hypothesis="Dit kan een bulk-import zijn (bijv. Salesforce- of Apollo-migratie) in plaats van organische groei.",
                recommended_next_investigation="Bevestig met de business of deze datum samenvalt met een bekende migratie.",
            )
            queue.push(
                question=f"Valt de {object_type}-burst op {cohort['date']} ({cohort['record_count']} records, bron {cohort['dominant_source']}) samen met een bekende migratie?",
                trigger_finding=finding_id,
                required_data=[f"{object_type} creation dates", "source property"],
                expected_information_value=0.3,
                depth=0,
                needs_human_context=True,
            )


def _register_cross_object_findings(ledger: EvidenceLedger, result: dict) -> None:
    m, s = result["metrics"], result["signals"]
    if m["open_deals_without_recent_activity"] > 0:
        ledger.add_finding(
            finding_id="deal-no-recent-activity",
            title="Open deals zonder recente activiteit",
            category="cross_object",
            evidence=[make_evidence("deals+activities", "Open deal zonder call/meeting/email/note/task binnen de stale-drempel", {"count": m["open_deals_without_recent_activity"]}, s["open_deal_ids_without_recent_activity"])],
            confidence=0.8,
            status=FindingStatus.CONFIRMED,
        )


def _run_ai_interpretation(interpreter, ledger: EvidenceLedger, queue: InvestigationQueue, config) -> dict:
    from .models import InvestigationStatus

    interpreted = 0
    if not config.ai.enabled:
        return {"interpreted": 0}

    known_finding_ids = {f.finding_id for f in ledger.all()}
    investigation = queue.pop_next()
    while investigation is not None:
        trigger = investigation.trigger_finding
        finding = ledger.get(trigger) if trigger in known_finding_ids else None
        candidates = [
            {"description": ev.description, "counts": ev.counts, "sample_record_ids": ev.sample_record_ids}
            for ev in (finding.evidence if finding else [])
        ]
        result = interpreter.interpret_cluster(trigger or investigation.investigation_id, investigation.question, candidates)
        if result is None:
            queue.resolve(investigation.investigation_id, InvestigationStatus.STOPPED_BUDGET, "AI uitgeschakeld of budget bereikt.")
            break

        interpreted += 1
        queue.resolve(investigation.investigation_id, InvestigationStatus.DONE, result.reasoning_summary)
        if finding:
            ledger.transition(finding.finding_id, FindingStatus(result.status))
            finding.hypothesis = result.hypothesis or finding.hypothesis
            finding.reasoning_summary = result.reasoning_summary
            finding.recommended_next_investigation = result.recommended_next_investigation
            finding.potential_remediation = result.potential_remediation
        investigation = queue.pop_next()

    return {"interpreted": interpreted}


def _build_next_actions(ledger: EvidenceLedger, queue: InvestigationQueue) -> list:
    actions = []
    for f in ledger.all():
        if f.status == FindingStatus.CONFIRMED and f.category != "historical_imports":
            actions.append(
                {
                    "priority": round(f.confidence, 2),
                    "action": f"Remedieer: {f.title}",
                    "source": f.finding_id,
                    "rationale": f"Bevestigde bevinding met vertrouwen {f.confidence:.2f}.",
                }
            )
    for inv in sorted(queue.all(), key=lambda i: -i.expected_information_value)[:20]:
        actions.append(
            {
                "priority": round(inv.expected_information_value, 2),
                "action": f"Onderzoek: {inv.question}",
                "source": inv.investigation_id,
                "rationale": f"Verwachte informatiewaarde {inv.expected_information_value:.2f}, status {inv.status.value}.",
            }
        )
    actions.sort(key=lambda a: -a["priority"])
    return actions
