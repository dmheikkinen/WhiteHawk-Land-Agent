@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title WhiteHawk Landman Pipeline

echo.
echo  ============================================================
echo   WhiteHawk Land Agent  -  Landman Pipeline
echo  ============================================================
echo.

REM ----------------------------------------------------------------
REM  1. Check Python
REM ----------------------------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo  [!!] Python not found.
    echo      Install Python 3.11+ from https://www.python.org/downloads/
    echo      Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

REM ----------------------------------------------------------------
REM  2. Create virtual environment if missing
REM ----------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo  [..] First run - setting up Python environment ^(one time only^)...
    python -m venv .venv
    if errorlevel 1 (
        echo  [!!] Failed to create Python environment.
        pause
        exit /b 1
    )
    echo  [OK] Environment created.
)

REM ----------------------------------------------------------------
REM  3. Install / verify dependencies
REM ----------------------------------------------------------------
echo  [..] Checking dependencies...
.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet --disable-pip-version-check 2>nul
if errorlevel 1 (
    echo  [!!] Failed to install dependencies. Check your internet connection.
    pause
    exit /b 1
)
echo  [OK] Dependencies ready.

REM ----------------------------------------------------------------
REM  4. Check for .env (API keys)
REM ----------------------------------------------------------------
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
    )
    echo.
    echo  [!!] First-time setup: your .env file is missing API keys.
    echo.
    echo      You need to add:
    echo        OPENAI_API_KEY   - from platform.openai.com/api-keys
    echo        SERPAPI_API_KEY  - from serpapi.com/manage-api-key
    echo.
    echo      Opening .env in Notepad now...
    echo      Save the file, then run this launcher again.
    echo.
    pause
    notepad .env
    exit /b 0
)

REM ----------------------------------------------------------------
REM  5. Check if something is already running on port 5000
REM ----------------------------------------------------------------
netstat -ano | findstr ":5000 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo  [OK] Server already running - opening browser...
    start http://127.0.0.1:5000
    exit /b 0
)

REM ----------------------------------------------------------------
REM  6. Open browser after 2-second delay (server needs time to start)
REM ----------------------------------------------------------------
start /B cmd /C "timeout /t 2 /nobreak >nul && start http://127.0.0.1:5000"

REM ----------------------------------------------------------------
REM  7. Start server
REM ----------------------------------------------------------------
echo  [OK] Starting server at http://127.0.0.1:5000
echo.
echo  Your browser will open automatically.
echo  Keep this window open while using the app.
echo  Close this window (or press Ctrl+C) to stop the server.
echo.
echo  ============================================================
echo.

.venv\Scripts\python.exe -m uvicorn server:app --host 127.0.0.1 --port 5000 --log-level warning

echo.
echo  Server stopped. Press any key to close.
pause >nul
