@echo off
cd /d "%~dp0"

REM ── Check venv exists ───────────────────────────────────────────────────
if not exist "integratedvenv\Scripts\python.exe" (
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

REM ── Release port 8012 if still bound from a previous run ─────────────────
echo Releasing port 8012...
for /f "tokens=5" %%a in ('%SystemRoot%\System32\netstat.exe -aon ^| findstr :8012 2^>nul') do (
    %SystemRoot%\System32\taskkill.exe /F /PID %%a >nul 2>&1
)

REM ── Start server using venv Python directly (from backend/ so imports resolve) ──
echo.
echo Starting server at http://localhost:8014
echo Press Ctrl+C to stop.
echo.

cd backend
"%~dp0integratedvenv\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port 8014

pause
