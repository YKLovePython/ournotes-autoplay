@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONPATH=src
set PYTHONIOENCODING=utf-8
python "tools\human_play.py" %*
echo.
pause
