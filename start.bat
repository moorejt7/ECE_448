@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Open http://127.0.0.1:5000 in your browser.
".venv\Scripts\python.exe" app.py
pause
exit /b
:failed
echo Setup failed. Make sure Python 3.10 or later is installed and internet access is available.
pause
exit /b 1
