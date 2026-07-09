# mYngle Lead Prioritizer — Cloud Run Jobs workflow

Deze handleiding beschrijft de v1-architectuur voor parallelle, cloud-gebaseerde
verwerking van lead-prioritization runs met Google Cloud Run Jobs. Dit document
bevat **geen** deploy-acties — alle `gcloud`-commando's zijn voorbeelden die je
later handmatig uitvoert.

## Doelarchitectuur (simpele tekst)

1. Een gebruiker uploadt één Excel naar een Cloud Storage input-bucket, onder
   het prefix `incoming/`.
2. Een Cloud Storage "object finalized"-event triggert (via Eventarc) de
   dispatcher (`cloud_dispatcher.py`), een kleine Cloud Run service.
3. De dispatcher maakt een `run_id`, bepaalt het aantal tasks (10, 25 of 50,
   afhankelijk van het aantal rijen), schrijft een `manifest.json`, en start
   een Cloud Run Job execution (`cloud_job_runner.py`) met dat aantal tasks.
4. Elke task downloadt dezelfde input-Excel, maar verwerkt alleen haar eigen
   aaneengesloten rijblok (contiguous chunk, geen round-robin — dat maakt
   debuggen makkelijker).
5. Elke task roept `lead_prioritizer_batch_cli.py` aan via subprocess — de
   Lead Prioritizer **v2**-pipeline (Serper + Anthropic + Firecrawl, dezelfde
   die de Streamlit batch-app en `run_batch_dataframe` gebruiken; geen
   herimplementatie van enrichment of scoring) — en schrijft een
   part-output-Excel plus een status-JSON naar Cloud Storage. Company-/
   domain-/(optionele) land-kolom worden automatisch herkend (dezelfde
   detectie als de Streamlit-app), of expliciet gezet via
   `COMPANY_COLUMN`/`DOMAIN_COLUMN`/`INPUT_COUNTRY_COLUMN`.
6. Zodra alle parts klaar zijn, draai je `cloud_merge_results.py` om alle
   part-outputs te combineren tot één finale Excel, gesorteerd op de
   oorspronkelijke rijvolgorde.
7. Status-JSON-bestanden maken op elk moment zichtbaar wat klaar, bezig of
   gefaald is.

Er is in v1 bewust **geen** database, **geen** 50 aparte buckets, **geen**
Kubernetes, en **geen** wijziging aan de bestaande scoringlogica. De
bestaande Streamlit-app (`streamlit_app.py`) blijft ongewijzigd werken — de
cloud-runner roept dezelfde `lead_prioritizer_batch_cli.py` / batch-core aan
die zij ook gebruikt. (De oude `enrich_clients_claude.py`-legacy-CLI wordt
door de cloud-job niet meer aangeroepen — zie "Wijzigingen t.o.v. v1" onderaan.)

## Bucketstructuur

Gebruik één (of twee) buckets met prefixes, geen 50 losse buckets:

```
gs://<input-bucket>/incoming/<bestand>.xlsx

gs://<runs-bucket>/runs/<run_id>/manifest.json
gs://<runs-bucket>/runs/<run_id>/parts/part_0000.xlsx
gs://<runs-bucket>/runs/<run_id>/parts/part_0001.xlsx
gs://<runs-bucket>/runs/<run_id>/status/part_0000_running.json
gs://<runs-bucket>/runs/<run_id>/status/part_0000_done.json
gs://<runs-bucket>/runs/<run_id>/status/part_0000_failed.json
gs://<runs-bucket>/runs/<run_id>/final/<input_stem>_prioritized.xlsx
gs://<runs-bucket>/runs/<run_id>/final/manifest_done.json
gs://<runs-bucket>/runs/<run_id>/logs/
```

`<input-bucket>` en `<runs-bucket>` mogen dezelfde bucket zijn.

## Benodigde Google Cloud services

- **Cloud Storage** — input, part-outputs, status, final output.
- **Cloud Run Jobs** — parallelle worker tasks (`cloud_job_runner.py`).
- **Eventarc** — triggert de dispatcher op nieuwe bestanden onder `incoming/`.
- **Secret Manager** — API keys (Anthropic, Serper, Firecrawl).
- **Artifact Registry** — container image voor Cloud Run.
- **Cloud Logging** — stdout/stderr van elke task, inclusief 429-monitoring.

## Benodigde secrets

- `ANTHROPIC_API_KEY`
- `SERPER_API_KEY`
- `FIRECRAWL_API_KEY`
- `OPENAI_API_KEY` (optioneel)

Zet deze in Secret Manager en koppel ze als env vars aan de Cloud Run Job
(`--set-secrets` bij deploy). Secrets worden nergens gelogd — de runner print
alleen `set`/`missing` per key, nooit de waarde zelf.

## Benodigde env vars

### `cloud_job_runner.py` (worker / task)

