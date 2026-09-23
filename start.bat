@echo off
rem ============================================
rem  Tokscale usage dashboard launcher (Windows)
rem  Double-click to (re)start the server and open
rem  the dashboard in your default browser.
rem  Restart semantics: any previously running
rem  dashboard process is terminated first.
rem ============================================
setlocal EnableExtensions
cd /d "%~dp0"

set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"
set "URL=http://127.0.0.1:8765/"
set "PROBE=http://127.0.0.1:8765/dashboard.html"
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
if not exist "%PY%" set "PY=pythonw"

rem ------------------------------------------------------------
rem  Step 1: kill any previously running dashboard server
rem  (python/pythonw running serve.py). No-op if none exists.
rem ------------------------------------------------------------
echo [1/4] Stopping any previously running dashboard process...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -like '*serve.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

rem ------------------------------------------------------------
rem  Step 2: start a fresh server (hidden window, no console).
rem ------------------------------------------------------------
echo [2/4] Starting server (hidden window)...
start "" /b "%PY%" "%CD%\serve.py" 8765
timeout /t 3 /nobreak >NUL 2>&1 || ping -n 4 127.0.0.1 >NUL

rem ------------------------------------------------------------
rem  Step 3: verify the server is up.
rem ------------------------------------------------------------
echo [3/4] Verifying dashboard is up...
curl -s -f -I -o NUL --noproxy "*" -m 8 "%PROBE%"
if %errorlevel% neq 0 goto fail

rem ------------------------------------------------------------
rem  Step 4: fire one background data refresh and open browser.
rem ------------------------------------------------------------
echo [4/4] Opening %URL% in your browser...
echo (triggering one background data refresh; the page will pick it up)
curl -s -X POST -o NUL --noproxy "*" -m 5 "%URL%api/refresh"
start "" "%URL%"
exit /b 0

:fail
rem  Port may be held by the desktop (EXE) edition, which this script
rem  does not kill. If it serves our dashboard, just open the browser.
curl -s -f -I -o NUL --noproxy "*" -m 5 "%PROBE%" && goto open
echo.
echo ERROR: the dashboard server did not start.
echo Possible causes:
echo   - port 8765 is occupied by another program
echo   - pythonw.exe is not available on this machine
echo.
echo You can start it manually in a terminal:
echo   python serve.py 8765
echo then open %URL%
echo.
pause
exit /b 1
