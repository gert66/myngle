# update_data_index_and_push.ps1
# Genereert DATA_INDEX.md en pusht alleen als het bestand is gewijzigd.
# Weigert te draaien als er andere uncommitted wijzigingen zijn.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

# --- Controleer op uncommitted wijzigingen buiten DATA_INDEX.md ---
$dirty = git status --porcelain | Where-Object { $_ -notmatch "^\s*\S+\s+DATA_INDEX\.md$" }
if ($dirty) {
    Write-Host "GEWEIGERD: er zijn uncommitted wijzigingen buiten DATA_INDEX.md:" -ForegroundColor Red
    $dirty | ForEach-Object { Write-Host "  $_" }
    exit 1
}

# --- Genereer DATA_INDEX.md ---
Write-Host "Genereren DATA_INDEX.md..."
python update_data_index.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "FOUT: update_data_index.py mislukt." -ForegroundColor Red
    exit 1
}

# --- Controleer of DATA_INDEX.md is gewijzigd ---
$changed = git status --porcelain DATA_INDEX.md
if (-not $changed) {
    Write-Host "DATA_INDEX.md is niet gewijzigd. Niets te committen." -ForegroundColor Yellow
    exit 0
}

# --- Stage alleen DATA_INDEX.md ---
git add DATA_INDEX.md

# --- Commit ---
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm"
git commit -m "chore: update DATA_INDEX.md [$timestamp]"
if ($LASTEXITCODE -ne 0) {
    Write-Host "FOUT: commit mislukt." -ForegroundColor Red
    exit 1
}

# --- Push naar origin/work ---
Write-Host "Pushen naar origin/work..."
git push -u origin work
if ($LASTEXITCODE -ne 0) {
    Write-Host "FOUT: push mislukt." -ForegroundColor Red
    exit 1
}

Write-Host "Klaar: DATA_INDEX.md gecommit en gepusht naar origin/work." -ForegroundColor Green
