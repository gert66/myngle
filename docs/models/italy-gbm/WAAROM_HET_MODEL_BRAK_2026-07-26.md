# Waarom de signalen in `Results1.xlsx` werkten en in productie niet

**Datum:** 2026-07-26
**Vraag:** het model is gefit op `Results1.xlsx` en haalde daar AUC 0,772. Live
haalt de eindscore 0,604 — slechter dan sorteren op bedrijfsgrootte. Waar zit
het verschil, en wat is de oorzaak?

**Kort antwoord:** er zijn twee onafhankelijke oorzaken, en ze versterken
elkaar. De ontwikkelset overschatte de signalen door hoe hij was samengesteld
(sectie 2 en 3). Daarnáást is het model in productie gevoed met **andere
signalen op een andere schaal** dan waarop het gefit is (sectie 4). Dat tweede
is geen interpretatiekwestie — het staat letterlijk in een kolom van de
productie-output.

---

## 1. Wat `Results1.xlsx` is

Tabblad `Sheet1`, 414 rijen met een bedrijfsnaam, 153 kolommen.

| | |
|---|---:|
| klanten (`cat = 1`) | 232 |
| controlegroep (`cat = 0`) | 182 |
| aandeel klanten | **56%** |

**Bevestigd: de facturen wáren het label.** Van de 232 klanten in de
ontwikkelset zijn er **230 (99%) terug te vinden in de factuurlijst**, samen
goed voor €21,1M. Van de 182 controles hebben er 8 (4%) ooit een factuur gehad.
Het label is dus in de kern juist — beter dan het HubSpot-label dat later is
gebruikt. Het probleem zit niet in wie klant was, maar in **waarmee die klanten
zijn vergeleken**.

---

## 2. Drie ontwerpkenmerken die de signalen deden oplichten

### a. De controlegroep komt uit een ander land dan de klanten

| | klanten | controles |
|---|---|---|
| Italië | 33% | 11% |
| **Spanje** | — | **65%** |
| VS | 12% | 16% |
| Duitsland | 11% | — |
| Nederland | 9% | — |
| Zwitserland | 8% | — |
| België | 7% | — |

Van de 121 Spaanse rijen zijn er maar 5 klant. De controlegroep is dus in
meerderheid een Spaanse prospectlijst; de klantgroep is een internationale mix
uit zes landen.

Elk signaal dat samenhangt met "in welk land en met welke zoekopdracht is dit
bedrijf opgehaald" wordt daarmee vanzelf voorspellend. Voor
`sig_foreign_hq_score` is dat fataal: of een bedrijf een buitenlands
hoofdkantoor heeft, hangt per definitie af van vanuit welk land je kijkt.

### b. De meetroute verschilt per groep

`enrichment_status` laat twee verschillende verrijkingspaden zien:

| | via `jina` (volledige pagina) | via `search` (snippets) |
|---|---:|---:|
| klanten | 118 (51%) | 113 (49%) |
| controles | 53 (29%) | 128 (70%) |

Klanten zijn dus vaker langs de diepere route gegaan. Wie meer tekst voorgelegd
krijgt, vindt meer bewijs en deelt hogere signaalwaarden uit. Dat verschil zit
in de *meting*, niet in het bedrijf.

### c. De klanten zijn de grote, oude winnaars

De klantzijde bevat The Boston Consulting Group (€2,72M), Luiss Guido Carli
(€877k), SKF Industrie (€649k). Mediaan acquisitiejaar 2018; 35% is van vóór
2017. Dat is de bovenkant van tien jaar verkoop, niet een doorsnee prospect.

### d. En de basiskans is 56 keer te hoog

56% klanten in de ontwikkelset tegen circa 1% in de live lijst. Een AUC blijft
formeel vergelijkbaar over base rates, maar de gekozen drempels, de
tierindeling en het gevoel van "dit werkt" zijn dat niet.

---

## 3. De beslissende test

Als het foreign-HQ-signaal echt fit meet, moet het ook binnen één land werken.
AUC van elk signaal, op dezelfde data, verschillend afgebakend:

| signaal | hele ontwikkelset | **alleen Italiaanse bedrijven** | zonder de Spaanse controles |
|---|---:|---:|---:|
| `sig_foreign_hq_score` | 0,692 | **0,441** | 0,619 |
| `sig_intl_footprint_score` | 0,598 | 0,586 | 0,684 |
| `sig_explicit_lnd_score` | 0,613 | 0,602 | 0,631 |
| `sig_lnd_onboarding_score` | 0,603 | 0,525 | 0,606 |
| `sig_employer_branding_score` | 0,537 | 0,523 | 0,510 |

