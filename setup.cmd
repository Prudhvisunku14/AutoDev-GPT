@echo off
rem DevPilot AI Windows setup. See setup.ps1 for what it does. Extra arguments are passed on to "devpilot setup".
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
exit /b %ERRORLEVEL%
