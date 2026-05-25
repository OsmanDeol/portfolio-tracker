@echo off
title Portfolio Tracker — Local Dev Build
echo.
echo  ============================================
echo   Portfolio Tracker  —  Local Dev Build
echo  ============================================
echo.
echo  NOTE: This produces a DEV build (no auto-update).
echo  To release a real versioned build, push a git tag:
echo.
echo    git tag v1.2.0
echo    git push origin v1.2.0
echo.
echo  GitHub Actions will build + publish automatically.
echo  ─────────────────────────────────────────────────
echo.

REM ── Step 1: Dependencies ─────────────────────────────────
echo [1/4] Installing dependencies...
pip install flask yfinance flask-cors pyinstaller pillow groq
if errorlevel 1 ( echo ERROR: pip install failed & pause & exit /b 1 )

REM ── Step 2: Icon ─────────────────────────────────────────
echo [2/4] Generating app icon...
python create_icon.py
if errorlevel 1 ( echo WARNING: Icon generation failed, building without icon )

REM ── Step 3: PyInstaller ──────────────────────────────────
echo [3/4] Cleaning previous build...
if exist dist\PortfolioTracker rmdir /s /q dist\PortfolioTracker
if exist build rmdir /s /q build
if exist PortfolioTracker.spec del /q PortfolioTracker.spec

echo        Building exe with PyInstaller...
pyinstaller ^
  --name "PortfolioTracker" ^
  --onedir ^
  --windowed ^
  --icon=icon.ico ^
  --add-data "templates;templates" ^
  --hidden-import flask ^
  --hidden-import flask_cors ^
  --hidden-import yfinance ^
  --hidden-import werkzeug ^
  --hidden-import jinja2 ^
  --hidden-import zoneinfo ^
  --hidden-import _strptime ^
  --collect-all yfinance ^
  main.py

if errorlevel 1 ( echo. & echo ERROR: PyInstaller build failed & pause & exit /b 1 )

REM ── Step 4: Inno Setup installer ─────────────────────────
echo [4/4] Building installer (PortfolioTracker-Setup.exe)...

REM Search for Inno Setup compiler in common locations
set ISCC=
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe"       set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles(x86)%\Inno Setup 5\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 5\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 5\ISCC.exe"       set "ISCC=%ProgramFiles%\Inno Setup 5\ISCC.exe"

if "%ISCC%"=="" goto :no_inno

if exist Output rmdir /s /q Output
"%ISCC%" installer.iss
if errorlevel 1 ( echo ERROR: Inno Setup compile failed & pause & exit /b 1 )

echo.
echo  ============================================
echo   ALL DONE!
echo  ============================================
echo.
echo   Installer:  Output\PortfolioTracker-Setup.exe
echo.
echo   Share that file or run it on any Windows PC.
echo   It installs like any normal app — Start Menu,
echo   Desktop shortcut, and Add/Remove Programs.
echo.
pause
exit /b 0

:no_inno
echo.
echo  ─────────────────────────────────────────────
echo   Inno Setup not found.
echo   Download it free from:
echo   https://jrsoftware.org/isdl.php
echo   (Download "Inno Setup 6" — the first link)
echo.
echo   After installing Inno Setup, run build.bat
echo   again and it will produce the Setup.exe
echo   automatically.
echo  ─────────────────────────────────────────────
echo.
echo   For now the raw app is at:
echo   dist\PortfolioTracker\PortfolioTracker.exe
echo.
pause
