# Scoringmodel Italië — audit van het huidige model + prototype nieuw model

**Datum:** 2026-07-25. Alle cijfers hieronder zijn zelf doorgemeten op de live data,
niet overgenomen uit eerdere documentatie.

## 1. Waarom dit onderzoek

Aanleiding: de vraag of de ranking werkt, en of foreign-HQ-bedrijven terecht laag
scoren. Vertrekpunt was de vergelijking van HubSpot-klanten tegen het totale bestand.

## 2. Het huidige model werkt niet — en de reden is bekend

### Bevinding
Op 19.600 Italiaanse bedrijven (181 met domein-match op een HubSpot-klant):

| Wat | AUC | lift@10% |
|---|---|---|
| Volledige eindscore (`commercial_fit_score_app`) | 0.604 | 1.66x |
| **Alleen bedrijfsgrootte** | **0.643** | **2.66x** |

Sorteren op puur bedrijfsgrootte levert dus **meer** klanten op in de belbare top
dan het volledige model. Binnen elke groottecategorie apart is de AUC van de score
exact 0.50 (enterprise 0.502, large 0.504, mid 0.503, small_mid 0.509): de
ICP-component voegt **niets** toe bovenop grootte.

### AUC per signaal (live)
| Signaal | AUC live | AUC in Results1.xlsx | verschil |
|---|---|---|---|
| `sig_foreign_hq_score` | 0.533 | 0.703 | −0.171 |
| `sig_explicit_lnd_score` | 0.482 | 0.635 | −0.153 |
| `sig_intl_footprint_score` | 0.545 | 0.631 | −0.086 |
| `sig_lnd_onboarding_score` | 0.523 | 0.626 | −0.103 |
| `sig_employer_branding_score` | 0.448 | 0.562 | −0.115 |

Het model was dus **wel degelijk gevalideerd** (`Results1.xlsx`: 432 bedrijven,
232 klant / 200 prospect, lean model AUC 0.772, bootstrap B=500). Het is onderweg
naar productie kapotgegaan.

### Drie aanwijsbare oorzaken

**a. De signaalschaal is ingestort.** Signalen zijn ontworpen op 0–3
(normalisatie in `commercial_fit_scoring.py` is letterlijk `clamp(v,0,3)/3`).
In productie wordt de waarde 3 nooit meer uitgedeeld:

| Signaal | % waarde 3 in Results1 | % waarde 3 live |
|---|---|---|
| `sig_intl_footprint` | 61,8% | 0% |
| `sig_explicit_lnd` | 38,2% | 0% |
| `sig_lnd_onboarding` | 6,9% | 0% |

Alles klontert op 2 (resp. 74,6% / 67,3% / 80,5% van alle bedrijven). Het
onderscheid waar de voorspellende kracht in zat, bestaat niet meer.

**b. Twee van de zeven signalen worden nooit geproduceerd.**
`ti_onboarding_score` en `sig_rapid_growth_score` hebben 0% dekking over alle
19.600 bedrijven → voor iedereen op 0 gezet. Staat ook in elke rescore-audit:
*"2 of 7 signal field(s) missing (defaulted to 0)"*.

**c. Eén niet-voorspellend signaal domineert.** `sig_foreign_hq_score` heeft
coëfficiënt 0,7465 — 47% van alle positieve gewicht (som 1,58) — en is binair 0/3.
Doorgerekend met de echte scoringcode:
- mét foreign HQ, alles verder maximaal → eindscore 9,53 (Hot)
- zónder foreign HQ, alles verder maximaal én grootste bedrijfsklasse → 6,64 (Cool)

Voor de 80,8% zonder foreign HQ is Hot dus onbereikbaar — terwijl dat signaal
AUC 0.533 heeft (niet te onderscheiden van willekeur).

> **Nuancering, zie sectie 7:** AUC 0.533 betekent hier *niet* dat foreign HQ
> waardeloos is. AUC is waardeblind en meet alleen óf iemand klant wordt. Op
> **verwachte waarde** is foreign HQ juist een van de sterkste signalen (3,0x).
> Het zware gewicht (0,7465) is daarmee verdedigbaar; het **binaire** karakter
> (0 of 3, geen gradatie) blijft wel een probleem.

## 3. Wat wél voorspelt (klant vs. totaal, Italië)

