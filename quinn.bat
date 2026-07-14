@echo off
REM ============================================================
REM  Quinn - Telnyx AI SDR (POC) launcher
REM
REM  What this script does: one entrypoint for the whole demo.
REM  Double-click it (or run `quinn`) for a 4-option menu:
REM    1 crm    - live CRM dashboard (server + browser)
REM    2 run    - process the inbound queue in the terminal,
REM               then optionally approve drafts one by one
REM    3 reset  - wipe previous runs, fresh seed
REM    4 obs    - observability report (states, queues, spend)
REM  Power-user subcommands (status/trace/approve/...) still
REM  work from a terminal: quinn <command> [id]
REM ============================================================
setlocal
cd /d "%~dp0"

if "%~1"=="" goto :interactive

REM ---- the 4 demo options ------------------------------------
if /i "%~1"=="crm"    goto :crm
if /i "%~1"=="run"    goto :run_queue
if /i "%~1"=="reset"  goto :reset
if /i "%~1"=="obs"    goto :obs

REM ---- power-user commands (not shown in the menu) -----------
if /i "%~1"=="demo"       ( call :reset_db & py -m quinn.run --all & goto :eof )
if /i "%~1"=="all"        ( py -m quinn.run --all & goto :eof )
if /i "%~1"=="one"        ( py -m quinn.run --inbound-id %2 & goto :eof )
if /i "%~1"=="status"     ( py -m quinn.run --status & goto :eof )
if /i "%~1"=="trace"      ( py -m quinn.run --trace %2 & goto :eof )
if /i "%~1"=="costs"      ( py -m quinn.run --costs & goto :eof )
if /i "%~1"=="resume"     ( py -m quinn.run --resume & goto :eof )
if /i "%~1"=="reopen"     ( py -m quinn.run --reopen %2 & goto :eof )
if /i "%~1"=="review"     ( py -m quinn.run --review & goto :eof )
if /i "%~1"=="approve"    ( py -m quinn.run --approve-mail %2 & goto :eof )
if /i "%~1"=="send"       ( py -m quinn.run --send-mail %2 & goto :eof )
if /i "%~1"=="reject"     ( py -m quinn.run --reject-mail %2 & goto :eof )
if /i "%~1"=="seed"       ( py -m quinn.seed & goto :eof )
if /i "%~1"=="test"       ( py -m tests.test_pipeline & goto :eof )
if /i "%~1"=="auth-gmail" ( py -m quinn.gmail & goto :eof )

echo Unknown command: %1
call :showhelp
goto :eof

REM ------------------------------------------------------------
:crm
REM Server in its own window (so its log stays visible), browser
REM opens after a beat. The dashboard is LIVE - it re-reads the DB
REM every 4s, so keep it open while option 2 processes leads.
start "Quinn CRM server" cmd /k py -m quinn.web
timeout /t 2 >nul
start "" http://localhost:8642
echo CRM running at http://localhost:8642 (its server has its own window).
goto :eof

REM ------------------------------------------------------------
:run_queue
py -m quinn.run --all
echo.
set "R="
set /p "R=Approve drafts now? Each approve creates the Gmail draft. [y/N] "
if /i "%R%"=="y" py -m quinn.run --review
goto :eof

REM ------------------------------------------------------------
:reset
call :reset_db
echo.
echo Fresh database: 22 leads seeded, zero runs. Pick "run" to process them
echo (running "run" twice afterwards proves idempotency: 0 extra LLM calls,
echo  0 extra sends on the second pass).
goto :eof

:reset_db
if exist data\quinn.db del /q data\quinn.db
py -m quinn.seed
exit /b 0

REM ------------------------------------------------------------
:obs
echo ================= QUINN OBSERVABILITY =================
py -m quinn.run --status
echo.
echo ----------------- LLM spend by stage ------------------
py -m quinn.run --costs
echo.
echo Full per-lead audit: quinn trace ^<id^>
echo Prompt-level detail (every LLM call): CRM ^> Observability tab
goto :eof

REM ------------------------------------------------------------
:interactive
call :showhelp
echo Pick an option (1-4 or its name) - Enter or "exit" to quit.
echo.
:menu
set "CMDLINE="
set /p "CMDLINE=quinn> "
if "%CMDLINE%"=="" goto :eof
if /i "%CMDLINE%"=="exit" goto :eof
if "%CMDLINE%"=="1" set "CMDLINE=crm"
if "%CMDLINE%"=="2" set "CMDLINE=run"
if "%CMDLINE%"=="3" set "CMDLINE=reset"
if "%CMDLINE%"=="4" set "CMDLINE=obs"
call "%~f0" %CMDLINE%
echo.
goto :menu

:showhelp
echo.
echo Quinn - Telnyx AI SDR (POC)
echo.
echo   1. crm     live CRM dashboard (opens browser; updates every 4s)
echo   2. run     process the inbound queue here, then approve drafts
echo   3. reset   wipe previous runs + fresh seed (clean slate)
echo   4. obs     observability: states, tiers, queues, LLM spend
echo.
exit /b 0
