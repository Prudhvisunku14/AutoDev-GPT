@echo off
rem The DevPilot AI command line:  devpilot build | devpilot doctor | devpilot --help
setlocal
set "DEVPILOT_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%DEVPILOT_PY%" (
    echo DevPilot is not set up yet. Run setup.cmd first.
    exit /b 1
)
set PYTHONUTF8=1
"%DEVPILOT_PY%" -m app.cli %*
exit /b %ERRORLEVEL%
