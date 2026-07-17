@echo off
REM Launcher for the Bank Statement to Excel Converter.
REM Once this folder is on your PATH (install.bat does that for you), you can
REM run  statements "C:\path\to\pdf\folder"  from any terminal, in any folder.
REM
REM It uses the private environment created by install.bat (the .venv folder),
REM so it never depends on system-wide Python packages.

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo The tool has not been set up yet.
    echo Please run  install.bat  in this folder first:
    echo     %~dp0install.bat
    exit /b 1
)

"%~dp0.venv\Scripts\python.exe" "%~dp0parse_statements.py" %*