Lift = oververtegenwoordiging bij klanten t.o.v. het hele bestand.

**Lusha-omzetklasse — sterkste enkelvoudige signaal, monotoon:**
$10B–100B 18,1x · $500M–1B 5,2x · $1B–10B 4,2x · $250M–500M 3,9x ·
$100M–250M 1,5x · $50M–100M 1,2x · $1M–10M 0,19x
(dekking maar 18,6%, maar waar aanwezig zeer informatief)

**Bedrijfsgrootte:** enterprise 3,30x · large 2,89x · large_enterprise 1,77x ·
mid_large 1,29x · mid 0,98x · small_mid 0,64x

**Sub-industry (fijnmazig, veel beter dan brede sector):** Real Estate Agencies
31,7x (n=55, wankel) · Software Development 4,7x · Medical Equipment 4,5x ·
Motor Vehicles 3,4x · Hotels 3,4x · Industrial Machinery 3,1x (n=800, robuust)
→ brede sector is juist waardeloos: Manufacturing 0,98x, Technology 1,01x

**Bedrijfsleeftijd:** 50–100 jaar 2,41x · 100+ jaar 1,66x
**Bron:** in Lusha én KvK 1,85x · alleen KvK 0,25x
→ *dit KvK-cijfer is een meetfout, zie sectie 5: gemeten op de gefilterde
app-lijst waaruit de geparkeerde KvK-bedrijven juist verwijderd zijn*
**Foreign HQ:** 1,44x op conversie — maar **3,0x op verwachte waarde**, zie sectie 7

## 4. Prototype nieuw model

**Labels** — uit HubSpot, veel rijker dan alleen klanten: 213 customer +
670 opportunity + 31 SQL = 914 positieven op 24.863 bedrijven (base rate 3,7%).
**Features** — 78 stuks: alle vrije Lusha-velden (omzetklasse, exact
werknemersaantal, oprichtingsjaar, funding, IPO, beschrijvingslengte, aantal
specialiteiten, aantal gevulde Lusha-velden als prominentie-proxy), bron/kanaal,
foreign HQ, de 5 bestaande sig_-signalen, en one-hot van top sub-industry /
industry / stad.
**Model** — HistGradientBoosting, 5-voudige kruisvalidatie.

| Model | AUC | lift@10% |
|---|---|---|
| Huidige eindscore (baseline) | 0.580 | 1.49x |
| Alleen bedrijfsgrootte | 0.623 | 2.35x |
| Alleen Lusha-omzetklasse | 0.671 | 2.15x |
| Alleen de 5 oude sig_-signalen | 0.619 | 2.04x |
| Grootte + omzet + #werknemers | 0.679 | 2.68x |
| Logistische regressie (alle features) | 0.710 | 3.27x |
| **Nieuw model, alle features (GBM)** | **0.727** | **3.39x** |
| Nieuw model **zonder** de sig_-signalen | 0.721 | **3.42x** |

Op het strengere label (alleen echte klanten, n=213): nieuw model AUC 0.670 /
lift 2,86x tegen huidige score 0.604 / 1,66x.

**Kernpunt:** het weglaten van de vijf oude AI-signalen kost niets — de lift wordt
zelfs marginaal beter (3,42x vs 3,39x). De dure enrichment levert geen
voorspellende waarde op die de gratis Lusha-velden niet al bieden.

**Belangrijkste features** (permutation importance, AUC-verlies):
`emp_exact` 0.038 · `desc_len` 0.021 · `ch_both` (in beide bronnen) 0.019 ·
`chamber_cnt` 0.014 · `size_ord` 0.014 · `year_founded` 0.011 ·
`lusha_fields` 0.009 · `rev_band` 0.007
→ daarna pas de sig_-signalen (0.007 en lager).

Praktisch: base rate 3,68%. In de top 10% van de lijst haalt de huidige score
5,5% raak, het nieuwe model 12,5% — ruim **twee keer zoveel bruikbare
gesprekken per belronde**.

## 5. Brondekking — en een correctie op een eerdere aanname

### Hoeveel Italiaanse klanten zitten überhaupt in onze bronnen?
326 Italiaanse klanten in HubSpot (`country` = Italy/Italia), 297 met domein,
286 unieke domeinen. Matching op **hoofddomein** (subdomeinen samengevouwen):

