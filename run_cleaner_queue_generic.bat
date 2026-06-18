@echo off
setlocal EnableDelayedExpansion

rem Generic runner for input_cleaner_register_edition.py
rem Usage: run_cleaner_queue_generic.bat <queue> "<batches>" <dry|test|full> [max_rows] [simple]
rem Examples:
rem   run_cleaner_queue_generic.bat Italy50 "1" dry
rem   run_cleaner_queue_generic.bat Italy50 "01" dry
rem   run_cleaner_queue_generic.bat Italy50 "1" test 50
rem   run_cleaner_queue_generic.bat Italy50 "1-7" full
rem   run_cleaner_queue_generic.bat Italy50 "1" test 50 simple
rem   run_cleaner_queue_generic.bat Italy50 "1-7" full 0 simple
rem Aliases: 0=Italy50, 1=Italy100, 2=Italy200, 3=Germany
rem 5th arg "simple" enables --simple-serper-top-domain benchmark mode

if "%~1"=="" goto usage
if "%~2"=="" goto usage
if "%~3"=="" goto usage

set "QUEUE=%~1"
set "BATCHES=%~2"
set "MODE=%~3"
set "MAX_ROWS=%~4"
set "SIMPLE_FLAG=%~5"

if /I "%QUEUE%"=="0" set "QUEUE=Italy50"
if /I "%QUEUE%"=="1" set "QUEUE=Italy100"
if /I "%QUEUE%"=="2" set "QUEUE=Italy200"
if /I "%QUEUE%"=="3" set "QUEUE=Germany"

if /I not "%QUEUE%"=="Italy50" if /I not "%QUEUE%"=="Italy100" if /I not "%QUEUE%"=="Italy200" if /I not "%QUEUE%"=="Germany" goto usage
if /I not "%MODE%"=="dry" if /I not "%MODE%"=="test" if /I not "%MODE%"=="full" goto usage

set "SIMPLE_ARG="
if /I "%SIMPLE_FLAG%"=="simple" set "SIMPLE_ARG=--simple-serper-top-domain"

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_FILE=%SCRIPT_DIR%input_cleaner_register_edition.py"
if not exist "%SCRIPT_FILE%" (
  echo ERROR: input_cleaner_register_edition.py not found in %SCRIPT_DIR%
  exit /b 1
)

if not defined MYNGLE_DATA_ROOT (
  if exist "C:\Users\%USERNAME%\Nextcloud\Myngle" set "MYNGLE_DATA_ROOT=C:\Users\%USERNAME%\Nextcloud\Myngle"
)
if not defined MYNGLE_DATA_ROOT if exist "C:\Users\gertm\Nextcloud\Myngle" set "MYNGLE_DATA_ROOT=C:\Users\gertm\Nextcloud\Myngle"
if not defined MYNGLE_DATA_ROOT if exist "C:\Users\gmeijer4\Nextcloud\Myngle" set "MYNGLE_DATA_ROOT=C:\Users\gmeijer4\Nextcloud\Myngle"
if not defined MYNGLE_DATA_ROOT (
  echo ERROR: MYNGLE_DATA_ROOT not found. Set it first.
  echo Example: set MYNGLE_DATA_ROOT=C:\Users\gertm\Nextcloud\Myngle
  exit /b 1
)

set "QUEUE_DIR=%MYNGLE_DATA_ROOT%\%QUEUE%"
set "RAW_DIR=%QUEUE_DIR%\00_raw"
set "LOG_DIR=%QUEUE_DIR%\_logs"
if not exist "%RAW_DIR%" (
  echo ERROR: raw folder not found: %RAW_DIR%
  exit /b 1
)
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
if not defined CLEANER_MAX_QUERIES set "CLEANER_MAX_QUERIES=5"

echo Queue: %QUEUE%
echo Raw folder: %RAW_DIR%
echo Mode: %MODE%
echo Batches: %BATCHES%
echo.

if /I "%BATCHES%"=="all" (
  for %%F in ("%RAW_DIR%\%QUEUE%_*_R*.xlsx") do if exist "%%~fF" call :run_file "%%~fF"
  exit /b %ERRORLEVEL%
)

