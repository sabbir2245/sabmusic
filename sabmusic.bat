@echo off
cd /d "%~dp0"
python "%~dp0sabmusic.py" %*
if errorlevel 1 pause