| | aantal | % |
|---|---|---|
| in Lusha | 170 | 59,4% |
| in KvK | 124 | 43,4% |
| in minstens één bron | 193 | 67,5% |
| idem + naam-match | 210 | 73,4% |
| **echt nergens** | **76** | **26,6%** |

Strikte matching op exact domein gaf 55,6% / 33,9% — te pessimistisch. Het
verschil zit in **subdomeinen**, niet in ontbrekende bedrijven:

| HubSpot heeft | master heeft | bron |
|---|---|---|
| `airliquide.it` | `it.airliquide.com`, `airliquide.com` | KvK |
| `luiss.it` | `businessschool.luiss.it`, `lsl.luiss.it` | KvK + Lusha |
| `cameo.it` | `company.cameo.it` | beide |
| `willistowerswatson.com` | `crbclientportal.willis.it` | KvK |
| KPMG (4 entiteiten) | alle `kpmg.com` | KvK |

**Let op:** eerder in deze sessie is beweerd dat Luiss Guido Carli helemaal niet
in de dataset zat. Dat klopt niet — Luiss staat er wél in, onder subdomeinen.

Wat na correctie echt ontbreekt (76 klanten, €851k) zijn internationale concerns
op een `.com`-groepsdomein: Willis Towers Watson, Tennant, Stanley Black & Decker.
Lusha registreert die onder hun wereldwijde hoofdkantoor, dus een land-gefilterde
Italië-pull vindt ze per definitie nooit.

### KvK-bron: eerdere conclusie omgedraaid, daarna zelf ook gecorrigeerd
Eerder in deze sessie is op de **live app-lijst** gemeten dat KvK-only bedrijven
lift 0,25x hebben (nauwelijks klanten). Die meting is misleidend: de live lijst
is juist het bestand waaruit de geparkeerde KvK-bedrijven zijn weggefilterd.
Op het **volledige** master-bestand:

| Bron | n | % in trechter | % klant | omzet/klant |
|---|---|---|---|---|
| Lusha only | 13.566 | 3,86% | 0,91% | € 26.964 |
| **KvK only** | 8.026 | **3,90%** | 0,96% | **€ 69.599** |
| Beide bronnen | 5.981 | **7,31%** | 1,71% | € 29.080 |

> **Correctie (2026-07-25):** deze tabel telt dubbel. KvK heeft 1,23 rijen per
> hoofddomein (meerdere Italiaanse rechtspersonen van hetzelfde concern — SKF
> telde 2x mee met € 747.729, COOP 3x). Ontdubbeld op hoofddomein:
>
> | Bron | n | klanten | omzet/klant | verwachte waarde/bedrijf |
> |---|---|---|---|---|
> | Lusha only | 12.389 | 74 (0,60%) | € 19.198 | € 115 |
> | **KvK only** | 5.952 | 45 (0,76%) | **€ 68.128** | **€ 515** |
> | Beide | 5.947 | 99 (1,66%) | € 27.730 | € 462 |
>
> Zie sectie 7b voor de definitieve cijfers: ook deze tabel is nog niet
> gefilterd op HubSpot-land, waardoor 25 niet-Italiaanse klanten meetellen.
>
> **Verworpen hypothese:** het verschil komt *niet* doordat KvK-bedrijven vaker
> een buitenlands concerndomein (.com) hebben. Getoetst: KvK-klanten met een
> `.it`-domein zijn juist het meest waard (€ 80.263 vs € 64.661).

Na volledige correctie (ontdubbeld én gefilterd op HubSpot-land, zie 7b):
KvK-bedrijven converteren **niet beter** dan Lusha-bedrijven (0,52% vs 0,56%),
maar de klanten die eruit komen zijn **ruim 4x meer waard** (€ 79.913 vs
€ 19.221). Bedrijven in beide bronnen converteren wél bijna drie keer zo vaak
(1,56%) — consistent met `ch_both` als sterke feature in het nieuwe model.

Verklaring: Lusha vangt multinationals onder hun wereldwijde HQ, de KvK vangt de
Italiaanse entiteit. De grote Italiaanse dochters van internationale concerns
zitten dus juist in de KvK-stroom, en dat zijn de dikkere contracten.

## 6. De 4.671 geparkeerde KvK-bedrijven — waarde en technisch pad

Uit `italy_parked_keep_5621.xlsx` (zie `STATUS.md` voor de ontstaansgeschiedenis):

