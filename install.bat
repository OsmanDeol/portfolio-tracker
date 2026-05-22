@echo off
title Portfolio Tracker — Installer
echo.
echo  ============================================
echo   Portfolio Tracker  —  Installer
echo  ============================================
echo.

REM ── Check the build exists ────────────────────────────────
if not exist "dist\PortfolioTracker\PortfolioTracker.exe" (
    echo  ERROR: Build not found.
    echo  Please run  build.bat  first, then run this again.
    echo.
    pause
    exit /b 1
)

REM ── Install location ─────────────────────────────────────
set "INST=%LOCALAPPDATA%\PortfolioTracker"

echo  Installing to: %INST%
echo.

REM ── Stop any running instance ─────────────────────────────
taskkill /im PortfolioTracker.exe /f >nul 2>&1

REM ── Copy app files ────────────────────────────────────────
if exist "%INST%" rmdir /s /q "%INST%"
mkdir "%INST%"
xcopy /e /q /y "dist\PortfolioTracker\*" "%INST%\" >nul
if errorlevel 1 ( echo ERROR: Copy failed & pause & exit /b 1 )

REM ── Create Desktop shortcut via PowerShell ────────────────
echo  Creating Desktop shortcut...
powershell -NoProfile -Command ^
  "$s=(New-Object -COM WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Portfolio Tracker.lnk');" ^
  "$s.TargetPath='%INST%\PortfolioTracker.exe';" ^
  "$s.WorkingDirectory='%INST%';" ^
  "$s.IconLocation='%INST%\PortfolioTracker.exe,0';" ^
  "$s.Description='Portfolio Tracker — live stock portfolio';" ^
  "$s.Save()"
if errorlevel 1 ( echo WARNING: Desktop shortcut failed )

REM ── Create Start Menu shortcut ────────────────────────────
echo  Creating Start Menu shortcut...
set "SM=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
powershell -NoProfile -Command ^
  "$s=(New-Object -COM WScript.Shell).CreateShortcut('%SM%\Portfolio Tracker.lnk');" ^
  "$s.TargetPath='%INST%\PortfolioTracker.exe';" ^
  "$s.WorkingDirectory='%INST%';" ^
  "$s.IconLocation='%INST%\PortfolioTracker.exe,0';" ^
  "$s.Description='Portfolio Tracker — live stock portfolio';" ^
  "$s.Save()"
if errorlevel 1 ( echo WARNING: Start Menu shortcut failed )

echo.
echo  ============================================
echo   INSTALLED SUCCESSFULLY!
echo  ============================================
echo.
echo   - Desktop shortcut:   Portfolio Tracker
echo   - Start Menu:         Portfolio Tracker
echo   - App location:       %INST%
echo   - Portfolio data:     %USERPROFILE%\Documents\PortfolioTracker\
echo.
echo   You can now pin it to your Taskbar by right-clicking
echo   the Desktop shortcut and choosing "Pin to taskbar".
echo.
pause
