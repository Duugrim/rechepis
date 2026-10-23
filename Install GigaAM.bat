@echo off
setlocal
cd /d "%~dp0"

title Install GigaAM
echo Installing the local speech model runtime (PyTorch CPU, about 1 GB) into .venv ...
echo.

if not exist ".venv\Scripts\python.exe" (
  echo .venv was not found. Create it first:
  echo     python -m venv .venv
  echo     .venv\Scripts\activate
  echo     pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m pip install -r requirements-gigaam.txt
if errorlevel 1 goto failed

rem The package is pinned to a commit that was tested with this program. --no-deps because its own pins
rem do not install on Python 3.14 and are not needed (we use the PyTorch path, not ONNX).
".venv\Scripts\python.exe" -m pip install --no-deps "https://github.com/salute-developers/GigaAM/archive/7447938d791c4f3e643386ee22c33777004293a5.zip"
if errorlevel 1 goto failed

echo.
echo Done. Restart Rechepis, open Settings - Recognition and download a model (about 430 MB).
pause
exit /b 0

:failed
echo.
echo Installation failed - see the messages above.
pause
exit /b 1