| Env var | Betekenis |
|---|---|
| `INPUT_GCS_URI` | `gs://...` pad naar de input-Excel |
| `OUTPUT_GCS_DIR` | `gs://<runs-bucket>/runs/<run_id>` |
| `RUN_ID` | run-identifier |
| `TASK_COUNT` | totaal aantal tasks (fallback als `CLOUD_RUN_TASK_COUNT` ontbreekt) |
| `CLOUD_RUN_TASK_INDEX` | door Cloud Run Jobs automatisch gezet (0-based) |
| `CLOUD_RUN_TASK_COUNT` | door Cloud Run Jobs automatisch gezet |
| `ANTHROPIC_API_KEY` / `SERPER_API_KEY` / `FIRECRAWL_API_KEY` | API keys (nooit als CLI-argument, altijd via env naar de subprocess) |
| `TOTAL_ROW_LIMIT` | optioneel, kapt het INPUT-bestand af tot de eerste N rijen VÓÓRDAT er geshard wordt, zodat N evenredig over alle tasks verdeeld wordt (bv. 100 met `TASK_COUNT=50` → ~2 rijen/task) |
| `MAX_ROWS` | optioneel, beperkt rijen BINNEN de shard die één task al toegewezen kreeg (smoke tests) — anders verwerkt elke task zijn hele shard (`--row-limit 0`); niet hetzelfde als `TOTAL_ROW_LIMIT` hierboven |
| `FORCE_RERUN` | `true` om een bestaande part-output te overschrijven |
| `MODE` | Lead Prioritizer v2 run mode (default `full`); moet een geldige `SUPPORTED_RUN_MODES`-waarde zijn, anders faalt de task direct vóór de "running"-status |
| `COMPANY_COLUMN` / `DOMAIN_COLUMN` | optioneel; anders auto-detectie (zelfde candidate-lijsten als de Streamlit-app) |
| `INPUT_COUNTRY_COLUMN` | optioneel; anders auto-detectie, en anders geen (per-rij land wordt dan aan `default_input_country` overgelaten) |
| `DEEP_DIVE` / `RICH_ICP_CONTEXT` / `AI_SIGNAL_SCORING` | optionele opt-in feature-vlaggen, allemaal standaard uit |
| `DEEP_DIVE_MIN_SCORE` | score-drempel voor de Deep Dive-trigger (default 8.0; alleen relevant als `DEEP_DIVE` aanstaat) |
| `DEEP_DIVE_ON_FOREIGN_HQ` | `false` om de confirmed-foreign-HQ Deep Dive-trigger uit te zetten (default aan) |
| `COMPOSE_CALLER_CONTENT` | opt-in Step 3 caller content via AI (default uit) |
| `C5_ENABLED` | opt-in C5 Sonnet HQ-adjudicatie (default uit) |
| `USE_ENRICHMENT_CACHE` | opt-in gedeelde GCS enrichment-cache (default uit) — zie "Gedeelde enrichment-cache" hieronder |
| `ENRICHMENT_CACHE_BUCKET` | GCS-bucket voor de enrichment-cache (verplicht als `USE_ENRICHMENT_CACHE` aanstaat) |
| `GATE_FULL_ENRICHMENT_ON_FOREIGN_HQ` | opt-in kostengate: goedkope HQ-only screening voor elke rij, volledige v2-enrichment alleen voor bevestigd-buitenlandse HQ's (default uit) — zie "Goedkoper: alleen buitenlandse HQ volledig verrijken" hieronder |
| `CHECKPOINT_EVERY_ROWS` | crash-protectie: elke N verwerkte rijen wordt de tussenstand van déze task geüpload naar `status/part_XXXX_checkpoint.json` (default `5`; `0` zet het uit) — zie "Crash-protectie: tussentijdse checkpoints" hieronder |

`CLOUD_RUN_TASK_INDEX`/`CLOUD_RUN_TASK_COUNT` hebben voorrang zodra beide
gezet zijn (dat doet het platform automatisch); anders vallen we terug op
`--task-index`/`--task-count` (CLI) of `TASK_COUNT` (env), voor lokaal testen.

### `cloud_merge_results.py`

| Env var | Betekenis |
|---|---|
| `RUN_ID` | run-identifier |
| `OUTPUT_GCS_DIR` | zelfde `runs/<run_id>` map als de tasks |
| `EXPECTED_TASK_COUNT` | verwacht aantal tasks (fail-fast bij mismatch); dit is het aantal tasks dat een status-JSON moet hebben geschreven, **niet** het aantal part-bestanden — een task met een leeg rijblok (`TASK_COUNT` > aantal rijen) schrijft een `done`-status maar nooit een part-bestand, en telt hier gewoon mee |
| `FINAL_OUTPUT_NAME` | optioneel, default `lead_prioritizer_final.xlsx` |

### `cloud_dispatcher.py` (service)

| Env var | Betekenis |
|---|---|
| `CLOUD_RUN_JOB_NAME` | naam van de Cloud Run Job |
| `CLOUD_RUN_REGION` | bv. `europe-west1` |
| `CLOUD_RUN_PROJECT` | GCP project-id |
| `RUNS_GCS_DIR` | `gs://<runs-bucket>` (default: dezelfde bucket als de input) |
| `DEFAULT_TASK_COUNT` | fallback task count als rijen niet geteld konden worden (default 10 — de veiligste tier, want als tellen faalt is het bestand al verdacht) |

Parallelism is bewust géén dispatcher-instelling: de Cloud Run v2
execution-overrides ondersteunen wel `task_count` maar geen `parallelism` —
een executie draait dus altijd met de `--parallelism` van de job-deploy.

## Lokale testcommando's

