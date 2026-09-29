@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Emmin - Eternal Mod Manager of Nefia

set "PYEXE="

rem 1. the Windows py launcher, which knows about every Python you've installed
py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"

rem 2. whatever "python" means on PATH today
if not defined PYEXE (
  python -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
  if not errorlevel 1 set "PYEXE=python"
)

rem 3. python3, for the rare setup that only has that
if not defined PYEXE (
  python3 -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
  if not errorlevel 1 set "PYEXE=python3"
)

rem 4. the usual install spots, PATH or no PATH
if not defined PYEXE (
  for %%D in (
    "%LocalAppData%\Programs\Python"
    "%ProgramFiles%"
    "%ProgramFiles(x86)%"
    "C:"
  ) do (
    for /d %%P in ("%%~D\Python3*") do (
      if exist "%%~P\python.exe" (
        "%%~P\python.exe" -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
        if not errorlevel 1 if not defined PYEXE set "PYEXE=%%~P\python.exe"
      )
    )
  )
)

if not defined PYEXE (
  echo.
  echo   Couldn't find a Python 3.8 or newer on this machine.
  echo.
  echo   Get it from python.org/downloads and tick "Add python.exe to PATH"
  echo   on the first screen of the installer. Then run this again.
  echo.
  echo   If you know you have Python but it isn't on PATH, you can run it by hand:
  echo       "C:\path\to\python.exe" Emmin.py
  echo.
  pause
  exit /b 1
)

%PYEXE% Emmin.py
if errorlevel 1 (
  echo.
  echo   The manager stopped with an error. The lines above say why.
  pause
)