- **4.671 van de 5.621 (83%) staan nog steeds niet in de app**; 4.556 zitten
  volledig buiten de HubSpot-trechter.
- Bewijs dat het goede bedrijven zijn: **59 van deze geparkeerde bedrijven zijn
  al klant geworden** — buiten de app om, via andere kanalen — samen goed voor
  **€4.695.758**, gemiddeld **€79.589 per klant**. Bijna 3x een Lusha-klant.

Doorgerekend met het historische KvK-conversiecijfer (0,96%):

| Scenario | verwachte klanten | waarde |
|---|---|---|
| Conservatief (halve snelheid) | 22 | € 1,5M |
| Historisch KvK-tempo | 44 | € 3,0M |
| Optimistisch (1,5x, actief gebeld) | 66 | € 4,6M |

**Voorbehoud:** 0,96% is een *passief* cijfer — die bedrijven werden klant zonder
gebeld te worden. Actief bellen kan dat verhogen, maar het kan ook zijn dat de
makkelijkste al binnen zijn. Reken op €1,5M als ondergrens, €3M als middenscenario.
Dit is levensduur-omzet, geen jaaromzet.

### Kosten om ze alsnog door te sluizen
Enrichmentkosten uit `Results1.xlsx` (n=432): gemiddeld **USD 0,0205 per bedrijf**
(step1 0,0163 + step2 0,0042). Voor 4.671 bedrijven: **circa USD 96**.

Tegenover €1,5–3M verwachte waarde is dat verwaarloosbaar. Dit is waarschijnlijk
de hoogste verhouding opbrengst/inspanning in het hele systeem — hoger dan het
scoringsmodel opnieuw bouwen.

### Technisch pad
Het bestaande merge-pad kan dit aan zonder de gepubliceerde export te overschrijven:
1. Parked-lijst door de enrichment/scoring-pijplijn (`lead_prioritizer_batch_*`)
   → levert een `lead_prioritizer_final`-achtige xlsx.
2. `export_lead_prioritizer_to_lovable_json.py --input-xlsx <die xlsx>
   --country Italy --cold-callers "<huidige pool>"`.
3. `lovable_gcs_merge.py` — merget de nieuwe batch in de al gepubliceerde
   `italy/current/` export: hernummert bucketbestanden zodat ze niet botsen,
   voegt toe op `company_id` en werkt het manifest bij. Bestaande bedrijven
   blijven ongemoeid.
4. Daarna `reallocate_callers_from_gcs.py` om de nieuwe bedrijven aan callers
   toe te wijzen.

**Openstaand blijft de beslissing uit `STATUS.md`**: de verifier wijst deze
bedrijven af op een gedeeld groepsdomein. Optie 1 (drempel verlagen + zichtbaar
"laag-vertrouwen-domein"-label voor de caller) is nu beter te onderbouwen dan
in juli: de conversiecijfers laten zien dat het geen zwakke prospects zijn.

## 7. Foreign HQ: kans versus waarde — correctie op sectie 2

Eerder in deze analyse is geconcludeerd dat `sig_foreign_hq_score` "niet
voorspelt" (AUC 0.533) en dat het gewicht van 0,7465 onverdedigbaar is.
**Die conclusie was te stellig en is hierbij gecorrigeerd.**

AUC en lift meten alleen óf een bedrijf klant wordt. Ze zijn waardeblind.
Uitgesplitst naar verwachte waarde (live app-lijst, 19.298 unieke hoofddomeinen):

| | n | conversie | omzet per klant | verwachte waarde per bedrijf |
|---|---|---|---|---|
| zonder foreign HQ | 16.929 | 0,88% | € 21.408 | € 188 |
| **met foreign HQ** | 2.369 | **1,44%** | **€ 39.578** | **€ 568** |

**3,0x meer verwachte waarde** — opgebouwd uit 1,63x meer kans én 1,85x grotere
deal. Via het c5-oordeel nog iets scherper: bevestigd buitenlands moederbedrijf
1,62% conversie en € 41.286 per klant.

Bij base rates rond 1% levert een verhouding van 1,63x nauwelijks AUC-beweging
op, terwijl het commercieel zeer relevant is. Vandaar de schijnbare tegenspraak.

