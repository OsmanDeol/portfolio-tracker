@echo off
title Portfolio Tracker — Build
echo.
echo  ============================================
echo   Portfolio Tracker  —  Build Script
echo  ============================================
echo.

echo [1/4] Installing dependencies...
pip install flask yfinance flask-cors pyinstaller pillow
if errorlevel 1 ( echo ERROR: pip install failed & pause & exit /b 1 )

echo [2/4] Generating app icon...
python create_icon.py
if errorlevel 1 ( echo WARNING: Icon generation failed, building without icon )

echo [3/4] Cleaning previous build...
if exist dist\PortfolioTracker rmdir /s /q dist\PortfolioTracker
if exist build rmdir /s /q build
if exist PortfolioTracker.spec del /q PortfolioTracker.spec

echo [4/4] Building with PyInstaller...
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

if errorlevel 1 ( echo. & echo ERROR: Build failed & pause & exit /b 1 )

echo.
echo  ============================================
echo   BUILD COMPLETE!
echo  ============================================
echo.
echo   Now run  install.bat  to install the app
echo   and add it to your Desktop + Start Menu.
echo.
pause
