@echo off
setlocal EnableDelayedExpansion

:: ============================================================
:: run_opportunity_radar_queue_generic.bat
:: mYngle Opportunity Radar -- queue-aware batch runner v1
::
:: Usage:
::   run_opportunity_radar_queue_generic.bat <queue> "<batches>" <mode> [max_rows]
::
:: Arguments:
::   queue     - Queue name or numeric alias: Italy100, Italy200, Germany
::               Aliases: 1=Italy100, 2=Italy200
::   batches   - Batch number(s) in quotes: "1" or "1 2 3" or "11 12 13"
::   mode      - dry | test | full
::   max_rows  - (optional) override test row limit
::
:: Examples:
::   run_opportunity_radar_queue_generic.bat Italy100 "1" dry
::   run_opportunity_radar_queue_generic.bat Italy100 "1" test
::   run_opportunity_radar_queue_generic.bat Italy100 "1" test 20
::   run_opportunity_radar_queue_generic.bat Italy100 "1 2 3" full
::   run_opportunity_radar_queue_generic.bat Italy200 "11 12 13" test
::   run_opportunity_radar_queue_generic.bat 1 "1" test    (alias: 1=Italy100)
::
:: PROJECT_ROOT resolution (in priority order):
::   1. MYNGLE_DATA_ROOT environment variable (default: C:\Users\gmeijer4\Nextcloud\Myngle)
::      Override by setting MYNGLE_DATA_ROOT before calling this bat.
::   2. Parent of this repo folder  (%~dp0..)
::   3. This repo folder            (%~dp0)
::   4. %~dp0data subfolder
::   5. %~dp0input subfolder
::   The first candidate that contains any .xlsx file wins.
::
:: Input file discovery for queue Italy100 batch 1:
::   Searches {PROJECT_ROOT}\Italy100\02_lead_prioritized\ for the most
::   recently modified .xlsx file matching Italy100_1_* (that is not a
::   lock file starting with ~$).
::   Patterns (in order):
::     {root}\Italy100\02_lead_prioritized\Italy100_1_*.xlsx
::     {root}\Italy100\02_lead_prioritized\Italy100_01_*.xlsx
::     {root}\Italy100\02_lead_prioritized\Italy100_batch_1_*.xlsx
::     {root}\Italy100\02_lead_prioritized\*.xlsx  (newest, any name)
::   ~$ lock files are always excluded.
::
:: Output goes to: {PROJECT_ROOT}\{Queue}\03_opportunity_radar\  (auto-derived by Python)
:: Logs go to:     {PROJECT_ROOT}\{Queue}\_logs\radar_{Queue}_{N}_{timestamp}.log
::
:: No fresh Serper/Claude/Firecrawl calls by default (--force-fresh not set).
:: Set ANTHROPIC_KEY / SERPER_KEY env vars only if force-fresh is needed.
::
:: Implementation: writes a temp .ps1 per batch and runs it with
::   powershell -File  to avoid all CMD/PS quote-nesting issues.
::   Tee-Object streams live Python output to console + log.
:: ============================================================

:: -- DEFAULT DATA ROOT --------------------------------------------------
if not defined MYNGLE_DATA_ROOT (
    set "MYNGLE_DATA_ROOT=C:\Users\gmeijer4\Nextcloud\Myngle"
)

:: -- CONFIG -----------------------------------------------------------

:: Path to opportunity_radar.py
set "SCRIPT_FILE=%~dp0opportunity_radar.py"

:: BAT_DIR = folder containing this bat file (no trailing backslash)
set "BAT_DIR=%~dp0"
if "!BAT_DIR:~-1!"=="\" set "BAT_DIR=!BAT_DIR:~0,-1!"

:: PROJECT_ROOT: resolved below after parsing args
set "PROJECT_ROOT=%~dp0.."

:: Rows to process in test mode (overridable with 4th arg)
set "MAX_ROWS_TEST=5"

:: Halt entire queue run on first non-zero Python exit (1=yes, 0=no)
set "STOP_ON_ERROR=1"

