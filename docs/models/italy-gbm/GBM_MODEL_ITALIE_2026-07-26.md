# Italië — nieuw GBM-scoremodel: van factuurlijst naar live score

**Datum:** 2026-07-26. Vervolg op `MODEL_AUDIT_EN_NIEUW_MODEL.md` (25-07),
`WAAROM_HET_MODEL_BRAK_2026-07-26.md` en `SCORING_HERONTWERP_2026-07-26.md`
(beide 26-07, zelfde dag maar eerdere sessie). Dit document beschrijft het
vervolg: een nieuw, gevalideerd model gebouwd op de factuurlijst als label,
en de daadwerkelijke deploy naar de live Company Hub-app.

Alle cijfers hieronder zijn zelf doorgemeten op de brondata (niet overgenomen).
Scripts staan genoemd per sectie zodat dit reproduceerbaar is.

---

## 1. Waar we vandaan kwamen

Kort, voor context (volledige onderbouwing in de drie eerdere documenten):

- Het productiemodel (`commercial_fit_score_app`) werkt niet: AUC 0,604 tegen
  0,643 voor alleen bedrijfsgrootte. Oorzaak: een kapotte vertaallaag tussen
  de v2-signalenpijplijn en de v1-gewichten (schaal ingekrompen van 0-3 naar
  0-2, twee signalen op nul), plus een oorspronkelijke ontwikkelset met een
  ongeldige controlegroep (Spaanse prospects vs. internationale klanten).
- De factuurlijst (`2026-07-06_invoice_list (2).xlsx`) is een veel beter label
  dan HubSpot `lifecyclestage`: 483 Italiaanse klanten tegen 326 in HubSpot,
  mét bedrag en datum, geen domein.
- Client-status in de Lovable-app is deze sessie al omgezet van HubSpot-sync
  naar factuur-sync (zie de Company Hub-repo, commit `0cf8115`) — 168
  bedrijven wereldwijd gematcht, 152 HubSpot-only-markeringen teruggedraaid.

## 2. Deze sessie: van "welke bedrijven zijn klant" naar "welk model werkt"

### 2.1 Facturen aan domeinen koppelen (stap 1 uit `SCORING_HERONTWERP`)

Nieuwe matching, Italië-specifiek, tegen het volledige master-bestand
(`Italy_Companies_Lusha_vs_Chamber_of_Commerce_Master.xlsx`, 27.573 rijen,
dus inclusief de geparkeerde KvK-bedrijven die nog niet live staan) —
uitsluitend **exacte** naammatch, geen fuzzy/token-overlap (zie 2.2 voor
waarom).

**Resultaat:** 269 van 499 Italiaanse factuurbedrijven (54%) hard gekoppeld
aan een domein. Weggeschreven als
`italy_invoice_customers_with_domain_2026-07-26.xlsx`.

| bron | n klanten | omzet | gem./klant |
|---|---:|---:|---:|
| Chamber of Commerce (alleen) | 71 | €3,11M | **€43.749** |
| Beide bronnen | 133 | €2,40M | €18.013 |
| Lusha (alleen) | 65 | €1,24M | €19.011 |

Bevestigt onafhankelijk (andere methode dan `SCORING_HERONTWERP` sectie 4) de
kernbevinding: Chamber-of-Commerce-klanten zijn gemiddeld 2,3-2,4x meer waard.

### 2.2 Matching-bug gevonden en verworpen (methodenotitie)

Een eerste poging gebruikte token-overlap als fallback voor niet-exacte
matches. Twee fouten kwamen aan het licht:

1. Het master-bestand gebruikt `"Merknaam | Land"`-notatie (bv.
   `"Veolia | Italia"`). Blind splitsen op `|` maakte van "Italia" een losse
   indexsleutel, die vervolgens willekeurig elk Italiaans bedrijf matchte
   (bv. "Air Liquide Italia" en "Bosco Italia" kregen allebei `veolia.it`).
2. Dotted acroniemen (`"A.P.A. S.P.A."`) werden door de normalisatie-volgorde
   (punten eerst naar spaties, dán rechtsvorm-strip) verbrokkeld tot losse
   letter-tokens, die daarna via token-overlap op elkaar matchten.

