# Scoring Italië — herontwerp na binnenkomst van de factuurlijst

**Datum:** 2026-07-26
**Aanleiding:** de echte klantenlijst van Marina (`2026-07-06_invoice_list (2).xlsx`)
is binnengekomen. Dit document vervangt de probleemstelling waarmee deze sessie
begon en corrigeert een aantal conclusies uit
`MODEL_AUDIT_EN_NIEUW_MODEL.md` (25-07).

Alle cijfers hieronder zijn zelf doorgemeten op de bronbestanden, niet
overgenomen. Waar een meting onzeker is, staat de onzekerheid erbij.

---

## 1. Waar we stonden

Kort, om het vertrekpunt vast te leggen:

- Het productiemodel werkt niet: eindscore AUC 0,604 tegen 0,643 voor *alleen
  bedrijfsgrootte*. Binnen elke groottecategorie is de AUC exact 0,50.
- Een prototype-GBM op vrije Lusha-velden haalde AUC 0,727 / lift 3,39x.
- De vijf dure AI-signalen voegen niets toe (zonder die signalen: lift 3,42x).
- KvK-bedrijven leken waardevoller dan Lusha-bedrijven, maar het bewijs was
  wankel en is die dag twee keer gecorrigeerd.
- 4.671 KvK-bedrijven staan geparkeerd buiten de app op een
  domeinverificatieprobleem.

Het probleem waarmee deze sessie begon: het nieuwe model draait op Lusha-velden
en kan de KvK-groep dus niet ranken, terwijl juist die groep waardevol lijkt.

---

## 2. Nieuwe harde vondst: de KvK-bedrijven hébben geen kenmerken

Dit is geen interpretatiekwestie maar een feit in de data. Veldvulling in
`italy_batch_input_final.xlsx` — de 19.600 bedrijven die de live app voeden:

| kanaal | n | employees | revenue | description | sub-industry |
|---|---:|---:|---:|---:|---:|
| lusha | 12.409 | 100% | 22% | 42% | 44% |
| lusha + KvK | 5.912 | 100% | 37% | 57% | 58% |
| **alleen KvK** | **1.279** | **0%** | **0%** | **0%** | **0%** |

De ruwe registerexport (`Italy200/00_raw/Italy200_*.xlsx`) bevat 21 kolommen:
naam, adres, postcode, provincie, telefoon, e-mail, website. **Geen
werknemersaantal, geen omzet, geen sector.** De "50 / 100 / 200" bestaat alleen
als mapnaam — het is de bandbreedte waarmee de registerquery is gedraaid, niet
een waarde per bedrijf.

Gevolg: elk model dat op Lusha-velden draait, kan per definitie 0% van de
alleen-KvK-groep ranken. Ook de 4.671 geparkeerde bedrijven niet. Dat is een
harde randvoorwaarde voor elk ontwerp hieronder.

---

## 3. De factuurlijst

`2026-07-06_invoice_list (2).xlsx`, tabblad `invoice_list`.

- **9.584 facturen**, 2012 t/m juli 2026, 29 kolommen.
- **1.077 unieke bedrijven** (na normalisatie van de bedrijfsnaam), **€24,7M**
  gefactureerd.
- Per factuur: bedrijfsnaam, land, bedrag, acquisitiejaar, verzenddatum,
  contactpersoon, telefoon, betaalstatus. **Geen domein en geen HubSpot-ID.**

Landverdeling (top 8, per bedrijf toegewezen aan het meest voorkomende land):

| land | bedrijven | omzet |
|---|---:|---:|
| **Italië** | **500** | **€10,96M** |
| Nederland | 86 | €3,31M |
| Japan | 5 | €2,86M |
| Duitsland | 102 | €1,71M |
| België | 76 | €1,39M |
| Zwitserland | 59 | €1,11M |
| Frankrijk | 48 | €0,75M |
| Spanje | 61 | €0,41M |

Italië is 44% van alle ooit gefactureerde omzet. Na normalisatie werk ik verder
met **483 Italiaanse bedrijven / €10,64M**.

### Waarom dit een veel beter label is dan wat we gebruikten