:: API keys -- leave empty; no fresh enrichment by default.
:: Set these only if running with --force-fresh.
set "ANTHROPIC_KEY="
set "SERPER_KEY="

:: -- END CONFIG -------------------------------------------------------

:: -- Show usage if no args --------------------------------------------
if "%~1"=="" goto :show_usage

:: -- Detect legacy single-arg format ----------------------------------
set "_ARG1=%~1"
if /I "!_ARG1!"=="dry"  goto :legacy_mode
if /I "!_ARG1!"=="test" goto :legacy_mode
if /I "!_ARG1!"=="full" goto :legacy_mode

:: -- New queue-based argument parsing ---------------------------------
set "QUEUE_RAW=%~1"
set "BATCH_NUMBERS=%~2"
set "MODE_RAW=%~3"
set "MAX_ROWS_OVERRIDE=%~4"

:: Apply numeric aliases
set "QUEUE_NAME=%QUEUE_RAW%"
if "%QUEUE_RAW%"=="1" set "QUEUE_NAME=Italy100"
if "%QUEUE_RAW%"=="2" set "QUEUE_NAME=Italy200"

:: Parse mode (default: full if not recognised)
set "MODE=full"
if /I "%MODE_RAW%"=="dry"  set "MODE=dry"
if /I "%MODE_RAW%"=="test" set "MODE=test"
if /I "%MODE_RAW%"=="full" set "MODE=full"

:: Validate required args
if "%QUEUE_NAME%"=="" (
    echo ERROR: queue name is required.
    goto :show_usage
)
if "%BATCH_NUMBERS%"=="" (
    echo ERROR: batch number^(s^) required.  Example: "1" or "1 2 3"
    goto :show_usage
)
if "%MODE_RAW%"=="" (
    echo ERROR: mode required: dry / test / full
    goto :show_usage
)

:: Validate max_rows if supplied
set "_MAX_ROWS_SOURCE="
if not "%MAX_ROWS_OVERRIDE%"=="" (
    for /f "usebackq delims=" %%V in (`powershell -NoProfile -Command "if ('%MAX_ROWS_OVERRIDE%' -match '^[1-9][0-9]*$') { 'ok' } else { 'bad' }"`) do set "_MR_VALID=%%V"
    if "!_MR_VALID!"=="bad" (
        echo ERROR: max_rows must be a positive integer. Got: %MAX_ROWS_OVERRIDE%
        exit /b 1
    )
    set "_MAX_ROWS_SOURCE=user supplied"
)

:: -- Resolve PROJECT_ROOT (MYNGLE_DATA_ROOT override or auto-detect) --
call :resolve_project_root

:: -- Print raw args and parsed values ---------------------------------
echo.
echo [radar] Raw args:
echo   arg1 (queue):    %~1
echo   arg2 (batches):  %~2
echo   arg3 (mode):     %~3
if not "%~4"=="" echo   arg4 (max_rows): %~4
echo.
echo [radar] Parsed:
echo   queue:           %QUEUE_NAME%
echo   batch numbers:   %BATCH_NUMBERS%
echo   mode:            %MODE%
if not "%MAX_ROWS_OVERRIDE%"=="" (
    if "%MODE%"=="dry" (
        echo   max rows:        %MAX_ROWS_OVERRIDE% ^(supplied but ignored in dry mode^)
    ) else if "%MODE%"=="full" (
        echo   max rows:        %MAX_ROWS_OVERRIDE% ^(safety cap on full mode^)
    ) else (
        echo   max rows:        %MAX_ROWS_OVERRIDE% ^(user supplied^)
    )
) else (
    if "%MODE%"=="test" echo   max rows:        %MAX_ROWS_TEST% ^(default test limit^)
    if "%MODE%"=="full" echo   max rows:        all rows
    if "%MODE%"=="dry"  echo   max rows:        n/a ^(dry mode^)
)
echo.
echo [radar] PROJECT_ROOT: %PROJECT_ROOT%
echo [radar] BAT_DIR:      %BAT_DIR%
echo.

:: -- Locale-safe timestamp --------------------------------------------
for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"`) do set "LOG_STAMP=%%T"

