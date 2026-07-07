# Drie-staps zichtbaarheid van ICP-driver-signalen

Implementatie bovenop `e49d0b7` (per-signaal drempel + eigen-domein-poort). Lost twee dingen op
die de status-check aan het licht bracht: (a) een generic-evidence-check die een gescoord signaal
alsnog naar "Rejected" duwde, en (b) subthreshold-signalen die helemaal geen driver-kaart kregen
("absent" in de app). Branch: `work`.

## Stap 0 — bevestigde feiten (geciteerd)

- **(a)** De generic-evidence-check die ongeacht de score op "Rejected" kan zetten:
  `classify_curated_evidence`, `export_lead_prioritizer_to_lovable_json.py:976–1021`; het
  "Rejected"-resultaat werd gezet in `build_fixed_commercial_fit_drivers:1593–1598`.
- **(b)** Waar een subthreshold-signaal volledig werd weggelaten: de Italië-driverbouw
  `build_commercial_fit_drivers`, de `continue` op `1226–1227` (bucketed signalen zonder score>0
  of zonder schone evidence). In het non-Italië-pad werden ze niet weggelaten maar als
  "Rejected"/"Not evidenced" getoond.
- **Stap 0.2:** `extract_non_hq_signals` (v3) geeft wél een intern "weak"-oordeel
  (`signal_score = 1.0`) per signaal met 1 hit, en dat komt **mét `evidence_urls` de exporter
  binnen** via `build_visible_icp_signal_scores:502–530` (score op 517, evidence_urls op 526). Het
  ging pas verloren in de twee driver-builders. → **Geen wijziging aan
  `lead_non_hq_signal_extractor.py` nodig.**
- **FIT-score-vraag:** `commercial_fit_score` komt uit rij-kolommen (`export_..._lovable_json.py`
  regel 1940–1970), berekend upstream in de enrichment (`commercial_fit_scoring.py`), en wordt
  **niet** uit de driverkaarten opgebouwd. Driver-zichtbaarheid wijzigen raakt de FIT-score dus
  **niet** (buiten scope, en aantoonbaar losstaand).

## Wat aangepast is

Alleen `export_lead_prioritizer_to_lovable_json.py` (+ tests). Twee **onafhankelijke assen** per
driverkaart:

1. **Betrouwbaarheid (`strength`)** — afgeleid van de v3-score + evidence-kwaliteit:
   - **Strong** — score ≥ 2 met schone, bedrijfsspecifieke evidence.
   - **Moderate** — score ≥ 1 met schone evidence (de generic-check verbergt een gescoord signaal
     niet meer → fix (a)).
   - **Weak** — score > 0 met ≥ 1 bruikbare `evidence_url` maar geen schone evidence-tekst; getoond
     met de link + "Weak"-badge (fix (b)). Uniform voor alle vijf non-HQ-signalen, inclusief
     `employer_branding`; de link mag een hosted platform zijn (Glassdoor/LinkedIn/…).
   - **verborgen** — score ≤ 0, of geen enkele bruikbare `evidence_url` (blijft "Not
     evidenced"/"Rejected" resp. weggelaten, zoals voorheen).
2. **Bron (`source_scope`)** — `"own_domain"` als minstens één getoonde bron op het eigen
   registreerbare domein staat, anders `"external"`. Onafhankelijk van de betrouwbaarheidsas: een
   Strong kan `external` zijn en een Weak kan `own_domain` zijn.

Config staat als één blok bovenaan de driver-builders (`_WEAK_TIER_ENABLED`,
`_DRIVER_STRONG_MIN_SCORE`, `_DRIVER_MODERATE_MIN_SCORE`, `_WEAK_VISIBLE_SIGNAL_NAMES`,
`_WEAK_TIER_NOTE`) plus de helpers `_driver_sources_and_scope` / `_apply_driver_sources`. Beide
paden (Italië `build_commercial_fit_drivers`, non-Italië `build_fixed_commercial_fit_drivers`)
gebruiken dezelfde regels.

> **Ontwerpkeuze (transparant):** "Strong" vereist naast score ≥ 2 óók schone, bedrijfsspecifieke
> evidence. Een score-2-signaal met alleen een generieke/externe snippet valt bewust naar **Weak
> (met link)** in plaats van een misleidende "Strong" zonder onderbouwing — conform de bestaande
> regressiegaranties (DORC/BauWatch: generiek bewijs nooit als positieve claim). Wil je puur
> score-gedreven tiers, dan is dat één config-aanpassing.

### Wijziging aan het "frozen" Italië-pad
`build_commercial_fit_drivers` stond als "frozen" gemarkeerd. Op expliciet verzoek van deze
opdracht is **alleen de driver-opbouw** voor de vijf non-HQ-signalen aangepast (drie-tier +
links + badges). `foreign_hq` en custom-signalen blijven ongewijzigd, en `why_relevant` /
`what_is_hot` / `cold_caller_summary` blijven byte-identiek — generiek bewijs wordt nog steeds
níét in die tekst gepromoveerd (getest).

## Testresultaat

- `test_export_lead_prioritizer_to_lovable_json.py`: **193 passed** (186 bestaand + 2 bewust
  bijgewerkt naar het nieuwe Weak-gedrag + 7 nieuw). Twee bestaande tests die het oude "verberg
  generieke externe employer-branding"-gedrag vastlegden zijn bijgewerkt: die evidence toont nu
  als **Weak [external] met link**, terwijl de kern-garantie (niet in `why_relevant`/`what_is_hot`)
  intact blijft.
- Regressiesuites `test_lead_non_hq_signal_extractor.py` + `test_lead_v2_full_pipeline.py`: mee
  gedraaid, **alle groen** (totaal 294 in de gecombineerde run).

### Voor/na — 36 Italiaanse bedrijven (Italië-pad, geen API; before via `git stash`)

| | Zichtbare non-HQ kaarten | Kaarten met bewijslink | Verdeling |
|---|---|---|---|
| **BEFORE** | 52 | **0** | Strong 20 / Moderate 13 / Weak 19 |
| **AFTER** | **146** | **146** | Weak 128 / Strong 17 / Moderate 1 |

Het Italië-pad hing voorheen **geen enkele** bewijslink aan een driver; nu heeft elke zichtbare
kaart er één, plus een own_domain/external-badge. Eerder weggelaten signalen verschijnen nu als
Weak-met-link. De daling in Strong/Moderate is `employer_branding` dat vroeger als Strong/Moderate
**zonder** link werd getoond → nu eerlijk Weak+external+link.

Voorbeeldbedrijven (na):
- **ART COSMETICS** — 4 Weak-kaarten met links (o.a. `artcosmetics.it/locations/` [own_domain],
  zoominfo [external]); voorheen 1 kaart (employer_branding zonder link).
- **FB BALZANELLI** — 4 Weak-kaarten met links (3× eigen domein, 1× Glassdoor [external]);
  voorheen 1 misleidende "Strong" employer_branding zonder link.

### Voor/na — FUJIFILM Manufacturing Europe (NL, non-Italië-pad; verse enrichment)

| Signaal | BEFORE | AFTER |
|---|---|---|
| International business context | Strong (link) | Strong [external] (link) |
| Explicit learning and development | **Rejected** (geen link) | **Weak [external]** (link) |
| L&D or onboarding needs | **Rejected** (geen link) | **Weak [external]** (link) |
| Possible onboarding need | **Rejected** (geen link) | **Weak [external]** (link) |
| Employer branding | Not evidenced | Not evidenced *(geen bewijs → terecht verborgen)* |

De drie ex-"Rejected" drivers (score ≥ 1 + bewijs, door de cleanliness-check afgekeurd) zijn nu
**Weak met link + external-badge** i.p.v. verborgen (fix (a)+(b)). `employer_branding` had geen
bewijs/URL en blijft terecht verborgen — **regressie bevestigd**: score 0 / geen URL levert geen
valse Weak-kaart op. (In deze verse run is het L&D-bewijs extern/generiek → Weak, niet Moderate;
het is nu wél zichtbaar met link, wat het doel is. Serper-resultaten variëren per run.)

## Terugdraai-instructie

Zet in `export_lead_prioritizer_to_lovable_json.py` (config-blok net vóór
`build_curated_display_signals`):

```python
_WEAK_TIER_ENABLED = False
```

Dan verdwijnt de Weak-tier: subthreshold-signalen worden weer weggelaten (Italië) resp. als
"Rejected"/"Not evidenced" getoond (non-Italië), exact als vóór deze wijziging. De
`source_scope`/`evidence_sources`-velden op Strong/Moderate-kaarten blijven dan bestaan (additief,
schaadt niets); wil je ook die weg, verwijder de `_apply_driver_sources`-aanroepen. Eén signaal
uitsluiten van de Weak-tier kan door het uit `_WEAK_VISIBLE_SIGNAL_NAMES` te halen.

## Export opnieuw draaien (nodig vóór de Lovable-integratie)

De op-schijf JSON's bevatten pas de drie-lagen-zichtbaarheid na een verse export van een
enriched workbook:

```bash
python export_lead_prioritizer_to_lovable_json.py \
  --input-xlsx <enriched_output>.xlsx \
  --output-dir <output_dir> \
  --country Italy \
  --cold-callers "Vanessa,Francesca,Lorenzo,Matteo" \
  --no-foreign-hq-only \
  --content-language Italian
```

(Voor niet-Italiaanse exports: `--country <Land>` en `--content-language English` of `Dutch`.)
De driver-kaarten staan in elk detail-record onder `ui_payload.commercial_fit_drivers[]`, met per
kaart `strength` (Strong/Moderate/Weak/…), `source_scope` (own_domain/external),
`evidence_source_url`, `evidence_source_domain` en `evidence_sources[]`. Geen deploy naar een
gedeelde omgeving — lokaal testbaar tot akkoord.
