@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ========================================
echo  Peer Review App — install
echo ========================================
echo.

set "PYTHON="
set "PYTHONW="

REM Prefer a real Python install over the WindowsApps stub.
for %%P in (
  "%LocalAppData%\Programs\Python\Python313\python.exe"
  "%LocalAppData%\Programs\Python\Python312\python.exe"
  "%LocalAppData%\Programs\Python\Python311\python.exe"
  "%LocalAppData%\Programs\Python\Python310\python.exe"
  "%LocalAppData%\Programs\Python\Python39\python.exe"
) do (
  if not defined PYTHON if exist %%~P set "PYTHON=%%~P"
)

if not defined PYTHON (
  where python >nul 2>&1
  if not errorlevel 1 (
    for /f "delims=" %%I in ('where python') do (
      echo %%I | findstr /I "WindowsApps" >nul
      if errorlevel 1 if not defined PYTHON set "PYTHON=%%I"
    )
  )
)

if not defined PYTHON (
  echo Python was not found.
  echo Install Python 3 from https://www.python.org/downloads/
  echo and check "Add python.exe to PATH", then run install.bat again.
  pause
  exit /b 1
)

for %%I in ("%PYTHON%") do set "PYDIR=%%~dpI"
if exist "%PYDIR%pythonw.exe" (
  set "PYTHONW=%PYDIR%pythonw.exe"
) else (
  set "PYTHONW=%PYTHON%"
)

echo Using Python:
echo   %PYTHON%
echo.

echo Installing dependencies...
"%PYTHON%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo.
  echo Dependency install failed.
  pause
  exit /b 1
)

echo.
echo Creating Desktop shortcut...
"%PYTHON%" "%~dp0scripts\create_desktop_shortcut.py"
if errorlevel 1 (
  echo.
  echo Shortcut creation failed.
  pause
  exit /b 1
)

echo.
echo Done. Double-click "Peer Review App" on your Desktop to open it.
echo You can also use run.bat in this folder.
echo.
pause
exit /b 0
