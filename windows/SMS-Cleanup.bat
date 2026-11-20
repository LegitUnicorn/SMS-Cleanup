@echo off
setlocal
title SMS backup cleanup
rem Double-click this file. It installs/updates the cleanup tool, then starts the guided run.
rem Needs Python 3 (https://www.python.org/downloads/) and Google Drive for desktop.

set "PY=py"
where py >nul 2>nul
if errorlevel 1 set "PY=python"
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python is not installed.
  echo Install it from https://www.python.org/downloads/  ^(keep "Add python.exe to PATH" ticked^),
  echo then double-click this file again.
  echo.
  pause
  exit /b 1
)

echo Getting the latest version of the cleanup tool...
%PY% -m pip install --quiet --disable-pip-version-check --force-reinstall --no-deps https://github.com/LegitUnicorn/SMS-Cleanup/archive/refs/heads/main.zip
if errorlevel 1 (
  echo.
  echo Could not install the tool ^(see the message above^). Check your internet connection and try again.
  pause
  exit /b 1
)

%PY% -m smscleanup wizard
echo.
pause