| | HubSpot `lifecyclestage` | factuurlijst |
|---|---|---|
| aard | verkoophygiëne, handmatig gezet | financiële administratie |
| Italiaanse positieven | 326 | **483** |
| bedrag per klant | niet aanwezig | ja, per factuur |
| tijdstip | alleen `createdate` van het record | acquisitiejaar + elke factuurdatum |
| verloop zichtbaar | nee | ja (laatste factuurdatum) |
| koppelsleutel | domein | **alleen naam** |

Drie dingen die nu pas kunnen:
1. **Rangschikken op verwachte waarde** in plaats van op kans alleen.
2. **Tijdgesplitste validatie** — trainen op wie vóór 2023 klant werd, testen op
   2023-2026. Dat was in `MODEL_AUDIT` expliciet als openstaand punt genoteerd.
3. **Verloop meenemen** — 216 van de 483 (45%) hebben sinds 2024 nog gefactureerd;
   de rest is stil.

### De waardeverdeling is extreem scheef

| | |
|---|---:|
| top 5 bedrijven | 27,3% van de Italiaanse omzet |
| top 10 | 39,6% |
| top 20 | **54,0%** |
| top 50 | 73,0% |
| mediaan levensduuromzet | **€4.305** |
| gemiddelde | €22.026 |

Twintig bedrijven zijn de helft van de omzet. Een model dat alleen
P(wordt klant) optimaliseert, behandelt SKF Industrie (€648.616) en een
willekeurige €3.000-klant als hetzelfde doelwit. Dat is de kern van waarom het
huidige onderscheidend vermogen commercieel tegenvalt, los van de AUC.

---

## 4. Wat de factuurdata omkeert

Matching van de 483 Italiaanse factuurbedrijven tegen
`Italy_Companies_Lusha_vs_Chamber_of_Commerce_Master.xlsx`, op genormaliseerde
naam (rechtsvormen, accenten en generieke woorden gestript; exacte match plus
tokencontainment).

| bron in master | klanten | omzet | per klant | conversie¹ | verwachte waarde per bedrijf |
|---|---:|---:|---:|---:|---:|
| alleen Lusha | 68 | €1,23M | €18.119 | 0,50% | €91 |
| **alleen KvK** | **131** | **€4,38M** | €33.434 | **1,63%** | **€546** |
| beide bronnen | 167 | €2,94M | €17.600 | **2,79%** | €491 |
| niet in master | 117 | €2,09M | €17.842 | — | — |

¹ tegen de masterpopulatie: 13.566 Lusha-only, 8.026 KvK-only, 5.981 beide.

**Correctie op `MODEL_AUDIT` sectie 7b.** Daar stond, op basis van
HubSpot-labels en domeinmatching: *"KvK-only converteert niet beter dan Lusha
(0,52% vs 0,56%), maar de klanten zijn ruim 4x zo groot."* Op de factuurdata is
dat andersom: **KvK-only converteert 3,3x beter** (1,63% vs 0,50%). De
oorspronkelijke richting — KvK is de waardevollere bron — klopt, maar het
mechanisme is een ander.

### Maar: het waardeverschil is grotendeels ouderdom

Uitgesplitst naar acquisitiecohort:

| cohort | alleen Lusha | alleen KvK | beide | niet in master |
|---|---|---|---|---|
| 2012-2016 | 10 · €527k | 25 · €2.314k | 44 · €1.329k | 22 · €1.130k |
| 2017-2019 | 11 · €137k | 22 · €987k | 36 · €514k | 16 · €702k |
| 2020-2022 | 11 · €266k | 36 · €640k | 43 · €713k | 36 · €153k |
| 2023-2026 | 36 · €302k | 47 · €383k | 44 · €383k | 43 · €157k |

Alleen het cohort 2020-2026 (recent gewonnen, vergelijkbare looptijd):

| bron | klanten | conversie | omzet per klant |
|---|---:|---:|---:|
| alleen Lusha | 47 | 0,35% | €12.087 |
| alleen KvK | 83 | **1,03%** | €12.329 |
| beide | 87 | **1,45%** | €12.593 |

**De omzet per klant is dan vrijwel identiek.** De €33k-vs-€18k uit de tabel
hierboven is een looptijdeffect: KvK-klanten zijn oververtegenwoordigd in het
cohort 2012-2016 en hebben dus tien jaar langer gefactureerd. SKF factureert
sinds 2016 en staat op 472 facturen.

**Wat overeind blijft:** de KvK-bron levert bij gelijke looptijd **3x zoveel
klanten per benaderd bedrijf**. Bedrijven in beide bronnen 4x zoveel. Dat is
een conversie-effect, geen waarde-effect.