Beide zijn *gefixt* (periodes verwijderen i.p.v. naar spatie, geo-stopwoorden
uitsluiten, minimum tokenlengte), maar zelfs ná de fix bleven er losse
generieke-domein-verzamelaars (`4uitalia.it` matchte 29 verschillende
bedrijven). **Conclusie: token-overlap matching is te onbetrouwbaar op
Italiaanse bedrijfsnamen** (te veel gedeelde generieke woorden: "italia",
"group", "servizi", "management"). De uiteindelijke resultaten in 2.1 gebruiken
daarom **alleen exacte match** — minder dekking (54% i.p.v. de eerdere
gebrekkige ~76%), maar aantoonbaar betrouwbaar (steekproefsgewijs 100%
correcte matches, geen enkele valse trigger meer).

### 2.3 Welke signalen overleven een eerlijke multivariate toets

Dataset: 23.605 bedrijven (live GCS `italy/current/`, kanaal Lusha +
Chamber of Commerce + Both), gelabeld met de 269 gematchte factuurklanten
(als domein aanwezig in beide bestanden — 267 daadwerkelijk gevonden in de
live bucket). Model: `HistGradientBoostingClassifier` (scikit-learn), 5-voudig
gestratificeerd kruisvalideren, AUC als maatstaf.

**Los getest (univariaat):**

| signaal | AUC (univariaat) |
|---|---:|
| foreign_hq (bool) | 0,558 |
| grootte (ordinaal) | 0,565 |
| omzet (log, sterk KvK-schaars) | 0,667 (n=4.860) |
| kanaal (Lusha/Chamber/Both) | 0,608-0,609 |
| de 5 oude AI-signalen samen | 0,630 |

**Collineariteits-fout gevonden en gecorrigeerd:** `sig_foreign_hq_score`
(0-3, AI-signaal) en de kale `foreign_hq`-boolean zijn vrijwel dezelfde
informatie. Met beide in het model kreeg de kale boolean een absurd lage
permutation importance (0,007) — een klassiek collineariteits-artefact, niet
een teken dat foreign_hq niet telt. Na het weglaten van de dubbele/afgeleide
velden (`sig_foreign_hq_score`, en de losse "heeft-data"-vlaggen die zelf 0
importance gaven) steeg de importance van `foreign_hq` naar 0,053 — een
eerlijk beeld.

**Nog belangrijker: permutation importance op het volledige model is
misleidend voor zwakke features.** De 4 overgebleven AI-signalen
(`intl_footprint`, `explicit_lnd`, `lnd_onboarding`, `employer_branding`)
scoorden nog steeds gematigd op permutation importance (0,04-0,06), maar toen
ze **één voor één aan het basismodel werden toegevoegd** (de eerlijke,
out-of-fold test), daalde de AUC voor 3 van de 4:

| toegevoegd aan basis (AUC 0,679) | AUC | verschil |
|---|---:|---:|
| + sig_intl_footprint | 0,669 | −0,009 |
| + sig_explicit_lnd | 0,665 | −0,013 |
| + sig_lnd_onboarding | 0,673 | −0,006 |
| + sig_employer_branding | 0,679 | +0,001 |

**Geen enkele helpt — drie maken het zelfs slechter.** Permutation importance
mat hier alleen wat het model al gebruikte (in-sample), niet of het model er
beter van werd (out-of-sample). Dit is een sterkere, eerlijker toets dan de
audit van 25-07 kon uitvoeren en bevestigt die conclusie nog harder.

**Sector (hoofdcategorie) getest op dezelfde manier:**

| toegevoegd aan basis | AUC | verschil |
|---|---:|---:|
| + main_industry (16 cat., 77,6% gevuld) | **0,695** | **+0,016 (houdt stand)** |
| + sub_industry (98 cat., 37,5% gevuld) | 0,672 | −0,007 (helpt niet) |
| + beide | 0,665 | −0,014 |

Sector telt dus mee — maar alleen de **grove** hoofdsector, niet de
fijnmazige subsector (te schaars gevuld, te veel categorieën voor 267
positieven → overfitting, dezelfde valkuil als de AI-signalen).

### 2.4 Gevalideerde feature-set (definitief)

| feature | bron | dekking |
|---|---|---:|
| grootte (ordinaal, "onbekend" voor KvK-only) | live bucket `size_category_app` | 100% (met expliciete "onbekend"-klasse) |
| foreign_hq (boolean) | live bucket `foreign_hq_detected_for_export` | 100% |
| kanaal (Lusha / Chamber / Both) | master-bestand `Source` | 100% |
| omzet (log, ontbrekend toegestaan) | master-bestand `Lusha Revenue` | ~21% |
| hoofdsector (16 categorieën) | master-bestand `Lusha Main Industry` | 77,6% |