### De rode draad van dit hele onderzoek
Drie losse bevindingen wijzen dezelfde kant op:
- foreign HQ → 3,0x verwachte waarde (deze sectie)
- KvK-klanten → € 79.913 vs € 19.221 per klant (gecorrigeerd, sectie 7b)
- de KvK-bron vangt juist de **Italiaanse dochters van buitenlandse concerns**,
  precies waar Lusha ze mist (die staan onder het wereldwijde HQ)

**De Italiaanse vestiging van een internationaal concern is het beste
klanttype.** De oorspronkelijke hypothese achter `sig_foreign_hq_score` klopte
dus. Het systeem zit zichzelf op twee manieren in de weg: de meting is binair
en grofmazig (0 of 3; 551 "unclear"-gevallen krijgen niets), én de bedrijven die
dit type het zuiverst vertegenwoordigen staan geparkeerd buiten de app (sectie 6).

### Wat blijft staan als kritiek
- Binair 0/3 sluit Hot-tier volledig af voor 80,8% van de bedrijven; een
  gradatie zou beter zijn.
- De 551 "unclear"-gevallen (o.a. Engel & Völkers, franchisestructuren) vallen
  tussen wal en schip.
- **Belangrijkste punt:** het model optimaliseert op *fit*, niet op *verwachte
  waarde*. Foreign HQ is het duidelijkste bewijs waarom dat verschil uitmaakt —
  op kans-alleen lijkt het zwak, op waarde is het een topsignaal. Ranken op
  P(conversie) × E(waarde) is daarom geen verfijning maar een kernverbetering.

## 7b. WAAROM Lusha bedrijven mist — en wat dat voor mYngle betekent

Dit is de belangrijkste bevinding van het hele onderzoek. Er blijken **twee
losstaande oorzaken** te zijn, die eerder door elkaar gehaald zijn.

### Oorzaak 1: Lusha's resultaatplafond van 10.000 (willekeurig)
Lusha kapt elke prospecting-query af op 10.000 resultaten. Live gemeten
(2026-07-25): Italië heeft **18.573** bedrijven in de band 51-200, maar de
oorspronkelijke pull haalde er **10.000** op (checkpoint: 200 pagina's × 50,
400 credits). Ditzelfde plafond is zichtbaar in Lusha's eigen webinterface
("1 - 25 out of 10.000").

