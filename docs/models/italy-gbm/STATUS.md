# Italië — status Chamber-of-Commerce-verificatie

**Laatst bijgewerkt:** 2026-07-23, na onderzoek op 2026-07-22/23.

## Kernvondst

Van de bedrijven die geparkeerd staan in `italy_parked_uncertain_and_nodomain.xlsx`
(6.621 rijen: 4.973 `uncertain` + 936 `not_verified` + 712 `reject`) is
**5.621 al inhoudelijk goedgekeurd** als goede prospect
(`myngle_target_eligibility = KEEP`, zie `italy_chamber_only_verified.xlsx`).
Ze zijn dus geen slechte bedrijven — ze zitten vast op een domeinverificatieprobleem
en zijn nooit doorgestroomd naar de live Company Hub-app (de ~19.600 bedrijven die
callers nu te zien krijgen).

**Alleen Chamber-of-Commerce-bedrijven** zitten in dit parkeerbestand
(`channels` = 100% `chamber_of_commerce`) — geen enkel Lusha-bedrijf heeft dit probleem.

## Root cause (bevestigd met concrete voorbeelden + live Lusha-API-check)

Grote concerns met meerdere lokale juridische entiteiten onder **één centraal
groepsdomein** krijgen van de Firecrawl/Serper-domeinverificatiestap
(`hq_simple_detector.py`, zie ook `LEAD_PRIORITIZER_ENRICHMENT_DATAFLOW.md` in de
GitHub-repo) een lage-zekerheidsoordeel (`uncertain`/`reject`), omdat niet valt te
bepalen welk domein bij welke specifieke entiteit hoort.

Voorbeelden:
- **Fives Itas / Fives Intralogistics / Fives Oto** → alle drie onder `fivesgroup.com`.
  Live Lusha-check: Fives staat in Lusha geregistreerd onder **Frankrijk (Parijs)**,
  niet Italië — een land-gefilterde Lusha-pull zou dit bedrijf dus sowieso nooit vinden.
- **SKF Industrie / Automotive / Seals Italy** → gedeeld domein `skf.com` (of fout
  toegewezen: SKF Seals Italy kreeg per ongeluk `federazionegommaplastica.it`, de
  website van de Italiaanse rubber/kunststof-brancheorganisatie).
- Ter vergelijking (niet Chamber-of-Commerce, maar zelfde soort probleem): **Oliver
  Wyman GmbH** — al klant sinds 2015, staat in Lusha geregistreerd onder de **VS**
  (New York), dus ook nooit via een Duitsland-pull te vinden.

**Conclusie**: Lusha vangt multinationals onder hun wereldwijde hoofdkantoor;
Chamber-of-Commerce-data vangt de lokale entiteit wél correct — maar loopt vast in
de verificatiestap erna. De twee bronnen zijn dus complementair, alleen werkt de
huidige verificatielogica niet goed voor bedrijven met een gedeeld groepsdomein.

## Nieuw bestand

**`italy_parked_keep_5621.xlsx`** — schone lijst van precies deze 5.621 bedrijven
(`CompanyName`, `Domain`, `MasterID`, `DomainReviewStatus`, `FinalDomain`,
`FinalConfidence`, `VerifierDecision`, `PreFilterReason`), gebouwd door
`italy_parked_uncertain_and_nodomain.xlsx` en `italy_chamber_only_verified.xlsx`
te koppelen op `master_id`.

## Openstaande beslissing

Twee opties om de 5.621 bedrijven alsnog naar de live app door te sluizen:

1. **Snel**: verlaag de acceptatiedrempel voor `KEEP`-bedrijven met status
   `uncertain`, laat ze door naar de app met een zichtbaar
   "laag-vertrouwen-domein"-label voor de caller. Geen codewijziging aan de
   verifier nodig; klein kwaliteitsrisico (zie het SKF-Seals-voorbeeld hierboven).
2. **Structureel**: pas de verifier zelf aan zodat een gedeeld groepsdomein bewust
   geaccepteerd wordt in plaats van afgewezen. Nettere, herbruikbare oplossing,
   maar meer bouwtijd voordat er iets verandert.

**Nog niet gekozen — hier verdergaan.**

## Workflow-tijdlijn ter referentie (oorspronkelijke run 15 juli 2026, 09:43–21:20)

| Tijd | Bestand | Stap |
|---|---|---|
| 09:43 | `Italy_Companies_Lusha_vs_Chamber_of_Commerce_Master.xlsx` | Ruwe Lusha + Chamber-of-Commerce samengevoegd/gematcht op domein (27.574 rijen) |
| 10:35 | `italy_input_lusha.xlsx` + `italy_input_chamber_only.xlsx` | Gesplitst in twee invoerstromen |
| 13:21 | `register_cleaned_..._haikuuncertain_8026rows...xlsx` | Eerste AI-opschoning (Claude Haiku) van de Chamber-only-stroom |
| 16:58 | `italy_chamber_only_verify_checkpoint.jsonl` + `italy_chamber_only_verified.xlsx` | Domeinverificatie (Serper + Firecrawl + AI) |
| 17:02 | `italy_chamber_only_final.xlsx` | Verified-bestand teruggebracht tot compacte finale versie |
| 17:05 | `italy_combined_for_batch.xlsx` | Chamber-only-final weer samengevoegd met Lusha-stroom |
| 17:55 | `italy_parked_uncertain_and_nodomain.xlsx` | **Parkeermoment** — uncertain/not_verified/reject eruit gefilterd |
| 18:05 | `italy_dedup_removed_rows.xlsx` | Losse controle op dubbele/verdachte domeinbevestigingen |
| 18:12 | `italy_batch_input_final.xlsx` | Definitieve, schone invoer voor scoring |
| 21:20 | `italy_lead_prioritizer_final.xlsx` | Volledige gescoorde uitvoer (84MB) → voedt via export-script de live app |

## Relevante bestanden elders

- Scoringcode: `commercial_fit_scoring.py`, `hq_simple_detector.py`,
  `lead_v2_scoring_adapter.py`, `lead_prioritizer_core.py` in de GitHub-repo
  (`C:\Users\gmeijer4\OneDrive - UMC Utrecht\Documenten\GitHub\myngle`).
- Lokale rescore-testtool: `rescore_streamlit_app.py` (Streamlit, `localhost:8505`)
  — hier kan een experimentele aanpassing eerst risicovrij getest worden.
- API-sleutels (Lusha/Anthropic/Serper/Firecrawl): `.streamlit\secrets.toml` in
  dezelfde repo.
