@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"

if not exist ".venv\Scripts\pythonw.exe" (
    echo Первый запуск: создаю окружение. Это займёт 1-3 минуты...
    %PY% -m venv .venv || goto :nopython
)
rem Ставим зависимости при первом запуске и после обновления программы (изменился requirements.txt).
fc /b requirements.txt ".venv\installed-requirements.txt" >nul 2>nul
if errorlevel 1 (
    echo Устанавливаю компоненты программы...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
    copy /y requirements.txt ".venv\installed-requirements.txt" >nul
)
start "" ".venv\Scripts\pythonw.exe" app.py %*
exit /b 0

:nopython
echo.
echo Python не найден. Установите Python 3.10 или новее с https://www.python.org/downloads/
echo При установке отметьте галочку "Add python.exe to PATH".
pause
exit /b 1

:fail
echo.
echo Не удалось установить компоненты. Проверьте подключение к интернету и запустите ещё раз.
pause
exit /b 1
