# Caller Workload Manager — herontwerp van de caller-reallocatie-app

Volledig herontwerp van `reallocate_callers_streamlit_app.py` voor het
managementteam. De onderliggende, geteste kern
(`reallocate_callers_from_gcs.py`, `caller_range_assignment.py`) is
ongewijzigd; alleen de UI-laag en haar pure helpers zijn nieuw.

```bash
streamlit run reallocate_callers_streamlit_app.py
```

## Wat er mis was met de oude app

- Geen mentaal model: per beller een uitklap-paneel met een eenheden-dropdown
  (count/percentiel/cohort) — je zag nooit het geheel (de ranking die je aan
  het verdelen bent).
- De "limiet" (welke bedrijven bellers überhaupt te zien krijgen) bestond
  alleen impliciet: wat buiten elke range viel werd stilletjes "— (none)".
- Geen score-feedback: je kon nergens zien "hoeveel bedrijven zitten er
  tussen score X en Y?".
- Upload/promotie als twee tekstblokken met gs://-paden in de checkboxen —
  intimiderend voor niet-technische gebruikers.

## Het nieuwe ontwerp: één pagina, één verhaal

1. **Country** (zijbalk) — land laden; bucket en de rank-optie zitten onder
   "Advanced". **"Rank by current score" staat nu standaard AAN** (de oude
   app stond standaard uit): na een re-score klopt de volgorde dan altijd;
   uitzetten behoudt de oorspronkelijke export-volgorde.
2. **Focus — which companies are in play?** Dit is de "limiet", nu een
   eersteklas keuze: *All companies* / *Only the top N* / *Only a score
   range*. Live feedback: "512 of 5,288 in play", score-bereik, tiertelling,
   en een histogram met de gekozen band gemarkeerd (score-range) of de
   top-N-grens als lijn. Alles buiten de focus krijgt géén beller en
   verdwijnt uit de bellijsten — expliciet zo benoemd.
3. **Team** — bellers in een simpele tabel (rijen toevoegen/verwijderen).
   Volgorde = prioriteit: de eerste beller krijgt het beste blok.
4. **Divide the work** — twee smaken:
   - **Blocks**: elke beller een aaneengesloten stuk van de ranking.
     Bewerkbare tabel (From/To in posities binnen de focus) met live
     kolommen *Companies / Best score / Lowest score*; "Split evenly"-knop;
     duidelijke meldingen bij gaten ("74 companies in play have no caller
     yet, positions 251–324") en overlap ("laagste blok in de tabel wint").
     Daaronder een **allocation map**: de hele ranking als één balk,
     gekleurd per beller, rood = nog geen beller, grijs = buiten focus.
   - **Mixed**: om-en-om over de bellers, zodat iedereen dezelfde mix van
     hoge/lage scores krijgt (identiek aan de export-round-robin wanneer de
     focus op "alles" staat).
5. **Check the result** — per-beller-samenvatting (aantal, beste/laagste
   score, tiers), before/after-werklastgrafiek, verplaatsingen in een
   uitklapbaar detail.
6. **Publish** — twee bewuste stappen naast elkaar: **A · Save draft**
   (schrijft een run-map; niets live) en **B · Make live** (promoot een
   draft naar `current/`). Checkbox-teksten in gewone taal; de vorige
   live-versie blijft als terugvaloptie bestaan.

## Techniek

- Alle nieuwe logica als pure, geteste helpers (`focus_selection`,
  `assignment_from_blocks`, `assignment_interleaved`, `blocks_coverage`,
  `blocks_with_feedback`, `allocation_map_segments`,
  `per_caller_summary_dataframe`, …) — 40 tests in
  `test_reallocate_callers_streamlit_app.py`, geen Streamlit nodig.
- Score-range-focus filtert op de score-wáárde (niet op rangcontinuïteit),
  dus ook correct op een verouderde run waar rank en score uiteenlopen.
- Blokposities tellen bínnen de focus (positie 1 = beste bedrijf in play),
  zodat "top N" + blokken samen blijven kloppen; weggeschreven ranks blijven
  de volledige ranking volgen (Hub-volgorde intact).
- Bij overlap wint het laagste blok in de tabel — zelfde semantiek als de
  oude range-kern.
- End-to-end headless geverifieerd met Streamlits AppTest: focus wisselen,
  live tellingen, mixed/blocks, en de publish-knoppen die vergrendeld
  blijven zonder bevestiging.

## Losse reparatie

De batch-download-optimalisatie van `download_current_run` (één `cp` voor
alle bestanden) had de subprocess-fake in
`test_reallocate_callers_from_gcs.py` gebroken (die kende alleen de
één-bestand-vorm) — 3 tests faalden daardoor. De fake ondersteunt nu beide
vormen, net als die in `test_rescore_from_gcs.py`.
