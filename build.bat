@echo off
title Portfolio Tracker — Build .exe
echo.
echo  ============================================
echo   Portfolio Tracker  Build Script
echo  ============================================
echo.

echo [1/3] Installing dependencies...
pip install flask yfinance flask-cors pyinstaller
if errorlevel 1 ( echo ERROR: pip install failed & pause & exit /b 1 )

echo [2/3] Cleaning previous build...
if exist dist\PortfolioTracker rmdir /s /q dist\PortfolioTracker
if exist build rmdir /s /q build

echo [3/3] Building with PyInstaller...
pyinstaller ^
  --name "PortfolioTracker" ^
  --onedir ^
  --windowed ^
  --add-data "templates;templates" ^
  --hidden-import flask ^
  --hidden-import flask_cors ^
  --hidden-import yfinance ^
  --hidden-import werkzeug ^
  --hidden-import werkzeug.security ^
  --hidden-import jinja2 ^
  --hidden-import zoneinfo ^
  --hidden-import _strptime ^
  --collect-all yfinance ^
  main.py

if errorlevel 1 ( echo ERROR: Build failed & pause & exit /b 1 )

echo.
echo  ============================================
echo   BUILD COMPLETE!
echo  ============================================
echo.
echo   App folder:  dist\PortfolioTracker\
echo   Run it:      dist\PortfolioTracker\PortfolioTracker.exe
echo.
echo   Your portfolio data is saved in:
echo   %USERPROFILE%\Documents\PortfolioTracker\
echo.
pause
