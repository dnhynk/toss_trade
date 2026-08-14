@echo off
REM ---------------------------------------------------------------------------
REM Coordinator headless session launcher (Task Scheduler entry point).
REM
REM Started by schtasks only. Do NOT run `schtasks /run` from an agent session:
REM console cleanup propagates Ctrl+C and kills the task (LastTaskResult
REM 3221225786 - it happened twice in this repo, see COORDINATOR-STATE 5-2).
REM
REM Usage:  coord_headless.cmd <spec-path-relative-to-repo>
REM
REM ASCII only. The console is cp949 here and non-ASCII raises
REM UnicodeEncodeError, which has already killed a process in this repo.
REM ---------------------------------------------------------------------------
setlocal

set "REPO=C:\Users\dongh\toss_trade"
set "CLAUDE=C:\Users\dongh\.local\bin\claude.exe"
set "SPEC=%~1"
if "%SPEC%"=="" set "SPEC=coordination\specs\coord_d21_verdict_execution.md"

if not exist "%CLAUDE%" (
  echo [coord_headless] FATAL claude.exe not found at %CLAUDE%
  exit /b 9
)
if not exist "%REPO%\%SPEC%" (
  echo [coord_headless] FATAL spec not found: %REPO%\%SPEC%
  exit /b 9
)

cd /d "%REPO%" || exit /b 9
if not exist "%REPO%\out" mkdir "%REPO%\out"

set "LOG=%REPO%\out\coord_headless.log"

echo. >> "%LOG%"
echo ======================================================================== >> "%LOG%"
echo [coord_headless] start %DATE% %TIME%  spec=%SPEC% >> "%LOG%"
echo ======================================================================== >> "%LOG%"

REM -p        : non-interactive (headless) mode
REM --dangerously-skip-permissions : unattended run, no human to approve tools.
REM             The authority boundary is enforced by the spec file, not by
REM             prompts - see coordination/AUTOMATION.md section 1.
"%CLAUDE%" -p --dangerously-skip-permissions "You are the coordinator. Read %SPEC% in this repository, in full, and execute it exactly. Read every file it tells you to read first. Do not widen your own authority: section 5 of that spec lists what you must not do." >> "%LOG%" 2>&1

set "RC=%ERRORLEVEL%"
echo [coord_headless] exit rc=%RC% at %DATE% %TIME% >> "%LOG%"
exit /b %RC%
