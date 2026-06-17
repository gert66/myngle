@echo off
setlocal EnableDelayedExpansion

:: ============================================================
:: run_cleaner_queue_generic.bat
:: mYngle Input Cleaner, queue-aware batch runner v1
::
:: Usage:
::   run_cleaner_queue_generic.bat <queue> "<batches>" <mode> [max_rows]
::
:: Arguments:
::   queue     - Queue name or numeric alias: Italy50, Italy100, Italy200, Germany
::               Aliases: 0=Italy50, 1=Italy100, 2=Italy200, 3=Germany
::   batches   - Batch number