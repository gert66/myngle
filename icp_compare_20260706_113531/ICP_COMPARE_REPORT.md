# ICP-signalen oud vs. nieuw — vergelijking op Italiaanse bedrijven

**Datum:** 2026-07-06 · **Branch:** work · **Scope:** de vijf niet-HQ ICP-signalen
(HQ/foreign-ownership expliciet buiten scope) · **Steekproef:** **36 Italiaanse bedrijven**
uit `current/`.

Dit is een *analyse*-rapport. Er is niets aan de productie-scoring gewijzigd. De map
`current/` is strikt read-only behandeld (`git status`: alleen untracked `??`, geen wijziging).

> **Noot over steekproefgrootte.** De opdracht vroeg een kostenbeheerste steekproef van 5-6
> bedrijven. Op expliciet verzoek van de gebruiker ("ga door totdat het API-budget bereikt is",
> Serper+Anthropic-plafond ~€10) is de steekproef verbreed naar 36 bedrijven om de kernvraag —
> *hoe vaak treedt mechanisme (b) op* — statistisch hard te maken. De werkelijke kosten bleven
> ruim onder budget (zie §2). De oorspronkelijke 6 (batch-1) dienen als kwalitatieve
> illustratie; alle 36 voeden de kwantitatieve diagnose.

---

## 0. Preflight

| Controle | Uitkomst |
|---|---|
| `SERPER_API_KEY` | **gevonden** (via `load_api_keys(--secrets-file)`) |
| `ANTHROPIC_API_KEY` | **gevonden** |
| `FIRECRAWL_API_KEY` | gevonden (optioneel) |
| Baseline-data `current/` | `companies.list.json` (14.101 records) + `details/companies.detail.000–056.json` |
| `current/` read-only | bevestigd ongewijzigd |

Keys zijn nooit geprint, gelogd of weggeschreven.

**Datavondst.** `current/` is niet het rúwe `enrich_clients_claude.py`-formaat, maar de
**app-export** ervan (`export_report.json`: `compact_rich_mode`, `evidence_audit_mode`,
`dataset_mode = italy_foreign_hq_21_url_hq_recalc`). Het letterlijke veld `icp_lead_score`
(0–10) uit `ICP_FIELDS` komt in deze export **niet** voor. Wat wél overleeft:

- `commercial_fit_score` (0–10) — de enige overgebleven overall-score; gebruikt als 0–10-proxy
  voor "oude sterke lead" bij de selectie (drempel > 6.5).
- `visible_icp_signal_scores` — de **oude holistische per-signaal-oordelen van Claude**
  (0–3 per signaal) mét evidence-tekst. Dit is de eigenlijke oude ICP-grondwaarheid die per
  signaal vergeleken wordt.

---

## 1. Gekozen bedrijven

Reproduceerbaar getrokken (`random.seed(42)` voor batch-1, `seed(7)` voor batch-2) uit alle
Italiaanse bedrijven met `commercial_fit_score > 6.5`, een echt `.it`-domein (om domein/land-
mismatch te vermijden — les uit het ALDI-geval) en ≥3 sterke oude non-HQ-signalen (score ≥2).
Land én domein **ongewijzigd** uit de oude JSON meegegeven.

36 bedrijven, `commercial_fit_score` 6.51–9.78. Volledige lijst in **Bijlage A**. Voorbeelden:
SIDEL, NORDCOM, COMET, SOLGAR, GEBERIT, KONICA MINOLTA, FORD ITALIA, MICHELIN, SAINT-GOBAIN,
ART COSMETICS, FB BALZANELLI, INTERNATIONAL SCHOOL OF EUROPE. Veel zijn buitenlands-eigendom
Italiaanse dochters — logisch bij "hot leads", en HQ is buiten scope.

---

## 2. Nieuwe verse verrijking

Commando (nieuwe werkmap, nooit naar `current/`):

```
lead_prioritizer_batch_cli.py --mode full --rich-icp-context \
  --input <batch>.xlsx --company-column company_name \
  --domain-column domain --input-country-column country \
  --default-country Italy --secrets-file .streamlit/secrets.toml
```

