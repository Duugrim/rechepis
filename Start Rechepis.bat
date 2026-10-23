@echo off
setlocal
cd /d "%~dp0"

title Rechepis
if not defined RECHEPIS_PORT set RECHEPIS_PORT=8765

rem A release ships its own Python in .\python. Then recordings, models and settings live in .\data,
rem so updating means unpacking the new version and moving that one folder.
if exist "python\python.exe" if not defined RECHEPIS_DATA set "RECHEPIS_DATA=%CD%\data"

echo Starting Rechepis...
echo The page http://127.0.0.1:%RECHEPIS_PORT% opens in your browser as soon as the server is ready.
echo The very first start can take a minute while Windows checks the freshly unpacked files.
echo Close this window to stop the server.
echo.

rem Other programs are never touched: if the port is taken, say so and stop.
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%RECHEPIS_PORT% .*LISTENING"') do (
  echo Port %RECHEPIS_PORT% is already in use ^(PID %%P^).
  echo Close that program, or pick another port before starting, for example:
  echo     set RECHEPIS_PORT=8800
  echo.
  pause
  exit /b 1
)

rem Open the page only once the server answers (it starts replying after the app has finished starting up).
rem A hidden PowerShell polls /api/health twice a second and gives up after about three minutes.
rem Set RECHEPIS_NO_BROWSER=1 to skip opening the browser.
if not defined RECHEPIS_NO_BROWSER start "" /min powershell -NoProfile -WindowStyle Hidden -Command "$u='http://127.0.0.1:%RECHEPIS_PORT%'; for($i=0;$i -lt 120;$i++){try{Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 ($u+'/api/health')|Out-Null;Start-Process $u;break}catch{Start-Sleep -Milliseconds 500}}"

if exist "python\python.exe" (
  "python\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %RECHEPIS_PORT%
) else if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %RECHEPIS_PORT%
) else (
  where py >nul 2>&1
  rem "if errorlevel" is evaluated when it runs; %ERRORLEVEL% would be expanded before "where" has run.
  if errorlevel 1 (
    python -m uvicorn app.main:app --host 127.0.0.1 --port %RECHEPIS_PORT%
  ) else (
    py -3 -m uvicorn app.main:app --host 127.0.0.1 --port %RECHEPIS_PORT%
  )
)

echo.
echo Server stopped.
pause