:: -- Verify script exists ---------------------------------------------
if not exist "%SCRIPT_FILE%" (
    echo ERROR: opportunity_radar.py not found.
    echo        Expected: %SCRIPT_FILE%
    exit /b 1
)

:: -- Loop over each batch number --------------------------------------
set "QUEUE_EXIT=0"
for %%B in (%BATCH_NUMBERS%) do (
    call :run_one_batch "%%B"
    set "QUEUE_EXIT=!ERRORLEVEL!"
    if !QUEUE_EXIT! neq 0 (
        if "!STOP_ON_ERROR!"=="1" (
            echo [radar] STOP_ON_ERROR=1 -- halting queue after batch %%B.
            exit /b !QUEUE_EXIT!
        )
    )
)
echo [radar] All batches done. Final exit code: %QUEUE_EXIT%
exit /b %QUEUE_EXIT%


:: =======================================================================
:resolve_project_root
:: Resolve PROJECT_ROOT from MYNGLE_DATA_ROOT env var or auto-detect.
:: =======================================================================
if defined MYNGLE_DATA_ROOT (
    set "PROJECT_ROOT=%MYNGLE_DATA_ROOT%"
    echo [radar] PROJECT_ROOT: using MYNGLE_DATA_ROOT=%MYNGLE_DATA_ROOT%
    exit /b 0
)

set "_PR_FOUND="
call :_try_project_root "%~dp0.."
if not defined _PR_FOUND call :_try_project_root "%~dp0"
if not defined _PR_FOUND call :_try_project_root "%~dp0data"
if not defined _PR_FOUND call :_try_project_root "%~dp0input"
if defined _PR_FOUND (
    echo [radar] PROJECT_ROOT: auto-detected as %PROJECT_ROOT%
) else (
    set "PROJECT_ROOT=%~dp0.."
    echo [radar] PROJECT_ROOT: no .xlsx files found in any candidate -- defaulting to %PROJECT_ROOT%
)
exit /b 0

:_try_project_root
if defined _PR_FOUND exit /b 0
set "_PR_CAND=%~1"
for /f "usebackq delims=" %%X in (`powershell -NoProfile -Command "if (Get-ChildItem '%_PR_CAND%' -Filter '*.xlsx' -Recurse -EA 0 | Select-Object -First 1) { 'yes' }" 2^>nul`) do (
    if "%%X"=="yes" (
        set "PROJECT_ROOT=%_PR_CAND%"
        set "_PR_FOUND=1"
    )
)
exit /b 0


:: =======================================================================
:run_one_batch
:: Process a single batch number.  Called with the batch number as %~1.
:: =======================================================================
set "BATCH_NUM=%~1"
echo.
echo ============================================================
echo  QUEUE: %QUEUE_NAME%  BATCH: %BATCH_NUM%  MODE: %MODE%
echo ============================================================

:: Resolve input file from 02_lead_prioritized
call :find_input "%QUEUE_NAME%" "%BATCH_NUM%"