Bewust **zonder** `--ai-signal-scoring` en `--legacy-enrichment-mode`: dat zijn juist de te
*voorstellen* tuning-knoppen; ze aanzetten zou het default-gedrag maskeren dat we meten.
`--rich-icp-context` is aan om de AI-laag (`icp_buying_signals` → `icp_context`) te vullen,
nodig voor de (b)-diagnose.

- **Resultaat:** batch-1 6/6 + batch-2 30/30 = **36/36 succesvol, 0 errors**.
- **API-verbruik (Serper + Anthropic):** per bedrijf 6 non-HQ signaal-Serper-queries (gemeten)
  + HQ- en icp_context-queries ≈ ~11–15 Serper; 2 Anthropic-calls (**Haiku 4.5**,
  `claude-haiku-4-5-20251001`, $1/$5 per M tok). Totaal 36 bedrijven ≈ **~400–540 Serper +
  ~72 Anthropic ≈ $1–2.5**. Ruim onder het €10-plafond.

---

## 3. Headline — kleurt de nieuwe pipeline minder positief?

Klassen: **oud** POS = score ≥2 (0–3-schaal); **nieuw** POS = score 2 (0–2-schaal); weak=1; none=0.
Matrix = 36 bedrijven × 5 signalen = **180 cellen**.

| Maat | Oud | Nieuw |
|---|---|---|
| **Positieve cellen (van 180)** | **131 (73%)** | **106 (59%)** |
| Divergenties omlaag (oud POS → nieuw niet) | — | **44** |
| Divergenties omhoog (oud niet → nieuw POS) | — | 19 |

**Ja — op schaal kleurt de nieuwe pipeline duidelijk minder positief** (131 → 106, −19%
relatief), anders dan de 6-bedrijven-steekproef suggereerde (die was toevallig gebalanceerd).
De daling is sterk **signaal-specifiek**:

| Signaal (interne naam) | Oud POS | Nieuw POS | Δ |
|---|---|---|---|
| explicit L&D (`icp_keyword_match`) | 32/36 | **17/36** | **−15** |
| international business context (`international_profile`) | 34/36 | 25/36 | −9 |
| employer branding (`employer_branding`) | 10/36 | 4/36 | −6 |
| L&D / onboarding (`onboarding_training_need`) | 29/36 | 30/36 | +1 |
| possible onboarding need (`company_size_complexity`) | 26/36 | 30/36 | +4 |

De onderkleuring zit vrijwel volledig in **`icp_keyword_match`** (expliciete L&D) en
`international_profile`. `company_size_complexity` en `onboarding_training_need` doen het in de
nieuwe pipeline juist *beter* (ruime, meertalige keywordsets). Dit verklaart de oorspronkelijke
waarneming en lokaliseert het probleem scherp.

**Mapping oud → nieuw** (met caveats):

| Nieuw signaal | Primair oud label | Mapping-kwaliteit |
|---|---|---|
| `international_profile` | International business context | 1-op-1 (+ Multicultural / Intercultural verwant) |
| `company_size_complexity` | Possible onboarding need | **semantische drift**: nieuw meet *grootte* (employees/sedi), oud label meet *onboarding* |
| `onboarding_training_need` | Onboarding or employee development signal | 1-op-1 (+ Learning and development verwant) |
| `employer_branding` | Employer branding signal | 1-op-1 |
| `icp_keyword_match` | Learning and development signal | overlapt met `onboarding_training_need` |

---

## 4. Kern-diagnose — (a) écht ontbrekend vs (b) opgehaald-maar-onderbenut

Uitsplitsing van de **44 ▼-cellen** (oud positief → nieuw niet-positief), met de nieuwe klasse
en of er een **eigen-domein, niet-hosted evidence-URL** aanwezig is (URL-grounding):

| | eigen-domein URL: **ja** | eigen-domein URL: **nee** | totaal |
|---|---|---|---|
| **nieuw = weak (1)** | **21** | 3 | 24 |
| **nieuw = none (0)** | 11 | 9 | 20 |

