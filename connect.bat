@echo off
rem Connect an AI agent to a Squidbrake gateway (yours or someone else's). Examples:
rem   connect.bat claude-code --project C:\my-project
rem   connect.bat wrap --sandbox --agent claude-code --url https://gateway.example.com --key gw_...
rem The first run installs what it needs into .venv.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto deps
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython
echo Setting up Squidbrake (first run only, takes a minute)...
%PY% -m venv .venv
if errorlevel 1 (
  if exist .venv rmdir /s /q .venv
  goto nopython
)

:deps
rem Reinstall only when requirements.txt has changed since the last install.
fc /b requirements.txt .venv\installed.txt >nul 2>nul && goto run
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo.
  echo Installing dependencies failed. Check your internet connection and run start.bat again.
  pause
  exit /b 1
)
copy /y requirements.txt .venv\installed.txt >nul

:run
".venv\Scripts\python.exe" connect.py %*
if errorlevel 1 pause
exit /b

:nopython
echo.
echo Python 3.10 or newer is needed. Get it from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" during install), then run start.bat again.
pause
exit /b 1