if "!RESOLVED_INPUT!"=="" (
    echo.
    echo ERROR: No input file found for %QUEUE_NAME% batch %BATCH_NUM%.
    echo.
    echo   Diagnostics:
    echo     Current dir:   !CD!
    echo     BAT dir:       %BAT_DIR%
    echo     PROJECT_ROOT:  %PROJECT_ROOT%
    echo     Queue:         %QUEUE_NAME%
    echo     Batch:         %BATCH_NUM%
    echo.
    echo   Patterns searched ^(in order^):
    echo     1. %PROJECT_ROOT%\%QUEUE_NAME%\02_lead_prioritized\%QUEUE_NAME%_%BATCH_NUM%_*.xlsx
    echo     2. %PROJECT_ROOT%\%QUEUE_NAME%\02_lead_prioritized\%QUEUE_NAME%_0%BATCH_NUM%_*.xlsx
    echo     3. %PROJECT_ROOT%\%QUEUE_NAME%\02_lead_prioritized\%QUEUE_NAME%_batch_%BATCH_NUM%_*.xlsx
    echo     4. %PROJECT_ROOT%\%QUEUE_NAME%\02_lead_prioritized\*.xlsx  ^(newest, any name^)
    echo     ^(~$ lock files always excluded^)
    echo.
    echo   .xlsx files found under 02_lead_prioritized ^(max 50^):
    powershell -NoProfile -Command "$d='%PROJECT_ROOT%\%QUEUE_NAME%\02_lead_prioritized'; $hits = Get-ChildItem $d -Filter '*.xlsx' -EA 0 | Where-Object { $_.Name -notlike '~$*' } | Select-Object -First 50; if ($hits) { $hits | ForEach { Write-Host \"    $_\" } } else { Write-Host '    (none found)' }"
    echo.
    echo   All .xlsx files under PROJECT_ROOT ^(max 50^):
    powershell -NoProfile -Command "$r='%PROJECT_ROOT%'; $hits = Get-ChildItem $r -Filter '*.xlsx' -Recurse -EA 0 | Select-Object -First 50; if ($hits) { $hits | ForEach { Write-Host \"    $_\" } } else { Write-Host '    (none found)' }"
    echo.
    exit /b 1
)

:: Resolve log dir (per queue, under _logs)
set "BATCH_LOG_DIR=%PROJECT_ROOT%\%QUEUE_NAME%\_logs"
set "BATCH_LOG_FILE=%BATCH_LOG_DIR%\radar_%QUEUE_NAME%_%BATCH_NUM%_%LOG_STAMP%.log"
if not exist "!BATCH_LOG_DIR!" mkdir "!BATCH_LOG_DIR!"

:: -- Parsed + resolved debug block ------------------------------------
echo [radar] Resolved input file: !RESOLVED_INPUT!
echo [radar] Log file:            !BATCH_LOG_FILE!
echo [radar] Output:              auto-derived by opportunity_radar.py -^> 03_opportunity_radar\
echo.

:: Write log header
echo ============================================================>>"%BATCH_LOG_FILE%"
echo  QUEUE: %QUEUE_NAME%  BATCH: %BATCH_NUM%  MODE: %MODE%>>"%BATCH_LOG_FILE%"
echo  Date/time: %LOG_STAMP%>>"%BATCH_LOG_FILE%"
echo  Input:     !RESOLVED_INPUT!>>"%BATCH_LOG_FILE%"
echo  Log:       !BATCH_LOG_FILE!>>"%BATCH_LOG_FILE%"
echo ============================================================>>"%BATCH_LOG_FILE%"

:: Python version and script identity
echo  Python version:
python --version
echo.
echo  Script identity:
python -c "import pathlib; p=pathlib.Path(r'%SCRIPT_FILE%'); print('  SCRIPT_EXISTS:', p.exists()); print('  SCRIPT_MTIME: ', int(p.stat().st_mtime) if p.exists() else 'MISSING')"
echo.

:: Set _RUN_* env vars consumed by the PS1
set "PYTHONUNBUFFERED=1"
set "_RUN_SCRIPT=%SCRIPT_FILE%"
set "_RUN_INPUT=!RESOLVED_INPUT!"
set "_RUN_LOG=!BATCH_LOG_FILE!"
set "_RUN_ANTHROPIC=%ANTHROPIC_KEY%"
set "_RUN_SERPER=%SERPER_KEY%"
set "_RUN_DRY="
set "_RUN_MAXROWS="

if "%MODE%"=="dry" (
    set "_RUN_DRY=1"
    if not "%MAX_ROWS_OVERRIDE%"=="" (
        echo [radar] Dry mode -- max_rows %MAX_ROWS_OVERRIDE% noted but Python is not invoked for scoring.
    )
) else if "%MODE%"=="test" (
    if not "%MAX_ROWS_OVERRIDE%"=="" (
        set "_RUN_MAXROWS=%MAX_ROWS_OVERRIDE%"
        echo [radar] Test mode -- row limit: %MAX_ROWS_OVERRIDE% ^(user supplied^)
    ) else (
        set "_RUN_MAXROWS=%MAX_ROWS_TEST%"
        echo [radar] Test mode -- row limit: %MAX_ROWS_TEST% ^(default^)
    )
) else (
    if not "%MAX_ROWS_OVERRIDE%"=="" (
        set "_RUN_MAXROWS=%MAX_ROWS_OVERRIDE%"
        echo [radar] Full mode with max rows safety cap: %MAX_ROWS_OVERRIDE%
    )
)

