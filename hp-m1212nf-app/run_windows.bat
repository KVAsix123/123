@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"

if not exist ".venv\Scripts\pythonw.exe" (
    echo Первый запуск: создаю окружение и устанавливаю зависимости...
    %PY% -m venv .venv || goto :nopython
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
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
echo Не удалось установить зависимости. Проверьте подключение к интернету.
pause
exit /b 1
