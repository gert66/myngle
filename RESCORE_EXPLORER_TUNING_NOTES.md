# Re-score Explorer: model-tuning & snelle preview

Wijzigingen aan `rescore_streamlit_app.py` (de "recalculator") en
`commercial_fit_scoring.py`. Branch: `claude/recalculator-model-tuning-x2piw7`.

## Waarom de top bleef hangen rond 8.7 (en hoe dat nu oplosbaar is)

Twee oorzaken, allebei nu aan te pakken vanuit de app:

1. **Vaste sigmoid-ankers.** `icp_similarity_score` wordt herschaald tussen
   twee vaste anker-kansen (`_SIGMOID_P_LO = 0.35734`, `_SIGMOID_P_HI =
   0.76427`) uit een oude referentiepopulatie. Halen de beste bedrijven van
   een land die boven-anker-kans niet, dan komt hun ICP-score nooit op 10 —
   en de eindscore dus ook niet. `score_company()` accepteert nu
   `params["sigmoid_p_lo"]` / `params["sigmoid_p_hi"]`, en de app kan die
   **kalibreren op de geladen data zelf** (p5 → score 1, p95 → score 10).
   Alles op/boven het hoge percentiel klemt op 10, alles op/onder het lage
   op 1 — de verdeling spant weer het volledige 1–10-bereik.
2. **Ontbrekende bedrijfsgrootte.** De blend is 75% ICP + 25% grootte.
   Zonder `employee_range` valt de grootte-score terug op neutraal 5.5;
   zelfs een perfecte ICP-10 komt dan maar op 0.75×10 + 0.25×5.5 = **8.88**.
   De Lucia-data zit al in de export (`scoring_inputs.employee_range`); de
   Impact-tab toont nu de **dekking** ervan, en herscoren met het standaard
   75/25-profiel neemt de grootte automatisch mee.

## Nieuw in de app

| Onderdeel | Wat het doet |
|---|---|
| **📊 Impact-tab** (eerste tab) | KPI's (max, top-25-gemiddelde, scheefheid huidig vs. nieuw), percentielentabel, before/after-histogrammen, **top-bedrijven hoog → laag**, size-dekking. |
| **🎯 Auto-kalibratie K + intercept** (Impact-tab) | Zoekt deterministisch (grid search) de intercept + K waarbij het p95-bedrijf op het top-doel landt (standaard 9.3, de echte top loopt door tot ~10) en het p5-bedrijf op het onderkant-doel (standaard 4.0). **Signaalgewichten, blend en ankers blijven onaangeroerd.** Optionele checkbox (standaard aan): foreign-HQ-gewicht licht verlagen (0.7465 → 0.65, de vijf andere positieve signalen iets omhoog, totaalgewicht gelijk) — verkleint de twee-bulten-verdeling die het dominante HQ-signaal veroorzaakt. Doelen instelbaar; resultaat (bereikt p95/p5/mediaan) blijft zichtbaar. Verving de eerdere "🪄 preset" die de ankers verschoof. |
| **⚡ Snelle preview** (zijbalk, standaard aan) | Herscoort per slider-tweak alleen een **deterministische percentiel-steekproef** (standaard 300; ranking hoog → laag, top-25 altijd volledig, rest gelijkmatig gespreid over de percentielen). Uploaden herscoort altijd álle bedrijven. Geldt ook voor de "Alle landen"-preview. |
| **🎯 Anker-kalibratie** (Sigmoid-tab) | Kalibreer p_lo/p_hi op instelbare percentielen van de geladen populatie; herstel-knop naar de standaard-ankers. Ankers zichtbaar in de audit (`sigmoid_p_lo`/`sigmoid_p_hi` in `SCORE_OUTPUT_COLS` en in de `rescore_audit.params` van elke run). |
| **📐 Tier-drempel-voorstel** (Tier-tab) | Drempels uit de níeuwe verdeling volgens de oorspronkelijke 10/20/30-regel (top 10% Hot, volgende 20% Warm, volgende 30% Cool). |
| **Reset-knop gerepareerd** | Reset zette wel de params terug maar niet de slider-widgetstate, waardoor de oude waarden direct terugkwamen. Programmatische updates (preset, kalibratie, voorstel, reset) lopen nu via een pending-queue die vóór widget-instantiatie wordt toegepast. |
| **Upload-flow** | De volledige her-score van het hele land gebeurt pas bij de upload-klik (met spinner + manifest-metrics), niet meer bij elke rerun. |

## K-factor en intercept

- **K (sigmoid-steilheid)** bepaalt hoe hard de kansen naar 1 en 10 worden
  geduwd; verandert de ránking nooit, alleen de spreiding.
- **Intercept** verschuift alle kansen (en dus de hele verdeling) tegelijk.
- De **auto-kalibratie op de Impact-tab** zoekt beide sámen: intercept
  centreert de populatie, K spreidt haar, tot p95/p5 op de doelen zitten.
  Afhankelijk van de data kan K daarbij omhoog óf omlaag gaan — het doel
  (top ≈ 9+, onderkant ≈ 4) is leidend, niet een vaste K-waarde.