`sig_foreign_hq_score` valt van 0,692 naar **0,441** zodra je alleen naar
Italiaanse bedrijven kijkt — onder toeval, dus licht de verkeerde kant op. Het
signaal onderscheidde geen goede van slechte prospects; het onderscheidde
**Spaanse binnenlandse bedrijven van internationale concerns**.

> **Voorbehoud:** die kolom rust op 75 klanten tegen 19 Italiaanse controles.
> Met 19 controles is 0,441 statistisch wankel. Maar de richting komt overeen
> met de live meting (AUC 0,533 op 19.600 bedrijven), en dat is wél een
> stevige steekproef. Twee onafhankelijke metingen wijzen dezelfde kant op.

De andere signalen houden zich beter: `explicit_lnd` en `intl_footprint`
verliezen weinig. Het probleem is dus niet "alle signalen waren nep" — het is
gericht: **het signaal met verreweg het zwaarste gewicht is precies het signaal
dat het meest door het ontwerp van de ontwikkelset werd opgeblazen.**

---

## 4. Wat er daarna in productie is gebeurd

Dit is de tweede, losstaande oorzaak — en de best aantoonbare.

In `italy_lead_prioritizer_final.xlsx` staat een kolom
`v2_score_input_mapping_note`, voor alle 19.600 rijen identiek:

```
v2→score_company mapping: sig_foreign_hq_score<-foreign HQ,
sig_intl_footprint_score<-international_profile,
sig_lnd_onboarding_score<-onboarding_training_need,
sig_explicit_lnd_score<-icp_keyword_match,
sig_employer_branding_score<-employer_branding,
lusha_employee_range<-lusha_employees; ti_onboarding/rapid_growth=0.0;
company_size_complexity (the 0-3 audit signal) is not used as the employee
range -- lusha_employees is; competitor not mapped.
```

Er draait dus een **v2-pijplijn die andere signalen produceert**, waarna die via
een vertaaltabel op de invoervelden van het oude model worden geplakt. Dat gaat
op drie manieren mis.

### a. De schaal is ingekrompen van 0-3 naar 0-2

Waargenomen waarden over alle 19.600 productierijen:

| invoerveld van het model | waargenomen waarden | hoogste |
|---|---|---:|
| `score_input_foreign_hq` | 0 · 3 | **3** |
| `score_input_intl_footprint` | 0 · 1 · 2 | **2** |
| `score_input_explicit_lnd` | 0 · 1 · 2 | **2** |
| `score_input_lnd_onboarding` | 0 · 1 · 2 | **2** |
| `sig_employer_branding_score` | 0 · 1 · 2 | **2** |
| `score_input_rapid_growth` | 0 | 0 |

In de ontwikkelset haalde 72% van de klanten een 3 op `intl_footprint` en 47%
op `explicit_lnd`. In productie is een 3 op die velden **onbereikbaar**.

De scoringcode normaliseert met `clamp(v,0,3)/3`. Gevolg: vier signalen kunnen
nog maar tot 2/3 van hun bedoelde bijdrage komen, terwijl `foreign_hq` binair
0 of 3 blijft en dus als enige zijn **volle** gewicht haalt.

**Dat verklaart de dominantie van foreign HQ mechanisch.** Het is niet dat het
signaal bewust te zwaar is gezet — de coëfficiënt van 0,7465 was gefit toen alle
signalen dezelfde 0-3-ruimte hadden. Alle andere signalen zijn daarna met een
derde ingekort en foreign HQ niet. Vandaar de uitkomst uit de audit: mét foreign
HQ maximaal 9,53 (Hot), zonder foreign HQ maximaal 6,64 (Cool).

### b. De betekenis van twee signalen is vervangen

- `sig_explicit_lnd_score` ← **`icp_keyword_match`**
- `sig_intl_footprint_score` ← **`international_profile`**

De coëfficiënt voor "op de site staat expliciet bewijs van L&D-activiteit" wordt
nu toegepast op "hoeveel ICP-trefwoorden komen voor". Dat zijn andere grootheden.
Een gewicht dat op grootheid X is gefit, is niet geldig voor grootheid Y.

### c. Twee signalen staan hard op nul, één is constant

`ti_onboarding` en `rapid_growth` zijn letterlijk `0.0` gezet — dat is de
"2 of 7 signal fields missing" uit elke rescore-audit. En
`sig_company_size_complexity_score` heeft voor alle 18.321 gevulde rijen de
waarde **2** en verder niets; het is bovendien expliciet niet in gebruik. Een
constante draagt per definitie geen informatie.

