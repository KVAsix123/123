@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Bot dogovorov

set PY=
py -3.12 -c "import sys" >nul 2>&1
if %errorlevel%==0 set PY=py -3.12
if not defined PY (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set PY=python
)
if not defined PY goto nopython

rem Bez python-docx knopka "Sformirovat dogovor" ne rabotaet - stavim v tot zhe Python
%PY% -c "import requests, docx" >nul 2>&1
if errorlevel 1 (
    echo Ustanavlivayu nedostayushchie biblioteki...
    %PY% -m pip install requests python-docx
)

%PY% bot_telegram.py
goto end

:nopython

echo.
echo Python ne nayden. Ustanovite Python s python.org

:end
echo.
echo ================================================
echo  Bot ostanovlen. Okno mozhno zakryt.
echo ================================================
pause
