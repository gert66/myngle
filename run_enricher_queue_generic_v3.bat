@echo off
:: ============================================================
:: run_enricher_queue_generic_v3.bat
:: Compatibility wrapper — forwards all arguments to the real runner.
::
:: Usage (identical to run_enricher_queue_generic.bat):
::   run_enricher_queue_generic_v3.bat <queue> "<batches>" <mode> [max_rows]
::
:: Examples:
::   run_enricher_queue_generic_v3.bat Italy100 "1" dry
::   run_enricher_queue_generic_v3.bat Italy100 "1" test
::   run_enricher_queue_generic_v3.bat Italy100 "1 2 3" full
:: ============================================================
call "%~dp0run_enricher_queue_generic.bat" %*
exit /b %ERRORLEVEL%
