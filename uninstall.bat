@echo off
title Portfolio Tracker — Uninstall
echo.
echo  ============================================
echo   Portfolio Tracker  —  Uninstall
echo  ============================================
echo.
echo  This will remove the app and shortcuts.
echo  Your portfolio DATA will NOT be deleted.
echo  (Saved in %USERPROFILE%\Documents\PortfolioTracker\)
echo.
set /p CONFIRM= Type YES to continue:
if /i not "%CONFIRM%"=="YES" ( echo Cancelled. & pause & exit /b 0 )

echo.

REM ── Stop any running instance ─────────────────────────────
taskkill /im PortfolioTracker.exe /f >nul 2>&1

REM ── Remove app files ──────────────────────────────────────
set "INST=%LOCALAPPDATA%\PortfolioTracker"
if exist "%INST%" (
    rmdir /s /q "%INST%"
    echo  Removed: %INST%
)

REM ── Remove Desktop shortcut ───────────────────────────────
set "DS=%USERPROFILE%\Desktop\Portfolio Tracker.lnk"
if exist "%DS%" ( del /q "%DS%" & echo  Removed Desktop shortcut )

REM ── Remove Start Menu shortcut ────────────────────────────
set "SM=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Portfolio Tracker.lnk"
if exist "%SM%" ( del /q "%SM%" & echo  Removed Start Menu shortcut )

echo.
echo  ============================================
echo   Uninstalled. Your portfolio data is safe at:
echo   %USERPROFILE%\Documents\PortfolioTracker\
echo  ============================================
echo.
pause