**Interpretatie:**
- **(b) — bewijs opgehaald, maar keyword-tel promoveert niet:** de 24 `weak`-cellen zijn het
  duidelijkst (evidence bestaat, kreeg 1 keyword-hit i.p.v. ≥2). Tel je de 11 `none`-cellen mét
  eigen-domein-URL erbij (evidence opgehaald, 0 keyword-hits), dan is **32/44 = 73%** een
  bovengrens voor (b)/herwinbaar-met-grounding. De **21 weak+grounded** cellen (48%) zijn de
  sterkste, schoonste (b)-gevallen.
- **(a) — écht ontbrekend/dun:** **9/44 (20%)** cellen zijn `none` zónder eigen-domein-URL —
  hier haalde de pipeline geen bruikbaar bedrijfsbewijs op.

**Herwinbaar-met-grounding, per signaal:** `icp_keyword_match` 14 · `international_profile` 10 ·
`onboarding_training_need` 4 · `company_size_complexity` 4 · `employer_branding` **0**.

### Belangrijke caveat: de 73% is een bovengrens (entiteitsvervuiling)

Kwalitatieve steekproef van herwinbare (weak+grounded) cellen:

| Bedrijf · signaal | Oordeel | Bewijs |
|---|---|---|
| **ART COSMETICS** · icp_keyword_match (3→1) | ✅ schoon (b) | eigen-domein `artcosmetics.it/working-at-art-cosmetics-how-our-employees-feel/`; AI bevestigt "employee collaboration, growth, and learning culture… structured training". Keyword-laag: 1 hit (`employees`). |
| **FB BALZANELLI** · icp_keyword_match (2→1) | ✅ schoon (b) | eigen-domein `fb-balzanelli.it/careers/`; AI bevestigt "career development… customer support". Keyword: 1 hit (`support`). |
| **VILLA SILVANA** · icp_keyword_match (2→1) | ⚠️ vals-herwinbaar | AI-`icp_context` zegt zélf: evidence is "a mix of **unrelated organizations** (Samsung C&T, Korian SA, Kion Group)". Domein-root "korian" matchte, maar het bewijs is entiteitsvervuild. |

→ Ongeveer **1 op 3** in deze mini-steekproef is een *valse* herwinbare door
entiteitsvervuiling/aggregator-domeinen (vgl. batch-1: SIDEL `sidelitalia.it` → bewijs op
`sidel.com`). **Realistische schone-herwinbaarheid ≈ 50–60%**, niet 73%.

### Employer branding = het vals-positief-risico

Alle **9 ▼-cellen van `employer_branding` hebben 0 eigen-domein-URL** — ze rusten volledig op
Glassdoor/Instagram/social (hosted platforms). Een botte drempelverlaging zou hier 9 directe
**vals-positieven** creëren. Dit signaal moet buiten elke drempel-versoepeling blijven.

### Antwoord op de kernvraag

