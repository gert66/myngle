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
5. Elke task roept de bestaande `enrich_clients_claude.py` CLI aan via
   subprocess (geen herimplementatie van enrichment of scoring) en schrijft
   een part-output-Excel plus een status-JSON naar Cloud Storage.
6. Zodra alle parts klaar zijn, draai je `cloud_merge_results.py` om alle
   part-outputs te combineren tot één finale Excel, gesorteerd op de
   oorspronkelijke rijvolgorde.
7. Status-JSON-bestanden maken op elk moment zichtbaar wat klaar, bezig of
   gefaald is.

Er is in v1 bewust **geen** database, **geen** 50 aparte buckets, **geen**
Kubernetes, en **geen** wijziging aan de bestaande scoringlogica. De
bestaande Streamlit-app (`streamlit_app.py`) en de lokale `.bat`-runners
blijven ongewijzigd werken — de cloud-runner roept dezelfde
`enrich_clients_claude.py` CLI aan die zij ook gebruiken.

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
| `ANTHROPIC_API_KEY` / `SERPER_API_KEY` / `FIRECRAWL_API_KEY` | API keys |
| `MAX_ROWS` | optioneel, beperkt rijen per task (smoke tests) |
| `FORCE_RERUN` | `true` om een bestaande part-output te overschrijven |
| `MODE` | vrij veld, alleen gelogd |

`CLOUD_RUN_TASK_INDEX`/`CLOUD_RUN_TASK_COUNT` hebben voorrang zodra beide
gezet zijn (dat doet het platform automatisch); anders vallen we terug op
`--task-index`/`--task-count` (CLI) of `TASK_COUNT` (env), voor lokaal testen.

### `cloud_merge_results.py`

| Env var | Betekenis |
|---|---|
| `RUN_ID` | run-identifier |
| `OUTPUT_GCS_DIR` | zelfde `runs/<run_id>` map als de tasks |
| `EXPECTED_TASK_COUNT` | verwacht aantal part-bestanden (fail-fast bij mismatch) |
| `FINAL_OUTPUT_NAME` | optioneel, default `lead_prioritizer_final.xlsx` |

### `cloud_dispatcher.py` (service)

| Env var | Betekenis |
|---|---|
| `CLOUD_RUN_JOB_NAME` | naam van de Cloud Run Job |
| `CLOUD_RUN_REGION` | bv. `europe-west1` |
| `CLOUD_RUN_PROJECT` | GCP project-id |
| `RUNS_GCS_DIR` | `gs://<runs-bucket>` (default: dezelfde bucket als de input) |
| `DEFAULT_TASK_COUNT` | fallback task count als rijen niet geteld konden worden (default 50) |
| `DEFAULT_PARALLELISM` | max gelijktijdige tasks bij het starten van de job |

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
  --task-timeout 3600
```

Cloud Run Job execution starten met task count en parallelism (handmatige test,
of wat de dispatcher automatisch doet via de `google-cloud-run` client):

```bash
gcloud run jobs execute myngle-lead-prioritizer \
  --region europe-west1 \
  --tasks 25 \
  --update-env-vars INPUT_GCS_URI=gs://myngle-input/incoming/klant.xlsx,OUTPUT_GCS_DIR=gs://myngle-runs/runs/20260101_120000_klant,RUN_ID=20260101_120000_klant,TASK_COUNT=25
```

Dispatcher als Cloud Run **service** deployen:

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
  (of de naam die je via `FINAL_OUTPUT_NAME`/`--final-output-name` opgeeft)
- Merge-manifest: `gs://<runs-bucket>/runs/<run_id>/final/manifest_done.json`

## Hoe retries werken

- Elke task is idempotent: als `parts/part_XXXX.xlsx` al bestaat en
  `FORCE_RERUN` niet `true` is, slaat de task verwerking over en schrijft een
  `done`-status met `status: "skipped"`.
- Cloud Run Jobs kan losse gefaalde tasks automatisch retrien
  (`--max-retries`); dankzij de idempotentie-check hierboven verwerkt een
  retry nooit dubbel als de part al klaar staat.
- `cloud_merge_results.py` faalt expliciet en duidelijk als het aantal
  gevonden part-bestanden niet overeenkomt met `EXPECTED_TASK_COUNT` — dat is
  het signaal om losse tasks opnieuw te draaien voordat je merget.

## Eerste veilige instellingen

- `TASK_COUNT=10` voor de eerste test.
- `TASK_COUNT=25` voor de tweede test.
- `TASK_COUNT=50` voor een productieachtige test.
- `PARALLELISM` gelijk aan `TASK_COUNT`, tot maximaal 50.

## Rate-limit notities

- Serper Standard (100 q/s) is ruim voldoende, ook bij `TASK_COUNT=50`.
- Anthropic Haiku Scale-tier is ruim volgens de huidige console-screenshot,
  maar blijf 429's meten in Cloud Logging — dit is geen garantie, alleen een
  momentopname.
- Firecrawl Standard heeft 50 concurrent requests. Cap eigen parallel
  Firecrawl-gebruik op 40–45 als je later aparte throttling toevoegt, zodat er
  marge overblijft naast de sharding-parallelism.

## Bekende beperkingen (v1)

- Geen database — status en manifests zijn platte JSON-bestanden op GCS.
- Geen 50 aparte buckets — alles loopt via prefixes in één of twee buckets.
- Geen Kubernetes.
- Geen wijziging aan de bestaande scoringlogica in `commercial_fit_scoring.py`.
- Streamlit (`streamlit_app.py`) en de lokale `.bat`-runners blijven ongewijzigd
  bestaan en werken.
- De dispatcher's Cloud Run Job execution-call is geïmplementeerd met de
  `google-cloud-run` v2 client, maar is nooit end-to-end getest tegen een echte
  Cloud Run Job in dit project — de eerste echte cloud-test gebeurt later
  handmatig.
