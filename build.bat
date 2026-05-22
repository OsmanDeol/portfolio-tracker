@echo off
title Portfolio Tracker — Windows Build
echo.
echo  ============================================
echo   Portfolio Tracker  Build Script
echo  ============================================
echo.

echo [1/4] Installing dependencies...
pip install flask yfinance flask-cors pywebview pyinstaller --quiet
if errorlevel 1 ( echo ERROR: pip install failed & pause & exit /b 1 )

echo [2/4] Cleaning previous build...
if exist dist\PortfolioTracker rmdir /s /q dist\PortfolioTracker
if exist build rmdir /s /q build
if exist PortfolioTracker.spec del /q PortfolioTracker.spec

echo [3/4] Building .exe with PyInstaller...
pyinstaller ^
  --name "PortfolioTracker" ^
  --windowed ^
  --onedir ^
  --add-data "templates;templates" ^
  --hidden-import flask ^
  --hidden-import flask_cors ^
  --hidden-import yfinance ^
  --hidden-import werkzeug ^
  --hidden-import werkzeug.security ^
  --hidden-import jinja2 ^
  --hidden-import webview ^
  --hidden-import zoneinfo ^
  --hidden-import _strptime ^
  --collect-all yfinance ^
  --collect-all webview ^
  main.py

if errorlevel 1 ( echo ERROR: PyInstaller build failed & pause & exit /b 1 )

echo [4/4] Build complete!
echo.
echo  Your app is in:  dist\PortfolioTracker\
echo  Run it with:     dist\PortfolioTracker\PortfolioTracker.exe
echo.
echo  Data (portfolio.db) is saved in:
echo  %%USERPROFILE%%\Documents\PortfolioTracker\
echo.
pause