**Model: `HistGradientBoostingClassifier`, out-of-fold AUC 0,695, lift@top10%
3,3x.** Ontbrekende waarden worden niet geïmputeerd (geen gemiddelde, geen
propensity matching) maar door het model zelf per boom-splitsing afgehandeld:
bij elke splitsing kiest het algoritme, op basis van wat op de trainingsdata
het minste fout scoort, in welke richting de missende bedrijven het beste
passen — een geleerde, contextafhankelijke regel die alle andere features al
meeneemt (bv. "KvK-bedrijf zonder omzetdata → rechts"). Dat is dichter bij
model-based imputation dan bij simpele gemiddelde-invulling.

### 2.5 Score-kalibratie: percentiel, niet ruwe kans

De ruwe modelkans (`P(klant)`) is sterk scheef: 71% van de bedrijven onder 1%,
lange staart tot 33,7%. Vertaald naar **1-10 op percentielrang** (niet op de
kansschaal), zodat elke score-band automatisch ~10% van de bedrijven bevat en
de conversie zichtbaar oploopt:

| score-band | n | klanten | conversie |
|---|---:|---:|---:|
| 1-2 | 2.491 | 10 | 0,40% |
| 2-3 | 2.632 | 9 | 0,34% |
| 3-4 | 2.460 | 17 | 0,69% |
| 4-5 | 2.795 | 23 | 0,82% |
| 5-6 | 2.353 | 20 | 0,85% |
| 6-7 | 2.872 | 23 | 0,80% |
| 7-8 | 2.624 | 27 | 1,03% |
| 8-9 | 2.623 | 37 | 1,41% |
| **9-10** | 2.755 | 101 | **3,67%** |

Top-10% haalt bijna 4x de basisconversie (1,13%) — het gros van het
onderscheidend vermogen zit in de bovenste band.

**Oude vs. nieuwe score: nauwelijks verband.** Pearson r = 0,169, Spearman ρ =
0,121 tussen `commercial_fit_score_app_OLD` en de nieuwe 1-10-score (23.605
bedrijven). Sprekende voorbeelden: KPMG Fides Servizi (oud 4,16 → nieuw 10,0),
Lafert Group (oud 8,69 → nieuw 10,0) — twee heel verschillende oude scores,
allebei nieuw op de max. Jacobacci & Partners (oud 5,0 → nieuw 3,2) en Intesa
Sanpaolo (oud 6,06 → nieuw 1,1) zakken juist weg. Consistent met de eerdere
bevinding dat het oude model binnen elke groottecategorie AUC 0,50 had.

### 2.6 Kwadrant-analyse: grootte × foreign HQ

Ter validatie/uitleg van waarom deze twee features samen sterker zijn dan
apart (23.605 bedrijven, klein = <500 fte of onbekend, groot = ≥500 fte):

| | Domestic HQ | Foreign HQ |
|---|---:|---:|
| **Klein** | 0,79% conversie (n=17.278) | 2,03% conversie (n=2.462) |
| **Groot** | 2,06% conversie (n=3.111) | 2,25% conversie (n=754) |

Foreign HQ tilt een klein bedrijf bijna naar het niveau van een groot bedrijf
— de twee zijn deels twee wegen naar hetzelfde effect, geen zuivere optelsom.
% foreign HQ stijgt ook zelf monotoon met grootte: 9,5% (51-200 fte) tot
34,1% (10.001+ fte).

---

## 3. Deploy naar de live Company Hub-app

### 3.1 Aanpak

Bestaand script `rescore_from_gcs.py` (in deze repo) biedt al een veilig
patroon: download `<land>/current/` → herbereken → schrijf naar een NIEUWE
`runs/<run_folder>/` map → pas als losse, expliciete stap promoten naar
`current/`. Dat patroon is overgenomen in een nieuw script
**`gbm_rescore_italy.py`** (deze repo), dat i.p.v. `score_company()` opnieuw
te draaien, de vooraf berekende GBM-score (uit
`italy_new_gbm_scores_2026-07-26.xlsx`) per `company_id` inmerget.

**Veldbeleid (geen dubbele scores in de bucket):**
- `commercial_fit_score_app` / `commercial_tier_app` (de velden die de
  Lovable-frontend leest) worden **overschreven** met de nieuwe GBM-score/tier.