Verder wordt **alles onder 51 werknemers bewust nooit bevraagd** (staat zo in
`lusha_prospecting_app.py`: *"Below 51 is deliberately excluded — Myngle's
commercial minimum"*); Lusha heeft daar 138.279 Italiaanse bedrijven. En de
sectoren Government + Community worden standaard uitgesloten (549 bedrijven).

**Opgelost op 2026-07-25:** door per hoofdsector te queryen blijft elk segment
onder het plafond. 8.461 ontbrekende bedrijven opgehaald (~752 credits), zie
`lusha_italy_51_200_missing_8461.jsonl` en `pull_missing_beyond_cap.py` in deze
map. Evenredig over alle sectoren verdeeld — dit is een willekeurige afkapping,
geen kwaliteitsselectie.

### Oorzaak 2: de structurele blinde vlek (niet willekeurig)
Het plafond verklaart maar **935** van de 8.026 KvK-only bedrijven. De overige
~7.100 hebben een andere oorzaak, en dáár zit de waarde.

**Lusha indexeert per domein, op concernniveau, in het land van het hoofdkantoor.**
`skf.com` staat onder Zweden, `capgemini.com` onder Frankrijk, `jacobs.com`
onder de VS. Een pull met `country = Italy` vindt die per definitie nooit — ook
niet in de 200+ band, want die pull was wél compleet (9.547, ruim onder het
plafond).

**Het handelsregister registreert Italiaanse rechtspersonen.** Daar staan
"SKF AUTOMOTIVE ITALY S.R.L." en "SKF INDUSTRIE S.P.A." keurig als Italiaanse
entiteiten. Vandaar ook 1,23 rijen per domein bij KvK tegen 1,05 bij Lusha:
multinationals hebben meerdere Italiaanse dochters onder één concerndomein.
Dat is tevens de reden dat de domeinverificatie op hen stukloopt (`STATUS.md`).

### Empirische verificatie (2026-07-25) — twee onafhankelijke tests

**Test 1 — wat geeft Lusha terug op het domein zelf?**

| Domein | Lusha-record | Land |
|---|---|---|
| `skf.com` | SKF Group, Gothenburg | **Zweden** |
| `capgemini.com` | Capgemini, Parijs | **Frankrijk** |
| `jacobs.com` | Jacobs, Dallas | **VS** |
| `coop.it` | Coop Lombardia | Italië |

**Test 2 — komen ze voor in de complete Italiaanse Lusha-set?**
(18.461 in band 51-200 ná het ophalen voorbij het plafond + 9.547 in 200+)

| Gezocht | Treffers |
|---|---|
| SKF | **0** |
| Capgemini | **0** |
| Jacobs | **0** |
| WAM Group | **0** |
| Ferragamo | 1 — `ferragamo.com`, HQ Florence |
| Mercedes-Benz Italia | 1 — `mercedes-benz.it` |

Sluitend bewijs: staat het concern-HQ in het buitenland, dan bestaat het bedrijf
niet in een `country = Italy`-query. Is het HQ Italiaans, dan wordt het gewoon
gevonden.

### CORRECTIE 2026-07-25 (later dezelfde dag): ze zitten wél in Lusha

Bovenstaande conclusie "Lusha kent deze bedrijven niet" is **onjuist gebleken**
bij directe controle. Alle 14 gecontroleerde KvK-only klanten staan in Lusha.
Ze zijn alleen onvindbaar met de gebruikte zoekopdracht, om drie verschillende
redenen.

**Oorzaak A — concern geboekt onder een ander land.** De KvK-entiteit draagt het
concerndomein; Lusha kent dat domein maar onder het moederland.

| KvK-entiteit | domein | Lusha kent het als | land |
|---|---|---|---|
| SKF Automotive Italy / SKF Industrie | `skf.com` | SKF Group | Zweden |
| Capgemini Italia | `capgemini.com` | Capgemini | Frankrijk |
| Jacobs Italia | `jacobs.com` | Jacobs | VS |
| Datwyler Sealing Solutions Italy | `datwyler.com` | Datwyler | Duitsland |

**Oorzaak B — lokaal record bestaat, maar met een absurd laag personeelsaantal.**
Dit is de belangrijkste vondst. Lusha heeft naast het groepsrecord vaak óók een
lokale schil, wél onder Italië, maar vrijwel leeg:

| Domein | Lusha-record | Land | Werknemers |
|---|---|---|---|
| `heinekenitalia.it` | HEINEKEN in Italia | Italië | **1 - 10** |
| `skf.it` | SKF INDUSTRIE SPA | Italië | **1 - 10** |
| `wamgroup.com` | WAMGROUP | VS | **1 - 10** |

Onze pull begon bij 51 werknemers. Deze bedrijven vielen er dus óók langs de
groottekant uit, niet alleen langs het landfilter.

**Oorzaak C — domeinvarianten (matchingfout, geen dekkingsprobleem).**
Van de 7.548 "alleen KvK"-rijen met domein blijkt **13,4% (1.013) wél een
Lusha-record te hebben op hoofddomein**. De master-merge matchte op exact
domein, dus `group.ferragamo.com` (KvK) en `ferragamo.com` (Lusha) golden als
twee verschillende bedrijven.

### Waarom de gemiste KvK-records rijker zijn dan de gemiste Lusha-records

Twee tegengestelde mechanismen — dat verklaart het verschil volledig:

| | wat er wegviel | waardoor | profiel |
|---|---|---|---|
| **8.461 Lusha-records** (opgehaald 25-07) | de staart voorbij het plafond van 10.000 | Lusha sorteert op hoeveel het van een bedrijf weet | **klein/onbekend** — mediaan `exact` 19 tegen 61 bij de eerste 10.000 |
| **KvK-only groep** | Italiaanse dochters van concerns | land- én groottefilter | **groot/kapitaalkrachtig** — mediaan 570 werknemers, €1 mld omzet |

Het plafond snijdt aan de onderkant, het landfilter aan de bovenkant.

### Wat dit praktisch betekent
Dit is **geen databronprobleem maar een zoekstrategieprobleem**, en dus veel
goedkoper op te lossen dan een handelsregister per land inkopen:
1. matchen op hoofddomein in plaats van exact domein (+13,4% dekking, gratis);
2. bij bekende concernnamen niet op land filteren maar op moederbedrijf zoeken;
3. de groottefilter niet vertrouwen voor lokale entiteiten — Heineken Italia
   staat als 1-10 werknemers geregistreerd.

### Het bewijs: KvK-klanten zijn 20x grotere bedrijven
Grootte-data uit HubSpot, klanten ontdubbeld op hoofddomein:

| Bron | n | mediaan werknemers | gemiddeld | mediaan jaaromzet |
|---|---|---|---|---|
| Lusha only | 74 | 200 | 692 | € 50M |
| Beide | 99 | 375 | 1.968 | € 100M |
| **KvK only** | 45 | **570** | **4.998** | **€ 1.000M** |

Daar komt de waardekloof vandaan: grotere bedrijven, grotere trainingsbudgetten.

### Wat dit betekent voor mYngle

> **Methodische correctie (2026-07-25).** Matching op hoofddomein kende
> concernomzet toe aan de Italiaanse entiteit, ook als de mYngle-relatie in een
> ander land loopt. Voorbeeld: `capgemini.com` staat in HubSpot op **Frankrijk**
> (€ 499.117) maar matchte op "CAPGEMINI ITALIA S.P.A." uit het handelsregister.
> **25 van de 218 getelde klanten bleken geen Italiaanse klant** (€ 809.740).
> Alle cijfers hieronder zijn gefilterd op HubSpot `country` = Italy/Italia.

Gecorrigeerd, ontdubbeld op hoofddomein, alleen bevestigd-Italiaanse klanten:

| Bron | n | klanten | omzet/klant | verwachte waarde/bedrijf |
|---|---|---|---|---|
| Lusha only | 12.389 | 69 (0,56%) | € 19.221 | € 107 |
| **KvK only** | 5.952 | 31 (0,52%) | **€ 79.913** | € 416 |
| Beide | 5.947 | 93 (1,56%) | € 28.023 | € 438 |

**Let op — nuance die eerder verkeerd stond:** KvK-only converteert *niet* beter
dan Lusha (0,52% vs 0,56%, praktisch gelijk). De eerdere claim "KvK converteert
even goed én is meer waard" was gebaseerd op de ongecorrigeerde cijfers.
Wat wél robuust is: **de klanten die eruit komen zijn ruim 4x zo groot.**

Van alle Italiaanse klantomzet komt **38,6% van bedrijven die alleen via het
handelsregister vindbaar zijn** (€ 2,48M van € 6,41M), terwijl die groep 24,5%
van het bestand is.

Aan de bovenkant van de markt: **5 van de 10 grootste Italiaanse klanten waren
met Lusha alleen niet vindbaar** —

| Klant | bron | omzet |
|---|---|---|
| SKF Automotive Italy | alleen KvK | € 747.729 |
| Luiss Business School | alleen KvK | € 712.744 |
| Jacobs Italia | alleen KvK | € 224.897 |
| WAM Industriale | alleen KvK | € 221.306 |
| Salvatore Ferragamo | alleen KvK | € 166.023 |

Samen € 2,07M. Niet omdat ze te klein zijn, maar omdat ze te groot en te
internationaal zijn: hun concern staat in Lusha onder een ander land.

**De juiste samenvatting is dus:** de blinde vlek is niet breed maar diep. Het
gaat niet om veel gemiste leads, maar om een klein aantal zeer grote klanten —
31 KvK-klanten leveren 38,6% van de Italiaanse omzet.

**En dit geldt op dit moment voor alle andere landen.** Live gecontroleerd
(2026-07-25): Duitsland, Zwitserland, Nederland en Uruguay hebben het
`channels`-veld volledig leeg — 100% Lusha-brondata, geen enkel handelsregister.
Italië is het enige land waar ooit handelsregisterdata is toegevoegd. De
structurele blinde vlek voor Italiaanse dochters van buitenlandse concerns
bestaat daar dus onverkort — en juist die vormen het meest waardevolle
klanttype (zie sectie 7).

**Conclusie:** een handelsregisterbron toevoegen per land is waarschijnlijk de
grootste onbenutte hefboom die er is — groter dan welke scoringsverbetering ook.
Het scoringsmodel kan alleen rangschikken wat het ziet.

## 8. Belangrijke voorbehouden

- **Label is besmet.** "Is klant/opportunity" correleert met groot-en-vindbaar én
  met "wie is historisch benaderd". Het model leert deels een gelijkenispatroon,
  geen zuiver conversiepatroon.
- **Alleen Italië.** Niet getoetst op andere landen; generalisatie onbekend.
- **Geen tijdsplitsing toegepast** in dit prototype (train/test is willekeurig,
  wel kruisgevalideerd). Voor productie: splitsen op acquisitiedatum.
- **`Lusha Technologies` is leeg** voor alle 27.573 rijen in de master-file,
  ondanks dat de kolom bestaat — nooit uit de API-respons overgenomen.
- Niet gebruikt (kost credits, 1 per bedrijf): `employeesByDepartment` /
  `employeesBySeniority`. Vermoedelijk het meest causale signaal dat er is
  (omvang HR/L&D-afdeling) — gericht ophalen voor de top-N is het overwegen waard.

## 9. Aanbevolen volgorde

Gerangschikt op verwachte opbrengst gedeeld door inspanning.

**0. Boven alles: de zoekstrategie op Lusha repareren** (sectie 7b, correctie).
Niet een handelsregister inkopen — de bedrijven zitten al in Lusha, maar zijn
onvindbaar door exact-domein-matching, het landfilter en onbetrouwbare
groottevelden. Matchen op hoofddomein alleen al levert +13,4% dekking, gratis.
Pas als dat is uitgeput is een tweede databron aan de orde. Lusha mist
structureel de lokale dochters van buitenlandse concerns — juist het meest waardevolle
klanttype. In Italië komt 38,6% van de klantomzet uit die groep, en 5 van de
top-10 klanten (€ 2,07M). Geen enkele scoringsverbetering kan dit compenseren:
het model kan alleen rangschikken wat het ziet.

0b. **De 8.461 zojuist opgehaalde bedrijven verwerken** (voorbij Lusha's
10.000-plafond, al gedownload). Enrichment ~USD 175, daarna export + merge.
Controleer ook of andere landen tegen hetzelfde plafond aanliepen — 1 credit
per land per grootteband om te toetsen.

1. **De 4.671 geparkeerde KvK-bedrijven doorsluizen**
   (sectie 6). Circa USD 96 aan enrichment tegenover € 1,5–3M verwachte waarde.
   Vergt eerst de openstaande beslissing uit `STATUS.md` over de
   domeinverificatiedrempel; die is nu veel beter te onderbouwen.
2. **Subdomein-matching** overal toepassen (hoofddomein i.p.v. exact domein).
   Puur een matchingfix, geen nieuwe data: +4 procentpunt klantdekking. Raakt
   ook de HubSpot-clientsync, die nu te weinig bedrijven als Client markeert.
3. **Nu direct te winnen in de score:** scoringsprofiel zwaarder op
   bedrijfsgrootte + Lusha-omzetklasse. Eén rescore-run.
4. Uitzoeken waarom de enrichment geen 3 meer uitdeelt — herstelt vier signalen
   tegelijk, als je ze wilt behouden.
5. `ti_onboarding_score` / `sig_rapid_growth_score` óf weer produceren óf uit de
   formule halen; nu het slechtste van twee werelden.
6. `sig_foreign_hq_score` niet in gewicht verlagen (zie sectie 7) maar
   **gradueler maken**: nu 0/3 zonder tussenwaarden, en 551 "unclear"-gevallen
   krijgen niets.
7. **Overstappen op ranken naar verwachte waarde** — P(conversie) × E(waarde)
   in plaats van één fit-score. Sectie 7 laat zien waarom dat wezenlijk is.
8. Nieuw model uitrollen; daarna de feedback-loop uit `call_logs` opbouwen. Dát
   wordt op termijn het echte conversiemodel in plaats van een gelijkenismodel.

## 10. Reproduceerbaarheid

Bronnen: `Italy_Companies_Lusha_vs_Chamber_of_Commerce_Master.xlsx` (deze map),
GCS `italy/current/companies.list.json` + `company-details-0NN.json` (40 buckets,
`scoring_inputs.signals`), HubSpot Companies-search op `lifecyclestage`,
`Results1.xlsx` (hoofdmap Nextcloud) voor de oorspronkelijke ontwikkelset.
Domeinmatching met dezelfde normalisatie als
`src/routes/api/hubspot/upsert-company.ts` (lowercase, strip protocol/pad/`www.`).
