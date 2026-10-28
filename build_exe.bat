@echo off
REM Run this from the folder that contains inventory_app.py
REM Requires Python 3.9+ installed with "Add Python to PATH" ticked.

pip install pyinstaller pillow google-api-python-client google-auth-oauthlib

REM 1) The app itself (no black console window)
pyinstaller --noconfirm --onefile --windowed --name ModernInventory --collect-all googleapiclient --collect-all google_auth_oauthlib inventory_app.py

REM 2) Password-recovery tool (needs a console window to type into)
pyinstaller --noconfirm --onefile --console --name ModernInventory-Recover inventory_app.py

echo.
echo Done. Your programs are in the "dist" folder:
echo   dist\ModernInventory.exe          - the app
echo   dist\ModernInventory-Recover.exe  - run from a terminal with --recover
pause