### Twee voorbehouden bij deze cijfers

1. **Naammatching bevoordeelt het register.** Facturen dragen de juridische
   naam ("SKF INDUSTRIE S.P.A."); het handelsregister ook. Lusha voert vaak de
   merknaam. Een klant die alleen in Lusha staat onder een merknaam die niet op
   de factuur voorkomt, valt in "niet in master" — niet in "alleen Lusha". De
   conversie van Lusha is daardoor **waarschijnlijk onderschat**; hoeveel is niet
   te zeggen zonder domeinen bij de facturen. Dit is de belangrijkste zwakte in
   de analyse.
2. **Het cohort 2023-2026 is besmet door het kanaal.** Recente Lusha-klanten
   komen deels uit de huidige app, die zelf op Lusha draait. Meer Lusha-klanten
   in dat cohort (36 tegen 10-11 daarvoor) is deels een gevolg van waar we
   gebeld hebben, niet van waar de markt zit.

---

## 5. Kritische analyse van de HubSpot-clientlijst

De vraag was: hoe is de klantlijst in de Lovable-app tot stand gekomen, en waar
zitten de verschillen.

### De methode

`myngle-company-hub/src/routes/api/hubspot/sync-clients.ts`:

1. HubSpot Companies-search, filter `lifecyclestage = "customer"`, alle pagina's.
2. Van elk record alleen de property `domain` overnemen.
3. Normaliseren: lowercase, protocol/pad/querystring/`www.` eraf.
4. Per land het gepubliceerde `companies.list.json` doorlopen en **exact op
   genormaliseerd domein** vergelijken.
5. Bij een treffer `company_status.status = 'client'` zetten.

Dezelfde definitie (`lifecyclestage`) is ook gebruikt om het model te evalueren
en het prototype te trainen — in de audit verbreed naar
`customer + opportunity + SQL`.

### Zes defecten, gemeten

Op de 326 HubSpot-companies met `country = Italy` en `lifecyclestage = customer`:

| # | defect | omvang |
|---|---|---|
| 1 | **Persoonsaccounts als bedrijf** — `mYngle Pro`-records en losse privénamen (Angela Sorbi, Barbara Mortari, Miriana Lavazza, …) | **20 van 326 (6%)** |
| 2 | **Geen domein** → onzichtbaar voor de sync, kan nooit matchen | **29 (9%)** |
| 3 | **Gedeeld domein** — meerdere records op één domein; de sync markeert dan één app-bedrijf op grond van een ander record | 9 domeinen, 20 records |
| 4 | **Fout domein** — bijv. `KPMG, SpA` → `spilgames.com`, `Storci spa` → `fava.it`, `Landi Renzo` → `lovatogas.com`, `MER MEC` → `angelcompany.com`, `Marraffa` → `werentgroup.com`, `Aptuit SRL` → `evotec.com` | steekproefsgewijs, minstens 6 |
| 5 | **Niet-Italiaanse entiteiten onder `country = Italy`** — World Food Programme Benin, Hoffmann Group Mexico, Anticimex (`.se`), Bucci Industries Swiss SA, Danieli Persia, Teleflex Global Services LLC | minstens 6 |
| 6 | **Exact-domeinmatching** i.p.v. hoofddomein | kost 13,4% dekking (al vastgesteld 25-07) |

Voorbeeld van defect 3 in één regel: `unibocconi.it` staat twee keer, één keer
als `BOCCONI UNIVERSITY` en één keer als het persoonsrecord `Matteo Gaipa`.

### Het verschil met de factuurwaarheid, in twee richtingen

**Richting A — betalende klanten die HubSpot niet als klant kent:**

| | bedrijven | omzet |
|---|---:|---:|
| gefactureerd in Italië | 483 | €10,64M |
| daarvan in HubSpot als `customer` | 288-307 (60-64%) | 78-79% |
| **niet als `customer` in HubSpot** | **176-195 (36-40%)** | **€2,2-2,3M** |

Bandbreedte = gevoeligheid voor de matchingdrempel; de uitkomst is stabiel.
**51 van die ontbrekende bedrijven factureren nog in 2024-2026** — samen €570k.
Dat zijn actieve klanten die het systeem als prospect kan aanmerken.

De grootste ontbrekende:

