@echo off
cd /d "%~dp0"
python rescue_desktop_icons.py
if errorlevel 9009 py -3 rescue_desktop_icons.py
pause