- De oorspronkelijke waarden worden **eenmalig** weggeschreven naar
  `commercial_fit_score_app_legacy` / `commercial_tier_app_legacy` (nooit
  overschreven op een herhaalde run, zodat legacy altijd het originele v1-model
  blijft tonen, niet een tussenliggende GBM-run).
- Nieuw veld `commercial_fit_score_model_version: "gbm_v1_2026-07-26"` voor
  traceerbaarheid.
- Nieuwe tier-grenzen (gekozen op basis van de conversie-tabel in 2.5, zelfde
  emoji-vocabulaire als het oude model): Hot ≥9, Warm ≥7, Cool ≥4, Pass <4.

### 3.2 Uitgevoerd

<!-- INVUL_NA_DRYRUN: tier-verschuiving, aantal gematchte/niet-gematchte bedrijven, upload-resultaat -->

### 3.3 Nog niet gedaan: promoten naar `current/`

`gbm_rescore_italy.py` schrijft alleen naar `runs/<run_folder>/`. Live zetten
is een aparte, bewuste stap via `rescore_from_gcs.promote_run_to_current()` —
dezelfde tweetrapsraket als het bestaande rescore-systeem, zodat een
verkeerde run altijd terug te draaien is zonder dat de live app ooit een
half-geschreven run ziet.

---

## 4. Andere landen — bewust NIET nu opgelost

Expliciet uitgesteld op verzoek. Vastgelegd zodat het niet vergeten wordt:

1. **Geen Lusha/Chamber-of-Commerce-onderscheid.** Duitsland, Zwitserland,
   Nederland, Uruguay (en vermoedelijk de overige landen) draaien 100% op
   Lusha — geen handelsregisterbron, dus geen "kanaal"-feature zoals in
   Italië. Het `source`/`channels`-veld dat hier zo veel deed, bestaat daar
   niet.
2. **De gedownloade datasets per land zijn deelverzamelingen, niet
   vergelijkbaar met Italië's volledige master.** Sommige landen zijn alleen
   verrijkt voor bedrijven met een foreign HQ, of een andere selectiefilter —
   dat verstoort elke poging om dezelfde featureset 1-op-1 te hergebruiken:
   een conversieratio of feature-verdeling gemeten op zo'n gefilterde
   deelverzameling is niet zonder meer vergelijkbaar met Italië's cijfers.
3. **De factuurlijst dekt in principe alle landen** (Nederland 86 bedrijven
   €3,31M, Duitsland 102/€1,71M, België 76/€1,39M, Zwitserland 59/€1,11M —
   zie `SCORING_HERONTWERP` sectie 8) — dus het label-probleem is daar
   oplosbaar. Het feature-probleem (punt 1+2) is dat niet zomaar.

**Volgorde-advies voor als dit weer wordt opgepakt:** eerst per land meten
hoe compleet/representatief de gedownloade dataset is (net zoals hier voor
Italië de KvK/Lusha-verhouding is uitgezocht) vóórdat er een model op wordt
gebouwd — nieuwe aannames verifiëren, niet de Italië-aanpak blind kopiëren.

---

## 5. Reproduceerbaarheid

| bestand | inhoud |
|---|---|
| `Myngle/2026-07-06_invoice_list (2).xlsx` | bron: facturen 2012-2026 |
| `Countries/Italy/Italy_Companies_Lusha_vs_Chamber_of_Commerce_Master.xlsx` | 27.573 bedrijven, bron-toewijzing, Lusha-velden |
| `Countries/Italy/italy_invoice_customers_with_domain_2026-07-26.xlsx` | 269 gematchte klanten, domein + Lusha/Chamber-kenmerken |
| `Countries/Italy/italy_new_gbm_scores_2026-07-26.xlsx` | alle 23.605 bedrijven: oude score, nieuwe score, ruwe kans, features |
| GCS `italy/current/companies.list.json` + `company-details-0NN.json` (50 buckets) | live populatie + `scoring_inputs.signals` |
| `gert66/myngle/rescore_from_gcs.py` | bestaande veilige GCS rescore/promote-plumbing |
| `gert66/myngle/gbm_rescore_italy.py` | nieuw: merget de GBM-score in een nieuwe run-folder |

Scripts voor de analyse zelf stonden in de scratchpad van deze sessie
(niet in de repo) — bij herhaling verplaatsen naar een `scripts/`-map als
deze aanpak blijft staan.