- De **anker-kalibratie** op de Sigmoid-tab is het alternatieve mechanisme
  (verschuift p_lo/p_hi in plaats van K/intercept); gebruik één van de
  twee, niet beide tegelijk.

## Bedrijfsgrootte in v2-exports (Spanje, …) — gevonden en gerepareerd

Italië toonde bedrijfsgroottes, Spanje niet. Oorzaak: de v2-pijplijn bewaart
de Lusha-range onder **`lusha_employee_range`** en laat de kolom
`employee_range` bewust leeg (`lead_v2_scoring_adapter.py`). De exporter las
alléén `employee_range` → voor v2-landen bleven én het app-veld én
`scoring_inputs.employee_range` leeg, en her-scores vielen stil terug op de
neutrale grootte-score 5.5. Drie fixes:

1. **Exporter** (`export_lead_prioritizer_to_lovable_json.py`): leest nu
   dezelfde alias-keten als de scoring-engine
   (`resolve_employee_range_value`: lusha_api → lusha → employee_range →
   company_size) voor zowel het app-veld als `scoring_inputs`.
2. **Re-score** (`rescore_from_gcs.py`): bestaande v2-records in GCS hebben
   de Lusha-kolommen nog onder `debug.lead_prioritizer_row`;
   `resolve_detail_employee_range` haalt de range daar terug (audit-spoor:
   `employee_range_source`), scoort met de échte grootte, en **vult de lege
   app-velden** (`employee_range`, `size_category_app`,
   `display_size_category_app`) op detail- én lijstrecords — expliciete
   bestaande waarden worden nooit overschreven. Eén re-score + promotie
   maakt de groottes dus zichtbaar in Spanje, zonder her-export.
3. **Dekking-paneel** (Impact-tab) telt nu via dezelfde keten en toont per
   bron (scoring_inputs / detailrecord / debug-rij) waar de range vandaan
   komt.

Let op: hierdoor verschuiven Spaanse her-scores t.o.v. eerdere her-scores —
de 25% grootte-blend rekent nu met echte groottes in plaats van overal 5.5.
Dat is precies de bedoeling ("company size implemented").

## Ongewijzigd gedrag

- Standaard-defaults van `commercial_fit_scoring` zijn identiek (zonder
  nieuwe params exact dezelfde uitkomsten — smoke tests + regressiesuites
  groen: 358 passed).
- Upload schrijft nog steeds uitsluitend naar een nieuwe run-folder;
  `current/` verandert pas na expliciete promotie
  (`promote_run_to_current`). Gekozen params (incl. ankers) staan in het
  run-manifest, dus een goede tuning is reproduceerbaar via
  `rescore_from_gcs.py --params-json`.

## Downloaden was traag — niet door de steekproefgrootte

De "⚡ Snelle preview"-steekproef versnelt alléén het *herscoren* ná het
laden (elke slider-tweak); ze verkleint niets aan het éénmalige downloaden
van `current/` uit de cloud — dat gebeurt nog steeds voor het hele land,
ongeacht de ingestelde steekproefgrootte.

Het echte probleem: `download_current_run` deed vroeger één losse
`gcloud storage cp`/`gsutil cp`-subprocesaanroep **per bestand**. Een land
met duizenden bedrijven heeft weliswaar maar een handvol
`company-details-*.json`-bucketbestanden (500 bedrijven per bucket), maar
elke aparte CLI-aanroep betaalt zijn eigen opstart-/auth-overhead — bij een
land als Spanje (5.288 bedrijven → 11 bucketbestanden + manifest + lijst =
13 aanroepen) telde dat op tot enkele minuten, puur overhead, geen
netwerktransport.

**Fix:** alle bestanden worden nu in **één** `cp`-aanroep gedownload
(`download_files_batch`; `cp bron1 bron2 … bestemmingsmap/`, ondersteund
door zowel `gcloud storage` als `gsutil`). Dat scheelt niet lineair met het
aantal bedrijven maar met het aantal *bestanden* — en dat blijft klein
ongeacht landgrootte. De laadknop toont nu ook hoeveel seconden het
downloaden kostte, zodat dit meetbaar is. Test
`test_many_detail_buckets_use_one_cp_call_not_one_per_file` bevestigt: 11
bucketbestanden → exact 1 `ls`- en 1 `cp`-aanroep, niet 13.

## Aanbevolen werkwijze

1. Land laden → **📊 Impact** → **🪄 preset** klikken.
2. Controleren: top-bedrijven ≈ 9.5–10, scheefheid dichter bij 0,
   size-dekking hoog genoeg.
3. **📐 tier-drempels** opnieuw laten voorstellen (de verdeling is verschoven).
4. Eventueel K/coëfficiënten bijstellen; snelle preview houdt het vlot.
5. **🚀 Toepassen & uploaden** → run reviewen → apart promoten naar current.
