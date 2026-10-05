@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Bot dogovorov

py -3.12 -c "import sys" >nul 2>&1
if %errorlevel%==0 (
    py -3.12 bot_telegram.py
    goto end
)

python -c "import sys" >nul 2>&1
if %errorlevel%==0 (
    python bot_telegram.py
    goto end
)

echo.
echo Python ne nayden. Ustanovite Python s python.org

:end
echo.
echo ================================================
echo  Bot ostanovlen. Okno mozhno zakryt.
echo ================================================
pause
