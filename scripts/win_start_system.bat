@echo off
REM Script for launching the Email-Telegram bot system via an interactive shell
echo Launching the Email-Telegram bot management system...

REM Change to the project root directory
cd /d %~dp0..

REM Activate the virtual environment
call .venv\Scripts\activate.bat

REM Set PYTHONPATH to include the project root
set PYTHONPATH=%CD%

REM Run the management tool
python -m src.cli.tools system shell

REM Deactivate the virtual environment when done
call deactivate

REM If the script fails, do not close the window immediately
if errorlevel 1 (
    echo.
    echo An error occurred while starting. Press any key to exit...
    pause >nul
)