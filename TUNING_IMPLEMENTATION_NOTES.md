# Tuning-implementatie: per-signaal drempel + own-domain-poort

Implementatie van voorstel **#1 (per-signaal drempel-verlaging)** en **#2 (domein-match-check)**
uit `icp_compare_20260706_113531/ICP_COMPARE_REPORT.md`. Branch: `work`.

## Wat is gewijzigd (en waarom)

Alleen de niet-HQ keyword-tier-logica. Twee chronisch onder-kleurende signalen —
`icp_keyword_match` (explicit L&D) en `international_profile` (international business context) —
bereiken nu de positieve tier al bij **1 keyword-hit**, MITS minstens één bruikbare
evidence-URL op het **eigen registreerbare domein** van het bedrijf staat (own-domain-poort).
Die poort blokkeert tegelijk:
- entiteitsvervuiling (SIDEL `sidelitalia.it` → bewijs op `sidel.com`; Villa Silvana `korian.it`
  → Korian/Samsung/Kion-mix), en
- hosted platforms / aggregators (Glassdoor, Indeed, LinkedIn-company-pages, …) — hun
  registreerbare root is nooit gelijk aan die van het bedrijf.

Ongewijzigd (bewust, conform scope):
- `company_size_complexity` en `onboarding_training_need` — presteren al goed, blijven op ≥2.
- `employer_branding` — leunt bijna volledig op hosted review-platforms; verlagen zou
  vals-positieven fabriceren (rapport §4). Blijft op ≥2.
- HQ / foreign-ownership-signaal — buiten scope, niet aangeraakt.
- Geen query-lokalisatie (`gl`/`hl`), geen `--legacy-enrichment-mode`/`--ai-signal-scoring`.

## Gewijzigde bestanden

| Bestand | Wijziging |
|---|---|
| `lead_non_hq_signal_extractor.py` | Config-blok `_SIGNAL_POSITIVE_THRESHOLD` + `_OWN_DOMAIN_GATED_SIGNALS` + `_DEFAULT_POSITIVE_THRESHOLD` bovenaan; helper `_evidence_on_company_domain`; `extract_non_hq_signals(evidence_items, company_domain=None)` met per-signaal drempel + own-domain-poort; auditregel in `signal_reason` bij promotie; `SIGNAL_EXTRACTOR_VERSION` → `v3-own-domain-threshold`. |
| `lead_prioritizer_core.py` | Beide aanroepen van `extract_non_hq_signals(...)` geven nu `company_domain=input_row.domain` door (dekt CLI én Streamlit-app, beide via `prioritize_single_lead`). |
| `test_lead_non_hq_signal_extractor.py` | Nieuwe klasse `TestOwnDomainThreshold` (9 tests): promotie mét eigen-domein, geen promotie zonder domein / off-domain / hosted-platform, 0-hits nooit gepromoveerd, `employer_branding` en niet-gated signalen ongewijzigd, subdomein/pseudo-TLD-match. |

De drempels en de poort staan als **data** bovenaan `lead_non_hq_signal_extractor.py`, niet
verspreid door de code — makkelijk te tunen of terug te draaien.

## Configureerbaarheid: `_SIGNAL_KEYWORDS` naar keyword-groepen; drempels apart

```python
_DEFAULT_POSITIVE_THRESHOLD = 2
_SIGNAL_POSITIVE_THRESHOLD = {"icp_keyword_match": 1, "international_profile": 1}
_OWN_DOMAIN_GATED_SIGNALS = frozenset({"icp_keyword_match", "international_profile"})
```

## Testresultaat

- `test_lead_non_hq_signal_extractor.py`: **62 passed** (53 bestaand + 9 nieuw).
- Regressiesuites `test_lead_v2_full_pipeline.py`, `test_lead_app_summary_builder.py`,
  `test_lead_prioritizer_batch_core.py`: **249 passed** (1 bestaande, ongerelateerde FutureWarning).

**Oud-drempel vs nieuw-drempel op batch-1 evidence** (vaste evidence uit de eerdere run,
deterministisch — zie `icp_compare_20260706_113531/`):
- `SOLGAR` `icp_keyword_match`: 1.0 → **2.0** (eigen-domein `solgar.it/lavora-con-noi`).
- `INTERNATIONAL SCHOOL OF EUROPE` `icp_keyword_match`: 1.0 → **2.0** (eigen-domein).
- `SIDEL` `icp_keyword_match`: **blijft 1.0** — domein-poort blokkeert `sidel.com`-bewijs
  (input `sidelitalia.it`). Vervuiling correct geweerd. ✅
- **0 regressies** op niet-gated signalen (`employer_branding`, `company_size_complexity`,
  `onboarding_training_need` byte-identiek oud vs nieuw).

**Live end-to-end** (2 bedrijven door de echte CLI/app-pipeline): `company_domain` stroomt
correct door; SIDEL bleef weak via de poort; `employer_branding`-logica ongewijzigd. (Serper-
resultaten variëren per run, dus de deterministische promotie-demo staat in de reconstructie
hierboven, niet in de live run.)

## Terugdraai-scenario

Volledig terug naar het oude uniforme ≥2-gedrag zonder verdere codewijziging:

```python
# in lead_non_hq_signal_extractor.py
_SIGNAL_POSITIVE_THRESHOLD: dict[str, int] = {}      # leeg
_OWN_DOMAIN_GATED_SIGNALS = frozenset()              # leeg
```

Dan valt elk signaal terug op `_DEFAULT_POSITIVE_THRESHOLD = 2`. De extra `company_domain`-
parameter blijft dan ongebruikt (default `None`) en is verder onschadelijk. Zet desgewenst ook
`SIGNAL_EXTRACTOR_VERSION` terug naar `"v2-multilingual"`. Eén-signaal terugdraaien (bijv. alleen
`international_profile`) kan door dat signaal uit beide collections te verwijderen.

## Lokaal bekijken in de Streamlit-app

Geen deploy naar een gedeelde/productieomgeving. Lokaal starten:

```bash
streamlit run lead_prioritizer_batch_app.py
```

Upload in de app een klein Excel-bestand (kolommen bedrijfsnaam + domein + land), draai de
enrichment, en bekijk per bedrijf de niet-HQ-signalen. Bij een via de verlaagde drempel
gepromoveerd signaal staat in de reden-tekst expliciet
"promoted to positive at lowered per-signal threshold … backed by own-domain evidence".