Sharding-unittests (geen live API's, geen echte keys):

```bash
python -m pytest tests/test_cloud_sharding.py -v
```

Syntax-check van de nieuwe cloud-bestanden:

```bash
python -m py_compile cloud_job_runner.py cloud_merge_results.py cloud_dispatcher.py
```

Lokale smoke test met een echte cleaned input (kleine steekproef, geen volledige
batch):

```bat
python cloud_job_runner.py ^
  --input "C:\Users\gmeijer4\Nextcloud\Myngle\Switzerland\Switzerland500_cleaned.xlsx" ^
  --output-dir ".\cloud_smoke_output" ^
  --task-index 0 ^
  --task-count 10 ^
  --max-rows 5
```

Doel van deze test:
- controleren dat de runner een echte cleaned Excel kan lezen;
- controleren dat row sharding werkt;
- controleren dat task 0 alleen zijn eigen rijblok verwerkt;
- controleren dat part-output en status-JSON worden geschreven;
- **geen** volledige batch draaien.

Als dit bestand niet bestaat op de machine waar je test, sla deze stap over —
gebruik dan een kleine synthetic Excel (zie `tests/test_cloud_sharding.py`) om
alleen de sharding-logica te verifiëren.

Lokale merge-test (na een lokale sharding-run met bv. `--task-count 2`):

```bash
python cloud_merge_results.py --output-dir .\cloud_smoke_output --expected-task-count 2
```

Lokale dispatcher-smoke test (zonder echte GCS/Eventarc, alleen event-parsing
en manifest-schrijven testen — de Cloud Run Job execution-call faalt dan netjes
met `started: false` als `CLOUD_RUN_JOB_NAME` e.a. niet gezet zijn):

```bash
uvicorn cloud_dispatcher:app --host 0.0.0.0 --port 8080
```

## Docker build voorbeeld

```bash
docker build -t myngle-lead-prioritizer-cloud:latest .
```

De dispatcher gebruikt dezelfde image met een ander commando:

```bash
docker run myngle-lead-prioritizer-cloud:latest \
  uvicorn cloud_dispatcher:app --host 0.0.0.0 --port 8080
```

## gcloud deploy-voorbeelden (niet uitvoeren — alleen documentatie)

Artifact Registry + image push:

```bash
gcloud builds submit --tag europe-west1-docker.pkg.dev/PROJECT_ID/myngle/lead-prioritizer-cloud:latest
```

Cloud Run Job deployen:

```bash
gcloud run jobs deploy myngle-lead-prioritizer \
  --image europe-west1-docker.pkg.dev/PROJECT_ID/myngle/lead-prioritizer-cloud:latest \
  --region europe-west1 \
  --set-secrets ANTHROPIC_API_KEY=anthropic-api-key:latest,SERPER_API_KEY=serper-api-key:latest,FIRECRAWL_API_KEY=firecrawl-api-key:latest \
  --tasks 10 \
  --parallelism 10 \
  --max-retries 1 \
  --task-timeout 3600 \
  --memory 2Gi
```

`--memory 2Gi` is bewust ruimer dan de Cloud Run-default (512Mi): elke task
draait pandas + openpyxl in de runner én een tweede Python-proces
(`lead_prioritizer_batch_cli.py`) met de volledige pipeline er bovenop —
houd het werkelijke verbruik in de metrics in de gaten voordat je dit
verlaagt.

Cloud Run Job execution starten met task count en parallelism (handmatige test,
of wat de dispatcher automatisch doet via de `google-cloud-run` client):

```bash
gcloud run jobs execute myngle-lead-prioritizer \
  --region europe-west1 \
  --tasks 25 \
  --update-env-vars INPUT_GCS_URI=gs://myngle-input/incoming/klant.xlsx,OUTPUT_GCS_DIR=gs://myngle-runs/runs/20260101_120000_klant,RUN_ID=20260101_120000_klant,TASK_COUNT=25
```

Dispatcher als Cloud Run **service** deployen (het service-account waaronder
dit draait heeft `roles/run.developer`, of een rol met minimaal
`run.jobs.run`, nodig — anders faalt `start_cloud_run_job_execution()` in
`cloud_dispatcher.py` met een permission-error zodra hij een job probeert te
starten):

```bash
gcloud run deploy myngle-dispatcher \
  --image europe-west1-docker.pkg.dev/PROJECT_ID/myngle/lead-prioritizer-cloud:latest \
  --region europe-west1 \
  --command uvicorn \
  --args cloud_dispatcher:app,--host,0.0.0.0,--port,8080 \
  --set-env-vars CLOUD_RUN_JOB_NAME=myngle-lead-prioritizer,CLOUD_RUN_REGION=europe-west1,CLOUD_RUN_PROJECT=PROJECT_ID,RUNS_GCS_DIR=gs://myngle-runs
```

Eventarc-trigger voor Cloud Storage "object finalized" (voorbeeld):

```bash
gcloud eventarc triggers create myngle-incoming-trigger \
  --location europe-west1 \
  --destination-run-service myngle-dispatcher \
  --destination-run-region europe-west1 \
  --event-filters type=google.cloud.storage.object.v1.finalized \
  --event-filters bucket=myngle-input \
  --service-account EVENTARC_SA@PROJECT_ID.iam.gserviceaccount.com
```

Merge-job draaien na afloop (handmatig of als vervolgstap in een orkestratie
die je later toevoegt):

```bash
python cloud_merge_results.py \
  --run-id 20260101_120000_klant \
  --output-dir gs://myngle-runs/runs/20260101_120000_klant \
  --expected-task-count 25
```

## Output terugvinden

- Part-outputs: `gs://<runs-bucket>/runs/<run_id>/parts/part_XXXX.xlsx`
- Status per task: `gs://<runs-bucket>/runs/<run_id>/status/part_XXXX_{running,done,failed}.json`
- Finale Excel: `gs://<runs-bucket>/runs/<run_id>/final/lead_prioritizer_final.xlsx`
  (of de naam die je via `FINAL_OUTPUT_NAME`/`--final-output-name` opgeeft) —
  bevat alle bladen die elke task ook al had (Enriched Leads, Evidence,
  Signals, Run Summary, en Deep Dive indien niet leeg), niet alleen Enriched
  Leads. `source_index` in Evidence/Signals/Deep Dive wordt bij het mergen
  herschreven naar de run-brede `_cloud_original_row_index`, zodat die rijen
  na het samenvoegen van meerdere tasks nog steeds bij het juiste bedrijf
  horen — anders begint elke task lokaal weer bij `source_index=0` en zou de
  Lovable-export (die op `source_index` matcht) door elkaar lopen.
- Merge-manifest: `gs://<runs-bucket>/runs/<run_id>/final/manifest_done.json`

## Gecombineerd API-verbruik + cache-hitrapport

Elke task's `lead_prioritizer_batch_cli.py`-subprocess schrijft zijn eigen
`usage_tracker`-snapshot (Serper/Anthropic/Firecrawl-calls, tokens,
cache-hits/misses, geschatte kosten) als JSON weg via `--usage-output`;
`cloud_job_runner.py` leest dat bestand en voegt het toe onder `"usage"` in
de `status/part_XXXX_done.json` van die task. De Cloud Run Streamlit-pagina
telt na de merge alle `_done.json`-bestanden bij elkaar op
(`usage_tracker.merge_snapshots`) en toont één samengevoegd rapport — inclusief
de cache-hitrate per bron (`serper`/`firecrawl`), zodat je in één oogopslag
ziet hoeveel credits een herhaalde run daadwerkelijk heeft bespaard. Ontbreekt
`"usage"` voor een task (bv. een ouder gedeployed image zonder
`--usage-output`-ondersteuning), dan wordt die task's data gewoon overgeslagen
in plaats van de rest van het rapport te breken.

## Hoe retries werken

- Elke task is idempotent: als `parts/part_XXXX.xlsx` al bestaat en
  `FORCE_RERUN` niet `true` is, slaat de task verwerking over en schrijft een
  `done`-status met `status: "skipped"`.
- Cloud Run Jobs kan losse gefaalde tasks automatisch retrien
  (`--max-retries`); dankzij de idempotentie-check hierboven verwerkt een
  retry nooit dubbel als de part al klaar staat.
- `cloud_merge_results.py` faalt expliciet en duidelijk als het aantal tasks
  dat een status-JSON heeft geschreven (`done` + `failed` samen) niet
  overeenkomt met `EXPECTED_TASK_COUNT`, of als er één of meer `failed`-
  statussen tussen zitten — dat is het signaal om losse tasks opnieuw te
  draaien voordat je merget. Tasks met een leeg rijblok (`done`, geen part-
  bestand) tellen gewoon mee als "gerapporteerd" en blokkeren de merge niet.

## Crash-protectie: tussentijdse checkpoints

Zonder checkpoints verwerkt elke task zijn hele rijblok volledig in het
geheugen en schrijft pas ÉÉN keer, aan het eind, een outputbestand. Crasht de
task halverwege (OOM-kill, onverwachte exception) dan is ALLES wat die task
al verwerkt had verloren — niet alleen de rij die het probleem veroorzaakte.
Bij bv. 3000 rijen / 50 tasks (~60 rijen/task, zie "Hoe sharding werkt")
betekent dat in het slechtste geval maximaal ~60 bedrijven kwijt, niet de
hele 3000 — maar zonder checkpoints was zelfs die ~60 volledig weg.

Met `CHECKPOINT_EVERY_ROWS` (default `5`, zet op `0` om uit te schakelen)
schrijft `lead_prioritizer_batch_cli.py` elke N verwerkte rijen de tussenstand
lokaal weg (`batch_checkpoint.py`, atomisch — nooit een corrupt half-geschreven
bestand). `cloud_job_runner.py` uploadt die tussenstand op zijn beurt periodiek
(elke ~20 seconden, alleen als het bestand gewijzigd is) naar
`status/part_XXXX_checkpoint.json`, plus altijd nog één laatste keer meteen
nadat de subprocess stopt (succes of fout) — dus ook een checkpoint die net
vlak vóór het einde geschreven werd, wordt niet gemist.

Bij een crash bevat `status/part_XXXX_failed.json` een `checkpoint_uri`-veld
met de locatie van de laatst geüploade checkpoint (of `null` als er nog geen
enkele rij verwerkt was). Dat bestand bevat de tot dan toe verwerkte
`enriched_rows`/`evidence_rows`/`signal_rows` als platte JSON — geen kant-en-
klaar Excel-bestand, en de merge-stap gebruikt het (nog) niet automatisch.
Voor nu is het vooral bedoeld om handmatig te kunnen inspecteren wat een
gecrashte task al had verwerkt, in plaats van dat die data spoorloos
verdwijnt; automatisch salvagen in `cloud_merge_results.py` is een mogelijke
vervolgstap.

## Eerste veilige instellingen

- `TASK_COUNT=10` voor de eerste test.
- `TASK_COUNT=25` voor de tweede test.
- `TASK_COUNT=50` voor een productieachtige test.
- Parallelism staat vast op de `--parallelism` van de job-deploy (executies
  kunnen hem niet overriden); zet hem bij deploy gelijk aan het hoogste
  `TASK_COUNT` dat je van plan bent, tot maximaal 50.
- Let op: het aantal tasks van een executie bepaal je met `--tasks` op
  `gcloud run jobs execute` (dat doen `run_cloud_lead_prioritizer.ps1` en de
  Cloud Run Streamlit-app inmiddels ook). Alleen de `TASK_COUNT` env var
  zetten is niet genoeg: Cloud Run zet zelf `CLOUD_RUN_TASK_COUNT` op het
  deploy-aantal en dat heeft voorrang in de runner.

## Rate-limit notities

- Serper Standard (100 q/s) is ruim voldoende, ook bij `TASK_COUNT=50`.
- Anthropic Haiku Scale-tier is ruim volgens de huidige console-screenshot,
  maar blijf 429's meten in Cloud Logging — dit is geen garantie, alleen een
  momentopname.
- Firecrawl zit nu daadwerkelijk in het hot path (de v2-pipeline gebruikt
  Firecrawl voor de eigen-domein-crawl, en voor Deep Dive/Public Source
  Signal Enrichment als die opt-ins aanstaan) — dit was in de vorige versie
  van dit document nog theoretisch, omdat de cloud-job toen de oude
  Firecrawl-loze `enrich_clients_claude.py` aanriep. Elke task verwerkt zijn
  shard sequentieel (één bedrijf tegelijk, geen in-task threadpool), dus de
  gelijktijdige Firecrawl-load op elk moment is ongeveer `TASK_COUNT`
  requests tegelijk (× de paar candidate-paths die één bedrijf soms na
  elkaar probeert, maar dat is nooit echt gelijktijdig binnen één task).
  Firecrawl Hobby (5 concurrent / 100 scrapes/min) is dus al snel de
  bottleneck bij `TASK_COUNT` boven ~5; Firecrawl Standard (50 concurrent /
  500 scrapes/min) past beter bij `TASK_COUNT=25–50`.
- Een 429 van Firecrawl of Serper wordt sinds deze wijziging een paar keer
  met backoff geretried (`api_retry.py`, gebruikt door
  `deep_dive_runner._firecrawl_scrape_page` en
  `lead_hq_ai_interpreter.call_serper_for_hq` /
  `lead_non_hq_enrichment.call_serper_for_enrichment`) in plaats van de hele
  crawl voor die lead meteen op te geven — dit maakt korte pieken onder
  concurrent load draaglijk, maar is geen vervanging voor een verstandige
  `TASK_COUNT` t.o.v. de Firecrawl-tier: er is geen gedeelde/verdeelde
  rate-limiter over tasks heen, dus een structureel te hoge `TASK_COUNT`
  leidt gewoon tot herhaalde 429's die alsnog uitputten.

## Goedkoper: alleen buitenlandse HQ volledig verrijken

Als het doel van een run is "geef me alleen bedrijven met een buitenlands
hoofdkantoor" (bv. per land een foreign-HQ-lijst), is de default `mode=full`
+ "Foreign-HQ-only export" combinatie duurder dan nodig: elke rij doorloopt
dan de VOLLEDIGE v2-pipeline (HQ + non-HQ evidence via Serper/Firecrawl +
AI-signalen + score + caller-content), en pas bij de Lovable-JSON-export ná
de merge worden de niet-buitenlandse rijen eruit gefilterd. Je betaalt dus
voor de hele batch, ook voor de bedrijven die achteraf toch wegvallen.

Zet `GATE_FULL_ENRICHMENT_ON_FOREIGN_HQ=true` (CLI: `--gate-full-enrichment-`
`on-foreign-hq`; Streamlit — kies "Alleen buitenlands HQ" bij de "Scope"-vraag)
om dat om te draaien, met de bestaande `mode=full` (of elke andere mode):

> **Scope is één keuze, geen twee losse vinkjes.** De Streamlit-app had
> hiervoor twee onafhankelijke instellingen: de kostengate hierboven én een
> apart "Foreign-HQ-only export"-vinkje bij de Lovable-export. Die konden uit
> elkaar lopen — bv. de gate aan (dus goedkoop screenen + alleen buitenlandse
> rijen volledig verrijken) maar de export-filter uit, waardoor bevestigd
> *binnenlandse* bedrijven (bv. "Molins") alsnog in de gepubliceerde
> `current/`-lijst terechtkwamen. De "Scope"-radio ("Alle bedrijven" /
> "Alleen buitenlands HQ") stuurt nu beide tegelijk aan: `foreign_hq_only_`
> `export` wordt rechtstreeks van dezelfde keuze afgeleid, dus de gate en de
> export-filter kunnen niet meer los van elkaar staan.

1. **Fase 1/2 — screening**: elke rij krijgt een goedkope HQ-only check (1
   Serper-call per bedrijf), net als `mode=hq_only`. Staat `C5_ENABLED`/
   `--c5-enabled` óók aan, dan draait C5 Sonnet-adjudicatie HIER al — vóór de
   gate-beslissing, exact zoals in de losse `full_foreign_hq_only`-modus —
   over dezelfde score-3/manual-review-rijen als normaal (geen extra
   API-kosten t.o.v. C5 los aanzetten).
2. **Fase 3 — volledige enrichment**: alleen rijen die confirmed foreign-HQ
   zijn (plain HQ-score == 3, óf — als C5 aanstaat — een C5-bevestigde
   buitenlandse parent) doorlopen de rest van de pipeline (de overige ~4
   Serper-calls plus Firecrawl/Anthropic). Omdat C5 al in Fase 1/2 heeft
   meegewogen, wordt een grensgeval dat de plain HQ-score alleen niet had
   herkend, maar dat C5 wél als buitenlands bevestigt, gewoon volledig
   verrijkt — niet alleen van C5-velden voorzien. Niet-bevestigde rijen
   blijven in de output staan met `enrichment_skipped=True` en een
   `enrichment_skip_reason`, maar kosten verder niets.

Dit is dezelfde tweefasige aanpak (inclusief C5-plek) als de losse
`full_foreign_hq_only`-modus in de lokale Streamlit-app, maar dan als opt-in
bovenop de gewone `SUPPORTED_RUN_MODES` — dus bruikbaar op het Cloud
Run-pad, waar `full_foreign_hq_only` zelf niet beschikbaar is.

De run-summary krijgt met deze gate drie extra kolommen:
`gated_full_enrichment_attempted_count`, `gated_full_enrichment_skipped_count`
en `gated_estimated_serper_calls_saved` (skipped × 4) — zo zie je per run
hoeveel er daadwerkelijk bespaard is. Staat C5 ook aan, dan krijg je
daarnaast de gebruikelijke `c5_*`-tellingen, gebaseerd op dezelfde Fase 1/2
C5-pass (geen dubbele C5-run).

## Opnieuw draaien van hetzelfde bestand — wat overschrijft, wat niet

- **Cloud Run-output (`runs/<run_id>/...`)**: nooit een botsing. Elke klik op
  "Start Cloud Run" genereert een nieuwe `run_id` op basis van het huidige
  tijdstip (`build_run_id`), dus elke run krijgt zijn eigen map — een
  herhaalde run van hetzelfde bestand overschrijft nooit een eerdere run z'n
  part-/status-/final-bestanden.
- **`incoming/<bestand>.xlsx`**: wordt bij elke upload stilzwijgend
  overschreven (`gcloud storage cp`, geen check). Onschuldig voor de
  Streamlit-app zelf (die het lokale bestand direct gebruikt, niet de
  GCS-kopie), maar relevant voor het Eventarc/dispatcher-pad.
- **Lovable-export `current/`**: expliciete keuze in het hoofdveld (niet de
  sidebar) vlak vóór de "Start Cloud Run"-knop, zodat hij niet gemist kan
  worden — **Overschrijven** (current/ volledig vervangen door alleen deze
  run, het oude gedrag) of **Mergen** (samenvoegen met wat er al staat, zie
  hieronder). Default blijft Overschrijven, dus bestaand gedrag verandert
  niet vanzelf. Alleen zichtbaar zodra én "Na afloop automatisch Lovable
  JSON exporteren" én "Na Lovable JSON-export uploaden naar Google Cloud
  Storage" (beide in de sidebar) aanstaan — current/ helemaal overslaan kan
  alleen nog door die laatste checkbox uit te zetten (schakelt dan ook het
  archief-uploaden uit).
- **Lovable-export archief (`runs/<run_folder>/`)**: dit IS bedoeld als
  permanent historisch record (altijd de onvermengde export van precies
  déze run, ook als current/ op Merge staat). Het "GCS run folder"-veld in
  de sidebar staat standaard op `<datum>_<mode>_<tijd>` — de tijd (HHMMSS)
  is bewust toegevoegd bovenop `lovable_gcs_upload.default_gcs_run_folder()`'s
  eigen `<datum>_<mode>` (dat blijft de standaard voor de lokale batch-app,
  hier niet aangepast), zodat twee runs op dezelfde dag/mode NOOIT meer
  automatisch naar dezelfde archiefmap wijzen. Vóór de Streamlit-app de dure
  Cloud Run Job start, checkt hij nog steeds of de (eventueel handmatig
  aangepaste) archiefmap al bestanden bevat, en toont dan een waarschuwing
  met de bestandslijst plus een bevestig-checkbox ("Ja, ik wil de bestaande
  archiefdata overschrijven") — pas na aanvinken start de run. Door de
  standaard-uniek-per-run-naam komt dit nu vrijwel alleen nog voor als je
  het veld zelf bewust hergebruikt (bv. om meerdere deelruns van dezelfde
  dag in één archiefmap te bundelen). Zonder conflict verandert er niets
  (geen extra klik nodig). Deze check zit alleen in de Streamlit-app (waar
  een mens de vraag kan beantwoorden), niet in de dispatcher/Eventarc-flow,
  die headless draait.

## current/ mergen in plaats van overschrijven

Kies je in het hoofdveld voor **Mergen**, dan wordt de bestaande
`current/`-data eerst gedownload en samengevoegd met de output van deze
run, in plaats van vervangen:

1. `companies.list.json` + alle `company-details-*.json` uit
   `gs://<bucket>/<land>/current/` worden gedownload (ontbreken ze nog —
   eerste run voor dit land — dan start de merge gewoon vanaf leeg, geen
   foutmelding).
2. Per bedrijf (`company_id`, domein-gebaseerd en dus stabiel over runs
   heen — zie `make_company_id`) geldt: de NIEUWE run wint, BEHALVE als de
   nieuwe rij `enrichment_skipped` is (bv. door de foreign-HQ-kostengate of
   een goedkopere mode) terwijl de bestaande rij dat niet was — dan blijft
   de rijkere bestaande versie staan. Zo kan een kleine/goedkope testrun
   nooit per ongeluk een eerder volledig verrijkt bedrijf downgraden.
   Bedrijven die maar aan één kant voorkomen blijven altijd staan.
3. `assigned_cold_caller`/`assigned_cold_caller_rank` worden NOOIT aangepast
   tijdens een merge — een bedrijf dat al bij een caller stond, blijft daar
   staan, ook als de rest van zijn data wordt bijgewerkt.
4. Bestaande bedrijven behouden hun positie in de lijst (inhoud wordt
   ge-update, niet verplaatst); nieuwe bedrijven worden achteraan
   toegevoegd. Daardoor blijft elk al-bestaand bedrijf in hetzelfde
   `company-details-XXX.json`-bucketbestand zitten na een merge (zolang de
   bucket size niet verandert) — alleen de laatste, aangroeiende bucket en
   eventuele nieuwe buckets wijzigen.
5. Het geüploade `export_manifest.json` in `current/` krijgt er een
   `merge_summary`-blok bij: `added` / `updated` / `kept_richer_existing` /
   `companies_before` / `total_after`, ook zichtbaar in de Streamlit-app
   direct na de merge.

Het archief (`runs/<run_folder>/`) gebruikt hierbij altijd de onvermengde
export van déze run — nooit de gemergde current/-set — zodat elke
archiefmap precies weergeeft wat die ene run zelf heeft opgeleverd.

**Belangrijk: mergen bespaart op zichzelf geen enkele API-kost.** De Cloud
Run Job verwerkt gewoon alle rijen uit het geüploade invoerbestand door de
volledige pipeline (Serper/Anthropic/Firecrawl) — die stap weet niets van
wat er al in `current/` staat. De `merge_summary`-cijfers (`added`/
`updated`) beschrijven alleen hoe de output van déze run zich verhoudt tot
wat er al gepubliceerd was, niet hoeveel er dit keer daadwerkelijk
verrijkt is — een run met `updated: 100` heeft dus gewoon opnieuw 100
bedrijven door de hele pipeline gehaald, niet 100 "gratis" hergebruikt.

### Skip-filter: al verrijkte bedrijven vooraf overslaan

Om wél kosten te besparen op een herhaalde run, staat er — alleen zichtbaar
bij **Mergen**, en daar standaard AAN — een extra checkbox: "Bedrijven die
al volledig verrijkt in current/ staan overslaan" (bewust default aan: een
Mergen-run zonder deze filter verwerkt gewoon alle rijen opnieuw, inclusief
bedrijven die al in `current/` stonden — dat overkwam de gebruiker
herhaaldelijk toen de checkbox nog default uit stond). Staat die aan, dan
gebeurt er vóórdat de Cloud Run Job start:

1. De bestaande `current/companies.list.json` wordt gedownload.
2. Elk bedrijf daarin met `enrichment_skipped=False` (dus écht volledig
   verrijkt, geen dunne foreign-HQ-gate-rij) levert een bekende
   `company_id` op (domein-gebaseerd, dezelfde normalisatie als
   `make_company_id`/`slugify`).
3. Rijen in het invoerbestand wiens domein al bij een bekende `company_id`
   hoort, worden VOOR de upload uit het bestand gefilterd — die rijen gaan
   dus nooit door Serper/Anthropic/Firecrawl. Een rij zonder (herkenbare)
   domein wordt nooit overgeslagen, uit voorzichtigheid.
4. Het invoerbestand dat daadwerkelijk naar Cloud Run gaat bevat dus alleen
   nog de nieuwe/nog-niet-verrijkte rijen — de Streamlit-app toont vooraf
   hoeveel rijen zijn overgeslagen en hoeveel er verwerkt worden. Staat er
   ook een "Row limit (totaal)" ingesteld, dan houdt deze melding daar
   rekening mee: **die rijlimiet wordt pas ná deze upload, server-side in
   de Cloud Run Job zelf toegepast**, dus het getoonde aantal is het
   werkelijke aantal ná die limiet, niet het (soms veel hogere) aantal
   onbekende rijen vóór de limiet. Zijn het er nul, dan wordt de Cloud Run
   Job niet eens gestart.
5. Ná de run vult de gewone merge-stap de overgeslagen bedrijven gewoon
   weer aan vanuit de bestaande `current/`-data (ze komen niet voor in de
   nieuwe run z'n output, dus ze vallen in de "alleen aan de oude kant"-tak
   van de merge en blijven ongewijzigd staan).

Dit is de daadwerkelijke kostenbesparing bij een herhaalde run — niet de
merge op zich. Wil je juist een bedrijf dat eerder door de foreign-HQ-gate
is overgeslagen alsnog een nieuwe kans geven, dan gebeurt dat automatisch:
alleen `enrichment_skipped=False`-bedrijven tellen als "al verrijkt", dus
een eerder dunne entry wordt gewoon weer meegenomen.

### Het "binnenlands bedrijf"-gat, en de screened_domains-ledger

Met "Foreign-HQ-only export" aan komt een bedrijf met een **binnenlands**
HQ nooit in `current/` terecht — `detect_foreign_hq_for_export` filtert
het er sowieso uit, of het nu door de gate goedkoop is gescreend
(`enrichment_skipped=True`) óf zelfs volledig verrijkt is maar gewoon
binnenlands bleek. De skip-filter hierboven, die alléén `current/` leest,
heeft dus **geen enkel record** van zulke bedrijven — ze worden bij elke
herhaalde run gewoon weer opnieuw gescreend (een nieuwe Serper-call plus
een nieuwe Anthropic HQ-interpretatiecall, geen van beide gedekt door
`enrichment_cache.py`, dat bewust nooit een afgeleid verdict opslaat, alleen
ruwe responses).

`screened_domains_ledger.py` lost dit apart op: een eigen, altijd-compleet
GCS-bestand per land (`gs://<bucket>/<land>/_screened_domains/`), volledig
los van wat er uiteindelijk in `current/` belandt:

- **Bijwerken**: na elke merge (stap 4c) worden alle rijen uit het
  samengevoegde "Enriched Leads"-sheet doorgelopen — ongeacht of deze run
  de skip-filter gebruikt, en ongeacht `foreign_hq_only_export`. Alleen
  rijen met een **definitief settled, ondubbelzinnig binnenlands** verdict
  worden vastgelegd (`is_clearly_domestic`: score exact 0, geen
  `needs_manual_review`/`hq_positive_score_suppressed_for_review`, en als
  C5 heeft meegedraaid alleen bij een expliciete
  `c5_adjudication="domestic_confirmed"` — een "unclear"/gefaalde
  C5-uitkomst of een score van 1/2 telt bewust niet als settled, zodat een
  latere run (met C5 aan, of verse evidence) het bedrijf alsnog een eerlijke
  kans geeft). Geen TTL — een HQ-locatie veroudert in de praktijk niet.
- **Raadplegen**: de skip-filter downloadt deze ledger ALLEEN als
  "Foreign-HQ-only export" voor déze run ook aanstaat (anders hoort een
  binnenlands bedrijf gewoon in de output, en moet het gewoon verwerkt
  worden), en telt een match daarin mee als "al bekend, overslaan" —
  bovenop de bestaande `current/`-gebaseerde check.
- Zelfde GCS-transport en concurrency-veiligheid als `enrichment_cache.py`
  (Python-client eerst, CLI-fallback, read-merge-write met een
  generation-precondition) — maar bewust een APART bestand/module, om
  `enrichment_cache.py`'s "nooit een afgeleid verdict, alleen ruwe
  responses"-contract niet te doorbreken.

## Gedeelde enrichment-cache in cloud-runs

De opt-in Serper/Firecrawl-cache (`USE_ENRICHMENT_CACHE` +
`ENRICHMENT_CACHE_BUCKET`, zie `enrichment_cache.py`) is één JSON-index per
land op GCS. In een cloud-run draaien 10–50 tasks parallel dezelfde
load→use→save-cyclus op diezelfde index; daarom overschrijft een save
nooit blind, maar merget hij met de actuele remote index (per key wint de
nieuwste `fetched_at`) en schrijft hij met een GCS
generation-preconditie (compare-and-swap met retries). Twee parallelle
tasks kunnen elkaars entries dus niet meer wissen. Kanttekening: tasks
binnen één run profiteren nauwelijks van elkaars *nieuwe* entries (ze
laden de index allemaal bij hun start) — de winst zit in volgende runs
over dezelfde bedrijven/landen.

## Twee ingangen, twee default-profielen

Let op het verschil in feature-defaults tussen de twee manieren om een run
te starten:

- **Eventarc → dispatcher**: de job-executie krijgt alleen
  `INPUT_GCS_URI`/`OUTPUT_GCS_DIR`/`RUN_ID`/`TASK_COUNT` als override; alle
  feature-vlaggen komen uit de env-config van de job-deploy. Zonder daar iets
  te zetten draait dit pad dus met alles uit (geen caller content, geen
  rich ICP, geen AI-signaalscoring, geen C5, geen cache, mode `full`).
- **Streamlit-app / PowerShell-script**: de Streamlit-app zet expliciete
  overrides mee en heeft de meeste opties standaard áán (zoals de lokale
  batch-app); het PowerShell-script geeft alleen `MODE` mee.

Hetzelfde bestand via `incoming/`-upload versus via de Streamlit-app kan
daardoor andere output (en kosten) opleveren. Wil je het Eventarc-pad
gelijktrekken, bak de gewenste vlaggen dan in de job-deploy
(`--set-env-vars` bij `gcloud run jobs deploy`).

## Bekende beperkingen (v1)

- Geen database — status en manifests zijn platte JSON-bestanden op GCS.
- Geen 50 aparte buckets — alles loopt via prefixes in één of twee buckets.
- Geen Kubernetes.
- Geen wijziging aan de bestaande scoringlogica in `commercial_fit_scoring.py`.
- Streamlit (`streamlit_app.py`) blijft ongewijzigd bestaan en werken.
- De dispatcher's Cloud Run Job execution-call (`google-cloud-run` v2 client)
  is inmiddels end-to-end getest: `cloud_dispatcher.py` gedeployed als
  authenticated Cloud Run service, aangeroepen met een gesimuleerd GCS
  "object finalized"-event, en die triggerde succesvol een echte Cloud Run
  Job execution (10 tasks, 3 rijen, 0 errors/429's, merge geslaagd). Vereist
  wel dat het service-account van de dispatcher `roles/run.developer` (of
  gelijkwaardig, incl. `run.jobs.run`) heeft — dat stond niet standaard aan.
  Het Eventarc-triggerpad zelf (Cloud Storage "object finalized" → dispatcher)
  is nog niet end-to-end getest, alleen de directe HTTP-aanroep.
- Geen gedeelde/distributed concurrency-cap over Cloud Run-tasks heen — alleen
  per-call 429-retry/backoff (zie Rate-limit notities). Bij een te hoge
  `TASK_COUNT` t.o.v. de Firecrawl-tier leidt dat tot herhaalde, uiteindelijk
  uitgeputte retries in plaats van een nette wachtrij.

## Wijzigingen t.o.v. v1

- De cloud-job draait sinds deze wijziging `lead_prioritizer_batch_cli.py`
  (Lead Prioritizer v2, incl. Firecrawl) in plaats van de oude
  `enrich_clients_claude.py`-legacy-CLI (alleen Serper + Claude web_search,
  geen Firecrawl) — cloud-runs leveren nu dezelfde output als lokale/
  Streamlit-runs, in plaats van een apart, ouder enrichment-format.
- Company-/domain-/land-kolom worden automatisch herkend (of expliciet gezet
  via `COMPANY_COLUMN`/`DOMAIN_COLUMN`/`INPUT_COUNTRY_COLUMN`) in plaats van
  ongebruikt te blijven.
- `MODE` wordt nu daadwerkelijk doorgegeven aan de v2-pipeline (met
  validatie tegen `SUPPORTED_RUN_MODES`) in plaats van alleen gelogd te
  worden.
- Firecrawl/Serper 429's worden nu een paar keer met backoff geretried in
  plaats van de crawl voor die lead meteen als hard failure af te schrijven
  (zie Rate-limit notities hierboven).
- `cloud_merge_results.py` telt sinds de eerste echte end-to-end-test tegen
  Cloud Run status-bestanden (`done`/`failed`) in plaats van part-bestanden
  om `EXPECTED_TASK_COUNT` te verifiëren. Met de eerdere part-telling faalde
  elke merge zodra `TASK_COUNT` groter was dan het aantal rijen (bv. de
  aanbevolen `TASK_COUNT=10` tegen een testbestand van een paar rijen) — een
  taak met een leeg rijblok schrijft namelijk nooit een part-bestand, alleen
  een `done`-status.