Mechanisme (b) is **reëel en dominant**: in **~24–32 van de 44** onderkleuringen is het bewijs
wél opgehaald (grotendeels met eigen-domein-URL), maar vertaalt de ≥2-keyword-drempel het niet
naar een positieve tier — geconcentreerd op `icp_keyword_match`. Na de gewenste URL-validatie-
eis blijft naar schatting **~50–60%** schoon herwinbaar; entiteitsvervuiling en enkele
overdreven oude oordelen (bv. INTL SCHOOL, waar de oude samenvatting zélf "no evidence of
corporate employee development" zegt) verklaren de rest. De nieuwe pipeline is dus
**conservatiever en beter onderbouwd**, ten koste van echte L&D-signalen op eigen bedrijfssites.

---

## 5. Tuning-voorstellen (voorstellen — NIET doorgevoerd)

Geprioriteerd. Elk voorstel houdt de URL-validatie en het betere HQ-signaal intact.

### 1. (Aanbevolen) Per-signaal drempel achter een eigen-domein-poort
Voor **`icp_keyword_match`** (en secundair `international_profile`, `onboarding_training_need`):
laat **1 keyword-hit = positief** *mits* er ≥1 gevalideerde **eigen-domein, niet-hosted**
evidence-URL bij zit. **Niet** voor `employer_branding` (9/9 hosted → vals-positief). **Niet** globaal.
- **Verwacht effect:** herstelt de ~21 weak+grounded ▼-cellen, geconcentreerd waar de
  onderkleuring zit (14× icp_keyword_match, 10× international_profile).
- **Risico vals-positief:** laag, dankzij de eigen-domein-poort + bestaande external-training-guard.
  Restrisico = entiteitsvervuiling (Villa Silvana/SIDEL) → mitigeer met een domein-match-check
  tussen evidence-URL en input-domein.

### 2. (Aanbevolen) URL-gegronde AI-tier
Laat een tier promoveren wanneer de AI-laag (`icp_context`) het thema bevestigt **én** er een
gevalideerde eigen-domein-URL bij hoort. Dichtst bij het oude, gevalideerde holistische gedrag;
de AI vond aantoonbaar echte signalen (ART COSMETICS, FB BALZANELLI, Solgar). Knop bestaat deels
(`--ai-signal-scoring`).
- **Verwacht effect:** vangt (b)-gevallen die de keyword-set mist maar de AI wél ziet.
- **Risico:** de ongegronde AI-claim (bv. CARTIERE) én entiteitsvervuiling (Villa Silvana) —
  **beide weggevangen door de URL-grounding-poort + domein-match-check**. Zonder die poort
  reïntroduceert het de oude zwakte.

### 3. Gelokaliseerde queries + meertalige keywords (`gl=it`, `hl=it`)
`international_profile` verloor 9 cellen deels doordat Engelse queries alleen de Italiaanse
homepage-snippet ophaalden (bv. CARTIERE). Gelokaliseerde queries + IT-keywords
(`mercati esteri`, `multilingua`, `sedi`) **meten beter** i.p.v. soepeler. Keywordset is al
`v2-multilingual`; de query-`gl`/`hl` is de ontbrekende schakel. Dit dicht type-(a)-gaten met
echt bewijs.
- **Risico:** laag; kost query-tuning, geen vals-positieven.

### 4. (Afgeraden) Globale drempel ≥2 → ≥1
Bot: promoveert alle 40 weaks inclusief de ~13 Glassdoor-`employer_branding`-cellen → directe
vals-positieven en verlies van URL-discipline. Niet doen.

### Aanbevolen volgorde
**1 → 2 → 3** (afgeraden: 4). Begin met de per-signaal-drempel achter de eigen-domein-poort
(#1, laag risico, direct effect op de aangetoonde ~21 (b)-cellen), voeg de URL-gegronde AI-tier
toe (#2, dichtst bij oud-gevalideerd), en verbeter parallel de query-lokalisatie (#3) om
type-(a)-gaten met echt bewijs te dichten. Voeg bij #1/#2 een **domein-match-check** toe tegen
entiteitsvervuiling.

---

## 6. Conclusie

- Op 36 bedrijven is de onderkleuring **bevestigd**: 131 → 106 positieve cellen (−19%),
  vrijwel volledig geconcentreerd op **`icp_keyword_match`** (32→17) en `international_profile`
  (34→25). `company_size_complexity`/`onboarding_training_need` doen het juist beter.
- Mechanisme (b) — opgehaald bewijs dat de keyword-tel niet naar positief vertaalt — is
  **dominant**: 24 van 44 ▼-cellen zijn `weak`, 32/44 (73%, bovengrens) hebben eigen-domein-
  grounding. Na correctie voor entiteitsvervuiling is **~50–60%** schoon herwinbaar.
- De grootste winst zit in een **URL-gegronde, per-signaal aanpak** (#1 + #2), niet in botte
  drempelverlaging (#4), die juist de Glassdoor-`employer_branding`-weaks (9/9 hosted) als
  vals-positief zou promoveren.
- Voeg een domein-match-check toe: enkele "herwinbare" cellen zijn entiteitsvervuild
  (Villa Silvana→Korian-mix, SIDEL→sidel.com) en niet elk oud-positief verdient herstel
  (INTL SCHOOL scoorde L&D=2 terwijl de oude samenvatting zélf geen intern L&D-bewijs vond).
- Voer niets door in productie-scoring; dit zijn voorstellen ter beslissing.

---

## Bijlage A — alle 36 bedrijven (gesorteerd op `commercial_fit_score`)

| cfs | Bedrijf | Domein |
|---|---|---|
| 9.78 | VILLA SILVANA - S.P.A. | korian.it |
| 9.63 | BNP PARIBAS REAL ESTATE ADVISORY ITALY S.P. | realestate.bnpparibas.it |
| 9.63 | GEBERIT MARKETING E DISTRIBUZIONE S.A. | geberit.it |
| 9.63 | HSBC CONTINENTAL EUROPE | hsbc.it |
| 9.63 | HYDAC S.P.A | it.hydac.it |
| 9.40 | SIDEL S.P.A. | sidelitalia.it |
| 9.40 | ELECTRIC S.P.A. | electricspa.it |
| 9.40 | CENTRO EUROPEO DI GESTIONE E ORGANIZZAZIONE | app.fatturatoitalia.it |
| 9.40 | KONICA MINOLTA BUSINESS SOLUTIONS ITALIA S. | konicaminolta.it |
| 9.22 | SOPRA STERIA GROUP S.P.A. | soprasteria.it |
| 9.09 | CA.DI.GROUP S.P.A. | cadigroup.it |
| 9.02 | NORDCOM S.P.A. | nord-com.it |
| 9.01 | C.M.R. GROUP S.P.A. | cmr.it |
| 8.98 | JD SPORTS FASHION S.R.L. | jdsports.it |
| 8.90 | COMET SPA | comet.it |
| 8.90 | O.M.S. SALERI S.P.A. | oms-saleri.it |
| 8.88 | IBF S.P.A. | ibfgroup.it |
| 8.82 | HEIDELBERG MATERIALS ITALIA CALCESTRUZZI S. | heidelbergmaterials.it |
| 8.73 | CREDIT AGRICOLE ASSICURAZIONI S.P.A. | ca-assicurazioni.it |
| 8.68 | VECTOR - S.P.A. | vectorspa.it |
| 8.65 | DM DROGERIE MARKT SRL | dm-drogeriemarkt.it |
| 8.51 | GAMELIFE S.R.L. | gamelife.it |
| 8.34 | FORD ITALIA S.P.A. | ford.it |
| 8.34 | MICHAEL PAGE INTERNATIONAL ITALIA S.R.L. | michaelpage.it |
| 8.30 | FB BALZANELLI S.P.A. | fb-balzanelli.it |
| 8.25 | CASSA PADANA BANCA DI CREDITO COOPERATIVO | cassapadana.it |
| 8.24 | STELLANTIS FINANCIAL SERVICES ITALIA S.P.A. | stellantis-financial-services.it |
| 8.09 | SOLGAR ITALIA MULTINUTRIENT S.P.A. | solgar.it |
| 8.09 | SOCIETA' PER AZIONI*MICHELIN ITALIANA*S.A. | michelin.it |
| 7.49 | SAINT-GOBAIN SEKURIT ITALIA S.R.L. | saint-gobain.it |
| 7.43 | CARTIERE MODESTO CARDELLA SPA | cartierecardella.it |
| 7.27 | INTERNATIONAL SCHOOL OF EUROPE S.P.A. | internationalschoolofeurope.it |
| 7.22 | A.M.R.A. S.P.A. | amra-chauvin-arnoux.it |
| 7.11 | ART COSMETICS S.R.L. | artcosmetics.it |
| 6.67 | 4U ITALIA S.R.L. | 4uitalia.it |
| 6.51 | COMMERZBANK AG | commerzbank.it |

*Databijlagen in deze werkmap:* `icp_compare_input*.xlsx`, `icp_compare_output*.xlsx`,
`combined_matrix.json`, `comparison.json`, `new_signals.json`.