call :write_and_run_ps1
exit /b %ERRORLEVEL%


:: =======================================================================
:find_input
:: Find the most-recently-modified input file in 02_lead_prioritized
:: for a given queue + batch.  ~$ lock files are always excluded.
:: Sets RESOLVED_INPUT to the full path, or empty string if not found.
:: Args: %1=queue name, %2=batch number
:: =======================================================================
set "RESOLVED_INPUT="
set "_FI_QUEUE=%~1"
set "_FI_BATCH=%~2"

:: Search dir is always 02_lead_prioritized
set "_FI_SD=%PROJECT_ROOT%\%_FI_QUEUE%\02_lead_prioritized"

:: Pattern 1: {Queue}_{N}_*.xlsx  (e.g. Italy100_1_batch_001_..._lead_prioritized_....xlsx)
set "_FI_SP=%_FI_QUEUE%_%_FI_BATCH%_*.xlsx"
call :_fi_try
if not "!RESOLVED_INPUT!"=="" exit /b 0

:: Pattern 2: zero-padded batch number
set "_FI_SP=%_FI_QUEUE%_0%_FI_BATCH%_*.xlsx"
call :_fi_try
if not "!RESOLVED_INPUT!"=="" exit /b 0

:: Pattern 3: batch_ infix
set "_FI_SP=%_FI_QUEUE%_batch_%_FI_BATCH%_*.xlsx"
call :_fi_try
if not "!RESOLVED_INPUT!"=="" exit /b 0

:: Pattern 4: any .xlsx (newest, excludes ~$ locks) -- fallback
set "_FI_SP=*.xlsx"
call :_fi_try_no_lock
if not "!RESOLVED_INPUT!"=="" exit /b 0

:: Nothing found
exit /b 0


:: =======================================================================
:_fi_try
:: Check _FI_SD for files matching _FI_SP, excluding ~$ lock files.
:: Picks the newest by LastWriteTime.
:: Dir and pattern passed via env vars to safely handle spaces.
:: =======================================================================
for /f "usebackq delims=" %%F in (`powershell -NoProfile -Command "(Get-ChildItem $env:_FI_SD -Filter $env:_FI_SP -EA 0 | Where-Object { $_.Name -notlike '~$*' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName"`) do (
    set "RESOLVED_INPUT=%%F"
)
exit /b 0


:: =======================================================================
:_fi_try_no_lock
:: Same as _fi_try but used for the *.xlsx wildcard fallback to make the
:: exclusion of ~$ locks explicit and visible in the source.
:: =======================================================================
for /f "usebackq delims=" %%F in (`powershell -NoProfile -Command "(Get-ChildItem $env:_FI_SD -Filter $env:_FI_SP -EA 0 | Where-Object { $_.Name -notlike '~$*' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName"`) do (
    set "RESOLVED_INPUT=%%F"
)
exit /b 0


:: =======================================================================
:write_and_run_ps1
:: Write a temp .ps1 from _RUN_* env vars and execute with powershell -File.
:: Avoids all CMD/PS quote-nesting issues for paths with spaces.
:: No --output-dir needed: opportunity_radar.py auto-derives output path
:: from the input file location (-> 03_opportunity_radar subfolder).
:: =======================================================================
set "TEMP_PS1=%TEMP%\radar_run_%RANDOM%.ps1"