### d. Wat er overblijft is bijna geen variatie

| veld | meest voorkomende waarde | aandeel |
|---|---|---:|
| `score_input_lnd_onboarding` | 2 | 86% |
| `score_input_intl_footprint` | 2 | 74% |
| `score_input_explicit_lnd` | 2 | 73% |

Als driekwart van alle bedrijven dezelfde waarde krijgt, kan het signaal
niemand meer van niemand onderscheiden. Precies wat de audit vond: binnen elke
groottecategorie is de AUC van de eindscore exact 0,50.

---

## 5. De keten in één alinea

De signalen zagen er in de ontwikkelset beter uit dan ze waren, doordat de
controlegroep uit een ander land kwam, langs een andere meetroute liep en tegen
de grootste klanten uit tien jaar verkoop werd afgezet — waarbij vooral
`foreign_hq` profiteerde. Dat opgeblazen signaal kreeg 47% van het gewicht.
Vervolgens is het model in productie gevoed door een andere pijplijn, die de
vier overige signalen op een 0-2-schaal aanlevert en twee ervan op nul zet,
terwijl `foreign_hq` als enige zijn 0-3-schaal behield. Het resultaat is een
score die feitelijk alleen nog "heeft dit bedrijf een buitenlands
hoofdkantoor, ja of nee" meet — en dat is precies het signaal waarvan de
oorspronkelijke validatie het minst betrouwbaar was.

---

## 6. Welke eerdere conclusies blijven staan

**Blijft staan:**
- Het model is kapot en presteert onder sorteren-op-grootte. Bevestigd op zowel
  HubSpot-labels als factuurlabels (zie `SCORING_HERONTWERP_2026-07-26.md`).
- Foreign HQ is commercieel wél waardevol op **verwachte waarde** (3,0x, sectie
  7 van `MODEL_AUDIT_EN_NIEUW_MODEL.md`). Dat is een andere meting — op de live
  populatie, gewogen met omzet — en die wordt hier niet weersproken.

**Vervalt of moet anders:**
- "Het model wás gevalideerd op AUC 0,772." Die validatie is niet houdbaar: de
  controlegroep was geen geldige vergelijkingsgroep. Het getal meet vooral het
  onderscheid tussen twee steekproefkaders.
- "`sig_foreign_hq_score` had AUC 0,703 in de ontwikkelset." Binnen één land
  zakt dat naar 0,441. De 0,703 is een landeffect.

---

## 7. Wat dit betekent voor de herbouw

1. **Repareer eerst de mapping, of haal de vertaallaag helemaal weg.** Zolang
   v2-signalen op v1-coëfficiënten worden geplakt, is elke rescore ruis. Dit is
   een codewijziging van beperkte omvang en de goedkoopste winst die er ligt.
2. **Fit nooit meer op een case-controlset met een andere herkomst.** Trek
   klanten én niet-klanten uit dezelfde populatie — dezelfde landen, dezelfde
   bron, dezelfde verrijkingsroute. Met de factuurlijst kan dat nu: label de
   volledige Italiaanse master en laat de niet-klanten gewoon niet-klant zijn,
   op hun echte basiskans.
3. **Toets elk signaal binnen land én binnen grootteklasse** voordat het gewicht
   krijgt. Precies de test uit sectie 3; die had dit in één tabel zichtbaar
   gemaakt.
4. **Controleer bij elke pijplijnwijziging de schaal van de invoervelden.** Eén
   regel die de waargenomen minima en maxima per signaal afdrukt en vergelijkt
   met de ontwikkelset, had dit een jaar eerder gevonden.
5. Houd er rekening mee dat de vijf AI-signalen sowieso weinig toevoegen: zonder
   die signalen presteerde het GBM-prototype even goed (lift 3,42x tegen 3,39x).
   Repareren is goedkoop; er veel van verwachten is niet realistisch.

---

## 8. Reproduceerbaarheid

| bron | gebruikt voor |
|---|---|
| `Results1.xlsx`, tabblad `Sheet1` | ontwikkelset: labels, signalen, land, verrijkingsroute |
| `2026-07-06_invoice_list (2).xlsx` | controle dat de labels de facturen zijn (230/232) |
| `Countries/Italy/italy_lead_prioritizer_final.xlsx`, `Enriched Leads` | productiewaarden, mapping-note, schaalcontrole |
| `commercial_fit_scoring.py` (GitHub-repo) | de normalisatie `clamp(v,0,3)/3` |

Scripts in de scratchpad van deze sessie: `analyse_results1.py`.
