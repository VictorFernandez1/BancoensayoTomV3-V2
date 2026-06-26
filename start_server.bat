@echo off
cd /d "%~dp0"

REM ── Check venv exists ───────────────────────────────────────────────────
if not exist "integratedvenv\Scripts\activate.bat" (
    echo ERROR: Virtual environment not found.
    echo Please create it first by running:
    echo.
    echo     python -m venv integratedvenv
    echo     integratedvenv\Scripts\activate
    echo     pip install -r backend\requirements.txt
    echo.
    pause
    exit /b 1
)

REM Activate virtual environment
echo Activating virtual environment...
call integratedvenv\Scripts\activate.bat

REM ── Release port 8000 if still bound from a previous run ─────────────────
echo Releasing port 8011...
for /f "tokens=5" %%a in ('%SystemRoot%\System32\netstat.exe -aon ^| findstr :8006 2^>nul') do (
    %SystemRoot%\System32\taskkill.exe /F /PID %%a >nul 2>&1
)

REM ── Start server ─────────────────────────────────────────────────────────
echo.
echo Starting server at http://localhost:8011
echo Press Ctrl+C to stop.
echo.
cd backend
python -m uvicorn main:app --host 0.0.0.0 --port 8011

pause