echo $pythonExe  = 'python'>>"%TEMP_PS1%"
echo $scriptPath = $env:_RUN_SCRIPT>>"%TEMP_PS1%"
echo $inputFile  = $env:_RUN_INPUT>>"%TEMP_PS1%"
echo $logFile    = $env:_RUN_LOG>>"%TEMP_PS1%"
echo $antKey     = $env:_RUN_ANTHROPIC>>"%TEMP_PS1%"
echo $serperKey  = $env:_RUN_SERPER>>"%TEMP_PS1%"
echo $maxRows    = $env:_RUN_MAXROWS>>"%TEMP_PS1%"
echo $isDry      = $env:_RUN_DRY>>"%TEMP_PS1%"
echo # --- debug: paths only, keys excluded --->>"%TEMP_PS1%"
echo Write-Host "[radar] Python:  $pythonExe">>"%TEMP_PS1%"
echo Write-Host "[radar] Script:  $scriptPath">>"%TEMP_PS1%"
echo Write-Host "[radar] Input:   $inputFile">>"%TEMP_PS1%"
echo Write-Host "[radar] Log:     $logFile">>"%TEMP_PS1%"
echo if ($maxRows)         { Write-Host "[radar] Max rows: $maxRows" }>>"%TEMP_PS1%"
echo if ($isDry -eq '1')  { Write-Host "[radar] Mode:     DRY RUN (path check only, no scoring)" }>>"%TEMP_PS1%"
echo if ($antKey)          { Write-Host "[radar] Anthropic key: present (not shown)" }>>"%TEMP_PS1%"
echo if ($serperKey)       { Write-Host "[radar] Serper key:    present (not shown)" }>>"%TEMP_PS1%"
echo Write-Host "">>"%TEMP_PS1%"
echo # --- build arg array --->>"%TEMP_PS1%"
echo $xargs = @('--input', $inputFile)>>"%TEMP_PS1%"
echo if ($antKey)    { $xargs += '--anthropic-key'; $xargs += $antKey }>>"%TEMP_PS1%"
echo if ($serperKey) { $xargs += '--serper-key';    $xargs += $serperKey }>>"%TEMP_PS1%"
echo if ($isDry -eq '1') {>>"%TEMP_PS1%"
echo     $xargs += '--dry-run-paths'>>"%TEMP_PS1%"
echo } elseif ($maxRows) {>>"%TEMP_PS1%"
echo     $xargs += '--max-rows'>>"%TEMP_PS1%"
echo     $xargs += $maxRows>>"%TEMP_PS1%"
echo }>>"%TEMP_PS1%"
echo # --- run Python with live output tee'd to log --->>"%TEMP_PS1%"
echo ^& $pythonExe -u $scriptPath @xargs 2^>^&1 ^| ForEach-Object { "$_" } ^| Tee-Object -FilePath $logFile -Append>>"%TEMP_PS1%"
echo $exitCode = $LASTEXITCODE>>"%TEMP_PS1%"
echo exit $exitCode>>"%TEMP_PS1%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP_PS1%"
set "PS1_EXIT=%ERRORLEVEL%"
del "%TEMP_PS1%" 2>nul

echo.
if "%PS1_EXIT%"=="0" (
    echo OK: batch completed successfully.
    echo OK: batch completed successfully.>>"%BATCH_LOG_FILE%"
) else (
    echo ERROR: Python exited with code %PS1_EXIT%.
    echo ERROR: Python exited with code %PS1_EXIT%.>>"%BATCH_LOG_FILE%"
)
echo Log saved: %BATCH_LOG_FILE%
exit /b %PS1_EXIT%


:: =======================================================================
:legacy_mode
:: Original single-file mode: run_opportunity_radar_queue_generic.bat [dry|test|full] [max_rows]
:: Input file: input\batch_input.xlsx  (drop a prioritized .xlsx there)
:: =======================================================================
set "QUEUE_NAME=legacy"
set "BATCH_NUM=0"
set "MODE=full"
if /I "%~1"=="dry"  set "MODE=dry"
if /I "%~1"=="test" set "MODE=test"
if /I "%~1"=="full" set "MODE=full"
set "MAX_ROWS_OVERRIDE=%~2"

set "RESOLVED_INPUT=%~dp0input\batch_input.xlsx"

for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"`) do set "LOG_STAMP=%%T"
set "BATCH_LOG_DIR=%BAT_DIR%\logs"
set "BATCH_LOG_FILE=%BATCH_LOG_DIR%\radar_legacy_%LOG_STAMP%.log"

