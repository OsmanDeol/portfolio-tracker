@echo off
title Portfolio Tracker
cd /d "%~dp0"

echo  Starting Portfolio Tracker...

REM ── Start Flask server silently in background ──────────────
start /min "PT-Flask" python app.py

REM ── Wait 3 seconds for Flask to be ready ──────────────────
timeout /t 3 /nobreak >nul

REM ── Try Edge first (pre-installed on Win 10/11) ────────────
set EDGE1="%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
set EDGE2="%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
set CHROME="%ProgramFiles%\Google\Chrome\Application\chrome.exe"
set CHROMEX="%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"

if exist %EDGE1% (
    %EDGE1% --app=http://127.0.0.1:5000 --window-size=1440,880 --no-first-run --user-data-dir="%USERPROFILE%\Documents\PortfolioTracker\browser-profile"
    goto :done
)
if exist %EDGE2% (
    %EDGE2% --app=http://127.0.0.1:5000 --window-size=1440,880 --no-first-run --user-data-dir="%USERPROFILE%\Documents\PortfolioTracker\browser-profile"
    goto :done
)
if exist %CHROME% (
    %CHROME% --app=http://127.0.0.1:5000 --window-size=1440,880 --no-first-run
    goto :done
)
if exist %CHROMEX% (
    %CHROMEX% --app=http://127.0.0.1:5000 --window-size=1440,880 --no-first-run
    goto :done
)

REM ── Fallback: open in default browser ─────────────────────
start http://127.0.0.1:5000

:done
REM ── Kill Flask when the window is closed ──────────────────
taskkill /fi "WindowTitle eq PT-Flask" /f >nul 2>&1