| bedrijf | levensduuromzet | nog actief |
|---|---:|---|
| Università Bocconi¹ | €391.985 | ja |
| WillCONSULTING S.r.l. | €226.335 | ja |
| L.I.C.AR. INTERNATIONAL S.P.A.¹ | €86.322 | ja |
| EBIT Sardegna | €73.991 | ja |
| Sarlux srl | €67.630 | nee |
| Swegon Operations Srl | €61.820 | ja |
| IP Cleaning Srl | €61.805 | ja |
| Prada | €57.250 | nee |
| The Boston Consulting Group CT Italy | €55.298 | ja |
| Luiss Business School S.p.A.¹ | €51.970 | ja |

¹ Bocconi, Licar en Luiss Business School bestaan wél in HubSpot, maar onder een
andere schrijfwijze of als aparte entiteit. Ze illustreren defect 3/4 in plaats
van een ontbrekend record — de meeste andere regels zijn echt afwezig.

De grootste post in de lijst is `Professionals` (€420.562, nog actief): dat is
geen bedrijf maar de verzamelregel voor het B2C-kanaal. Die moet uit elk label.

**Richting B — HubSpot-klanten zonder één factuur:**
56 van de 326 (17%) zijn met geen enkele factuur te koppelen. Een deel is
matchingruis, maar de persoonsaccounts (20) en de niet-Italiaanse entiteiten
zitten er hoe dan ook in. Realistisch: **10-17% vals-positief**.

### Wat dit betekent voor de audit van gisteren

Het prototype (AUC 0,727) is getraind en gevalideerd op een label dat:
- 36-40% van de echte klanten miste,
- 10-17% niet-klanten als klant telde,
- ~6% persoonsaccounts bevatte,
- geen bedragen kende, en
- geen tijdstip kende.

De AUC-cijfers uit `MODEL_AUDIT` zijn daarmee niet waardeloos — de vergelijking
tussen modellen onderling blijft geldig, want alle modellen zagen hetzelfde
label — maar het **absolute** niveau is niet betrouwbaar, en de vergelijking
tussen bronnen (Lusha vs KvK) was het duidelijkst besmet: HubSpot registreert
per domein en dat is precies waar de KvK-groep struikelt.

---

## 6. Het probleem, opnieuw geformuleerd

Niet: *"welk model geeft de hoogste AUC?"*
Maar: **"hoe geef ik elk Italiaans bedrijf een cijfer dat de verwachte
opbrengst van één belpoging weergeeft, terwijl een kwart van het bestand geen
enkel kenmerk heeft?"**

Drie deelproblemen, in volgorde van hoeveel ze de uitkomst bepalen:

**A. Het datagat (grootste hefboom).** 1.279 bedrijven in de app en 4.671
geparkeerde bedrijven hebben nul kenmerken. Geen model lost dat op; alleen data
lost dat op. Drie stappen, oplopend in kosten:
1. hoofddomein- i.p.v. exact-domeinmatching — gratis, +13,4% dekking;
2. Lusha company-lookup op domein **zonder landfilter** voor de KvK-rijen —
   credits, maar haalt SKF, Capgemini en Jacobs alsnog binnen;
3. de 8.461 al opgehaalde bedrijven verwerken (~USD 175) en de 4.671 geparkeerde
   erdoorheen (~USD 96).

**B. Het label.** Overstappen van `lifecyclestage` naar de factuurlijst als
waarheid. Vergt één ding dat er nu niet is: **een koppeling tussen factuurnaam
en domein**. Zonder domein kan de factuurlijst niet aan de app gekoppeld
worden, alleen aan de master (via naam, met de bias uit sectie 4).

**C. Het doel van de score.** Van fit-score naar
**P(klant) × E(omzet | klant)**, met de kanttekening dat de omzetkant vooral
door looptijd wordt gedreven — dus modelleren op *eerstejaars*omzet, niet op
levensduuromzet, anders leert het model "oud = waardevol".

---

## 7. Voorgestelde aanpak

In volgorde van opbrengst gedeeld door inspanning.

### Stap 1 — Factuurlijst koppelbaar maken (blokkeert al het andere)
De 483 Italiaanse factuurbedrijven een domein geven. Drie bronnen, in deze
volgorde: (a) naammatch tegen de master, die al domeinen heeft — dekt volgens
sectie 4 ongeveer 76%; (b) de HubSpot-records waar naam én domein bekend zijn;
(c) Serper-lookup voor de rest (enkele honderden, verwaarloosbare kosten).
Levert een herbruikbaar bestand `italy_invoice_customers_with_domain.xlsx`.

