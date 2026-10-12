@echo off
rem Double-click this file on Windows to start Duplio.
rem
rem It runs Duplio straight from this folder: nothing to install, nothing to compile, nothing to download.
rem The one thing the computer needs is Python 3.11 or newer; if it is missing, this file says how to get it.

setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
title Duplio

set "HERE=%~dp0"

if not exist "%HERE%src\duplio\__init__.py" (
  echo.
  echo Duplio cannot find its program files next to this launcher.
  echo If you opened this from inside a ZIP file, close this window, right-click the ZIP file,
  echo choose "Extract All...", and start Duplio.bat from the new folder.
  echo.
  pause
  exit /b 1
)

rem ---- find a Python that is new enough
set "PYEXE="
set "PYARGS="

if defined DUPLIO_PYTHON set "PYEXE=%DUPLIO_PYTHON%"

if not defined PYEXE (
  where py >nul 2>&1
  if not errorlevel 1 (
    for %%V in (3.15 3.14 3.13 3.12 3.11) do (
      if not defined PYEXE (
        py -%%V -c "import sys" >nul 2>&1
        if not errorlevel 1 (
          set "PYEXE=py"
          set "PYARGS=-%%V"
        )
      )
    )
  )
)

if not defined PYEXE (
  for %%C in (python python3) do (
    if not defined PYEXE (
      %%C -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
      if not errorlevel 1 set "PYEXE=%%C"
    )
  )
)

if not defined PYEXE goto :nopython

rem ---- run Duplio from this folder: its own code first, then the bundled libraries
if defined PYTHONPATH (
  set "PYTHONPATH=%HERE%src;%HERE%vendor;%PYTHONPATH%"
) else (
  set "PYTHONPATH=%HERE%src;%HERE%vendor"
)
set "PYTHONUTF8=1"

"%PYEXE%" %PYARGS% -m duplio %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" if not "%RC%"=="130" (
  echo.
  echo Duplio stopped with an error ^(code %RC%^). The messages above say why.
  pause
)
exit /b %RC%

:nopython
rem Delayed expansion is switched on only here (for the answer typed below); with it on, a "!" in a folder name or in
rem an argument would be swallowed.
setlocal EnableDelayedExpansion
echo.
echo Duplio needs Python 3.11 or newer, and this computer does not have it yet.
echo Python is a free, widely used program. Duplio itself installs nothing else.
echo.
where winget >nul 2>&1
if not errorlevel 1 (
  set "ANSWER="
  set /p "ANSWER=Install Python now with Windows' own installer (winget)? [y/N] "
  if /i "!ANSWER!"=="y" (
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    echo.
    echo When the installation has finished, close this window and double-click Duplio.bat again.
    pause
    exit /b 0
  )
)
echo To install it yourself: open https://www.python.org/downloads/ , download the installer,
echo tick "Add python.exe to PATH" on its first screen, finish it, then double-click Duplio.bat again.
echo.
pause
exit /b 1
