@echo off
setlocal
cd /d "%~dp0"

py -3 -m venv .venv
if errorlevel 1 (
    echo Failed to create virtual environment. Please install Python 3.12+ first.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo.
echo Install finished.
echo Make sure Google Chrome is installed on this computer.
pause