echo %BATCHES% | findstr /C:"-" >nul
if "%ERRORLEVEL%"=="0" (
  for /f "tokens=1,2 delims=-" %%A in ("%BATCHES%") do (
    for /L %%N in (%%A,1,%%B) do call :run_batch %%N
  )
  exit /b %ERRORLEVEL%
)

echo %BATCHES% | findstr /C:"," >nul
if "%ERRORLEVEL%"=="0" (
  set "BATCH_LIST=%BATCHES:,= %"
  for %%N in (!BATCH_LIST!) do call :run_batch %%N
  exit /b %ERRORLEVEL%
)

call :run_batch %BATCHES%
exit /b %ERRORLEVEL%

:run_batch
set "N=%~1"
set "ORIGINAL_N=%N%"

rem Normalize zero-padded batch numbers, so "01" matches files named "_1_R...".
:trim_leading_zeroes
if "!N:~0,1!"=="0" (
  set "N=!N:~1!"
  if not defined N set "N=0"
  goto trim_leading_zeroes
)

set "FOUND="
for %%F in ("%RAW_DIR%\%QUEUE%_!N!_R*.xlsx") do if exist "%%~fF" set "FOUND=%%~fF"

rem Also support raw files that use two-digit batch numbers.
if not defined FOUND (
  set "PAD2=!N!"
  if !N! LSS 10 set "PAD2=0!N!"
  for %%F in ("%RAW_DIR%\%QUEUE%_!PAD2!_R*.xlsx") do if exist "%%~fF" set "FOUND=%%~fF"
)

if not defined FOUND (
  echo ERROR: no raw file found for batch %ORIGINAL_N% ^(normalized: !N!^)
  echo Tried patterns:
  echo   %RAW_DIR%\%QUEUE%_!N!_R*.xlsx
  if defined PAD2 echo   %RAW_DIR%\%QUEUE%_!PAD2!_R*.xlsx
  exit /b 1
)
call :run_file "%FOUND%"
exit /b %ERRORLEVEL%

:run_file
set "INPUT_FILE=%~1"
for /f %%T in ('python -c "import datetime; print(datetime.datetime.now().strftime('%%Y%%m%%d_%%H%%M%%S'))"') do set "STAMP=%%T"
set "LOG_FILE=%LOG_DIR%\cleaner_%QUEUE%_%STAMP%.log"
set "ROWS=0"
if /I "%MODE%"=="test" set "ROWS=10"
if not "%MAX_ROWS%"=="" set "ROWS=%MAX_ROWS%"

echo Running cleaner on: %INPUT_FILE%
echo Log: %LOG_FILE%
if defined SIMPLE_ARG echo Mode: SIMPLE SERPER TOP-DOMAIN (benchmark)
if /I "%MODE%"=="dry" (
  python "%SCRIPT_FILE%" --input "%INPUT_FILE%" --project-root "%MYNGLE_DATA_ROOT%" --country auto --dry-run-paths %SIMPLE_ARG% > "%LOG_FILE%" 2>&1
) else (
  python "%SCRIPT_FILE%" --input "%INPUT_FILE%" --project-root "%MYNGLE_DATA_ROOT%" --country auto --max-rows %ROWS% --max-queries %CLEANER_MAX_QUERIES% %SIMPLE_ARG% > "%LOG_FILE%" 2>&1
)
set "RC=%ERRORLEVEL%"
type "%LOG_FILE%"
exit /b %RC%

:usage
echo Usage: run_cleaner_queue_generic.bat ^<queue^> "^<batches^>" ^<dry^|test^|full^> [max_rows] [simple]
echo Examples:
echo   run_cleaner_queue_generic.bat Italy50 "1" dry
echo   run_cleaner_queue_generic.bat Italy50 "01" dry
echo   run_cleaner_queue_generic.bat Italy50 "1" test 50
echo   run_cleaner_queue_generic.bat Italy50 "1-7" full
echo   run_cleaner_queue_generic.bat 0 "all" full
echo   run_cleaner_queue_generic.bat Italy50 "1" test 50 simple
echo   run_cleaner_queue_generic.bat Italy50 "1-7" full 0 simple
echo Aliases: 0=Italy50, 1=Italy100, 2=Italy200, 3=Germany
echo 5th arg "simple" enables --simple-serper-top-domain benchmark mode
exit /b 1
