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
| **🪄 Preset "minder scheef & top ≈ 10"** | Zet K → 6 (mildere spreiding) + kalibreert de ankers op de geladen data. Eén klik, daarna fijnafstemmen met de sliders. |
| **⚡ Snelle preview** (zijbalk, standaard aan) | Herscoort per slider-tweak alleen een **deterministische percentiel-steekproef** (standaard 300; ranking hoog → laag, top-25 altijd volledig, rest gelijkmatig gespreid over de percentielen). Uploaden herscoort altijd álle bedrijven. Geldt ook voor de "Alle landen"-preview. |
| **🎯 Anker-kalibratie** (Sigmoid-tab) | Kalibreer p_lo/p_hi op instelbare percentielen van de geladen populatie; herstel-knop naar de standaard-ankers. Ankers zichtbaar in de audit (`sigmoid_p_lo`/`sigmoid_p_hi` in `SCORE_OUTPUT_COLS` en in de `rescore_audit.params` van elke run). |
| **📐 Tier-drempel-voorstel** (Tier-tab) | Drempels uit de níeuwe verdeling volgens de oorspronkelijke 10/20/30-regel (top 10% Hot, volgende 20% Warm, volgende 30% Cool). |
| **Reset-knop gerepareerd** | Reset zette wel de params terug maar niet de slider-widgetstate, waardoor de oude waarden direct terugkwamen. Programmatische updates (preset, kalibratie, voorstel, reset) lopen nu via een pending-queue die vóór widget-instantiatie wordt toegepast. |
| **Upload-flow** | De volledige her-score van het hele land gebeurt pas bij de upload-klik (met spinner + manifest-metrics), niet meer bij elke rerun. |

## K-factor en intercept

- **K (sigmoid-steilheid)** bepaalt hoe hard de kansen naar 1 en 10 worden
  geduwd. K=10 (default) maakt de verdeling bimodaal/scheef; de preset zet
  **K=6**. Verandert de ránking nooit, alleen de spreiding.
- **Intercept** verschuift alle kansen tegelijk. Met gekalibreerde ankers is
  het effect op de eindscores grotendeels weggenormaliseerd — kalibratie is
  het effectievere instrument; de intercept-slider blijft beschikbaar.

## Ongewijzigd gedrag

- Standaard-defaults van `commercial_fit_scoring` zijn identiek (zonder
  nieuwe params exact dezelfde uitkomsten — smoke tests + regressiesuites
  groen: 358 passed).
- Upload schrijft nog steeds uitsluitend naar een nieuwe run-folder;
  `current/` verandert pas na expliciete promotie
  (`promote_run_to_current`). Gekozen params (incl. ankers) staan in het
  run-manifest, dus een goede tuning is reproduceerbaar via
  `rescore_from_gcs.py --params-json`.

## Aanbevolen werkwijze

1. Land laden → **📊 Impact** → **🪄 preset** klikken.
2. Controleren: top-bedrijven ≈ 9.5–10, scheefheid dichter bij 0,
   size-dekking hoog genoeg.
3. **📐 tier-drempels** opnieuw laten voorstellen (de verdeling is verschoven).
4. Eventueel K/coëfficiënten bijstellen; snelle preview houdt het vlot.
5. **🚀 Toepassen & uploaden** → run reviewen → apart promoten naar current.
