<#
.SYNOPSIS
    Draai de mYngle Lead Prioritizer v2-pipeline op Google Cloud Run Jobs
    tegen een lokaal Excel-bestand, en download het samengevoegde eindresultaat.

.DESCRIPTION
    Wrapper rond de losse stappen uit docs/cloud_run_workflow.md (Optie A,
    handmatig): upload -> job uitvoeren -> lokaal mergen -> downloaden.
    Vereist: gcloud CLI ingelogd met toegang tot het project, en Python met
    de repo-dependencies (draai dit script vanuit de repo-root).

.PARAMETER InputFile
    Pad naar het lokale .xlsx-bestand dat verwerkt moet worden.

.PARAMETER TaskCount
    Aantal Cloud Run-tasks (default 10). Hoger = sneller maar zwaardere
    Firecrawl/Serper-belasting; zie "Eerste veilige instellingen" in de doc.

.PARAMETER Mode
    Lead Prioritizer v2 run mode (default "full").

.EXAMPLE
    .\run_cloud_lead_prioritizer.ps1 -InputFile "C:\leads\klant.xlsx"

.EXAMPLE
    .\run_cloud_lead_prioritizer.ps1 -InputFile "C:\leads\klant.xlsx" -TaskCount 25
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$InputFile,

    [int]$TaskCount = 10,

    [string]$Mode = "full",

    [string]$Project = "project-979d7166-1016-40ce-94c",
    [string]$Region = "europe-west4",
    [string]$Bucket = "myngle-cloud-run-test",
    [string]$JobName = "myngle-lead-prioritizer"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $InputFile)) {
    Write-Error "Input-bestand niet gevonden: $InputFile"
    exit 1
}

$fileName = Split-Path -Leaf $InputFile
$stem = [System.IO.Path]::GetFileNameWithoutExtension($fileName)
$slug = ($stem -replace '[^a-zA-Z0-9]+', '_').Trim('_')
$runId = "{0}_{1}" -f ((Get-Date).ToUniversalTime().ToString("yyyyMMdd_HHmmss")), $slug
$incomingUri = "gs://$Bucket/incoming/$fileName"
$outputDir = "gs://$Bucket/runs/$runId"

Write-Host "=== mYngle Lead Prioritizer - Cloud Run run ===" -ForegroundColor Cyan
Write-Host "RunID       : $runId"
Write-Host "Input       : $InputFile"
Write-Host "TaskCount   : $TaskCount"
Write-Host "Mode        : $Mode"
Write-Host ""

Write-Host "[1/4] Uploaden naar $incomingUri ..." -ForegroundColor Yellow
gcloud storage cp $InputFile $incomingUri --project $Project
if ($LASTEXITCODE -ne 0) { Write-Error "Upload mislukt."; exit 1 }

Write-Host "[2/4] Cloud Run Job starten ($JobName, $TaskCount tasks) ..." -ForegroundColor Yellow
$envVars = "INPUT_GCS_URI=$incomingUri,OUTPUT_GCS_DIR=$outputDir,RUN_ID=$runId,TASK_COUNT=$TaskCount,MODE=$Mode"
gcloud run jobs execute $JobName `
    --project $Project `
    --region $Region `
    --update-env-vars $envVars `
    --wait
if ($LASTEXITCODE -ne 0) {
    Write-Error "Job-executie mislukt of gefaald. Check status-JSON's en Cloud Logging voor details."
    exit 1
}

Write-Host "[3/4] Deel-resultaten lokaal ophalen (vermijdt ADC-vereiste van de Python GCS-client) ..." -ForegroundColor Yellow
$localRunDir = Join-Path $env:TEMP "cloud_merge_$runId"
$localParts = Join-Path $localRunDir "parts"
$localStatus = Join-Path $localRunDir "status"
New-Item -ItemType Directory -Force -Path $localParts, $localStatus | Out-Null

gcloud storage cp "$outputDir/parts/*.xlsx" "$localParts/" --project $Project
if ($LASTEXITCODE -ne 0) { Write-Error "Ophalen van part-bestanden mislukt."; exit 1 }

gcloud storage cp "$outputDir/status/*_done.json" "$localStatus/" --project $Project
if ($LASTEXITCODE -ne 0) { Write-Error "Ophalen van status-bestanden mislukt."; exit 1 }
# Geen _failed.json-bestanden is de normale, geslaagde situatie -> negeer een niet-nul
# exitcode hier bewust (gcloud faalt op een glob-patroon zonder matches).
gcloud storage cp "$outputDir/status/*_failed.json" "$localStatus/" --project $Project 2>$null

Write-Host "[4/4] Resultaten mergen tot 1 Excel ..." -ForegroundColor Yellow
python cloud_merge_results.py --run-id $runId --output-dir $localRunDir --expected-task-count $TaskCount
if ($LASTEXITCODE -ne 0) {
    Write-Error "Merge mislukt. Zie foutmelding hierboven (bv. een gefaalde task -> opnieuw draaien voor het mergen)."
    exit 1
}

$localOutput = Join-Path (Get-Location) "${slug}_prioritized.xlsx"
Copy-Item (Join-Path $localRunDir "final\lead_prioritizer_final.xlsx") $localOutput -Force

Write-Host "Eindresultaat terugzetten naar GCS ($outputDir/final/) ..." -ForegroundColor Yellow
gcloud storage cp $localOutput "$outputDir/final/lead_prioritizer_final.xlsx" --project $Project

Write-Host ""
Write-Host "Klaar! Eindresultaat: $localOutput" -ForegroundColor Green
Write-Host "Run-gegevens in GCS: $outputDir"
