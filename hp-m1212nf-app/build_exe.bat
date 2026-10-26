@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Собирает один файл dist\HP-M1212nf.exe, который можно запускать без Python.
if not exist ".venv\Scripts\python.exe" (
    echo Сначала запустите run_windows.bat, чтобы установить зависимости.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install pyinstaller || goto :fail
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed --name HP-M1212nf ^
    --hidden-import win32timezone app.py || goto :fail
echo.
echo Готово: dist\HP-M1212nf.exe
pause
exit /b 0

:fail
echo Сборка не удалась.
pause
exit /b 1