if not exist "%BATCH_LOG_DIR%" mkdir "%BATCH_LOG_DIR%"
if not exist "%SCRIPT_FILE%" (
    echo ERROR: opportunity_radar.py not found: %SCRIPT_FILE%
    exit /b 1
)
if not exist "%RESOLVED_INPUT%" (
    echo ERROR: legacy input file not found: %RESOLVED_INPUT%
    echo        Place your prioritized .xlsx at  input\batch_input.xlsx  or use:
    echo        run_opportunity_radar_queue_generic.bat ^<queue^> "^<batches^>" ^<mode^>
    exit /b 1
)

echo [radar] Legacy mode: %MODE%, input: %RESOLVED_INPUT%

set "PYTHONUNBUFFERED=1"
set "_RUN_SCRIPT=%SCRIPT_FILE%"
set "_RUN_INPUT=%RESOLVED_INPUT%"
set "_RUN_LOG=%BATCH_LOG_FILE%"
set "_RUN_ANTHROPIC=%ANTHROPIC_KEY%"
set "_RUN_SERPER=%SERPER_KEY%"
set "_RUN_DRY="
set "_RUN_MAXROWS="
if "%MODE%"=="dry" (
    set "_RUN_DRY=1"
) else if "%MODE%"=="test" (
    if not "%MAX_ROWS_OVERRIDE%"=="" (
        set "_RUN_MAXROWS=%MAX_ROWS_OVERRIDE%"
    ) else (
        set "_RUN_MAXROWS=%MAX_ROWS_TEST%"
    )
) else (
    if not "%MAX_ROWS_OVERRIDE%"=="" set "_RUN_MAXROWS=%MAX_ROWS_OVERRIDE%"
)
call :write_and_run_ps1
exit /b %ERRORLEVEL%


:: =======================================================================
:show_usage
:: =======================================================================
echo.
echo  USAGE:
echo    run_opportunity_radar_queue_generic.bat ^<queue^> "^<batches^>" ^<mode^> [max_rows]
echo.
echo  QUEUE:    Italy100 ^| Italy200 ^| Germany ^| 1 ^| 2
echo  BATCHES:  "1" ^| "1 2 3" ^| "11 12 13"   (in quotes^)
echo  MODE:     dry  = path check only ^(--dry-run-paths^), no scoring
echo            test = first N rows (default: %MAX_ROWS_TEST%^)
echo            full = all rows
echo  MAX_ROWS: optional positive integer
echo            test: overrides the default %MAX_ROWS_TEST%-row limit
echo            full: applies a safety cap on row count
echo            dry:  noted but ignored
echo.
echo  EXAMPLES:
echo    run_opportunity_radar_queue_generic.bat Italy100 "1" dry
echo    run_opportunity_radar_queue_generic.bat Italy100 "1" test
echo    run_opportunity_radar_queue_generic.bat Italy100 "1" test 20
echo    run_opportunity_radar_queue_generic.bat Italy100 "1 2 3" test 10
echo    run_opportunity_radar_queue_generic.bat Italy100 "1 2 3" full
echo    run_opportunity_radar_queue_generic.bat Italy100 "1" full 50
echo    run_opportunity_radar_queue_generic.bat 1 "1" test
echo.
echo  INPUT FILE DISCOVERY:
echo    Looks for newest non-lock .xlsx in:
echo      {PROJECT_ROOT}\{Queue}\02_lead_prioritized\
echo    Set MYNGLE_DATA_ROOT environment variable to override PROJECT_ROOT.
echo    Output is auto-derived by opportunity_radar.py to:
echo      {PROJECT_ROOT}\{Queue}\03_opportunity_radar\
echo    Logs go to:
echo      {PROJECT_ROOT}\{Queue}\_logs\radar_{Queue}_{N}_{timestamp}.log
echo.
echo  NOTE: No Serper/Claude/Firecrawl calls are made by default.
echo        Set ANTHROPIC_KEY + SERPER_KEY and add --force-fresh only
echo        if you need to re-enrich from scratch.
echo.
exit /b 1