### Stap 2 — Labels opnieuw opbouwen
Per bedrijf: `ooit_klant`, `eerste_factuurjaar`, `omzet_jaar_1`,
`omzet_levensduur`, `nog_actief`. `Professionals` en de persoonsaccounts eruit.
Dit vervangt `lifecyclestage` in elke analyse.

### Stap 3 — Datagat dichten (parallel aan 1 en 2)
Zie A hierboven. Pas na deze stap is één model over de hele populatie zinvol.

### Stap 4 — Twee modellen, één cijfer
- **P-model:** kans op klant worden. Eén GBM over de hele populatie, met
  ontbrekende waarden als informatie (bron-indicator meegeven — `ch_both` was al
  een topfeature). Tijdsplitsing: trainen t/m 2022, testen op 2023-2026.
- **W-model:** verwachte eerstejaarsomzet. Kan simpel beginnen als een
  groepsgemiddelde per grootteklasse/bron; met 483 labels is een echt model
  krap.
- **Cijfer** = percentiel van P × W binnen Italië, getoond als A-E of 1-10, met
  de twee tot drie sterkste redenen erbij.

### Stap 5 — Tussenoplossing voor de app
Zolang 1-4 lopen: rescore op bedrijfsgrootte + Lusha-omzetklasse en KvK-bron
als plusfactor. Dat is één run en al beter dan de huidige score (0,643 vs
0,604 op de oude labels; op de nieuwe labels waarschijnlijk meer).

---

## 8. Wat dit betekent buiten Italië

De factuurlijst dekt alle landen. Nederland (86 bedrijven, €3,31M), Duitsland
(102, €1,71M), België (76, €1,39M) en Zwitserland (59, €1,11M) hebben genoeg
klanten om dezelfde analyse te doen — en die landen draaien 100% op Lusha zonder
handelsregister. Als het KvK-conversievoordeel daar standhoudt, is een
handelsregisterbron per land opnieuw de grootste onbenutte hefboom, nu met
betere onderbouwing dan gisteren.

Japan valt op: 5 bedrijven, €2,86M. Dat verdient los uitzoeken — het is 12% van
alle omzet ooit uit vijf klanten.

---

## 9. Openstaande beslissingen

1. **Waarde of kans?** Rangschikken op P × E(waarde), op P alleen, of twee
   badges naast elkaar. Sectie 3 (top 20 = 54% van de omzet) pleit voor P × E.
2. **Eerstejaarsomzet of levensduuromzet** als waardedoel. Levensduur beloont
   ouderdom; eerstejaars is eerlijker maar heeft minder spreiding.
3. **Budget** voor het dichten van het datagat (~USD 300 aan enrichment plus
   Lusha-credits voor ±7.200 domain-lookups).
4. **Volgorde:** eerst datagat en labels, of eerst een snelle rescore live.
5. **De geparkeerde 4.671** — de beslissing uit `STATUS.md` over de
   domeinverificatiedrempel staat nog open en blokkeert stap 3.

---

## 10. Bestanden

| bestand | inhoud |
|---|---|
| `Myngle/2026-07-06_invoice_list (2).xlsx` | bron: 9.584 facturen, 2012-2026 |
| `Countries/Italy/Italy_Companies_Lusha_vs_Chamber_of_Commerce_Master.xlsx` | 27.574 bedrijven, bron-toewijzing, Lusha-velden |
| `Countries/Italy/italy_batch_input_final.xlsx` | de 19.600 die de app voeden — hier is de nul-dekking gemeten |
| `Countries/Italy/italy_parked_keep_5621.xlsx` | de geparkeerde KvK-bedrijven |
| `Countries/Italy/MODEL_AUDIT_EN_NIEUW_MODEL.md` | audit 25-07; secties 5 en 7b worden hierboven gecorrigeerd |
| `myngle-company-hub/src/routes/api/hubspot/sync-clients.ts` | de code die de clientlijst maakt |

Analysescripts staan in de scratchpad van deze sessie
(`match_invoices2.py`, `compare_hubspot_invoices.py`) — verplaatsen naar de repo
als ze herhaald moeten worden.
