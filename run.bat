@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHONW="
set "PYTHON="

for %%P in (
  "%LocalAppData%\Programs\Python\Python313\pythonw.exe"
  "%LocalAppData%\Programs\Python\Python312\pythonw.exe"
  "%LocalAppData%\Programs\Python\Python311\pythonw.exe"
  "%LocalAppData%\Programs\Python\Python310\pythonw.exe"
  "%LocalAppData%\Programs\Python\Python39\pythonw.exe"
) do (
  if not defined PYTHONW if exist %%~P set "PYTHONW=%%~P"
)

if not defined PYTHONW (
  where pythonw >nul 2>&1
  if not errorlevel 1 (
    for /f "delims=" %%I in ('where pythonw') do (
      echo %%I | findstr /I "WindowsApps" >nul
      if errorlevel 1 if not defined PYTHONW set "PYTHONW=%%I"
    )
  )
)

if defined PYTHONW (
  "%PYTHONW%" "%~dp0launch.pyw"
  exit /b %ERRORLEVEL%
)

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

if defined PYTHON (
  "%PYTHON%" "%~dp0launch.pyw"
  echo.
  echo If the window closed immediately, see launch_error.log
  pause
  exit /b %ERRORLEVEL%
)

echo Python was not found.
echo Run install.bat first, or install Python 3 and try again.
pause
exit /b 1
