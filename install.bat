@echo off
setlocal enabledelayedexpansion

REM ===========================================================================
REM  Bank Statement -> Excel Converter : Windows setup
REM
REM  Double-click this file, or run it from a terminal. It will:
REM    1. Make sure Python is installed (installs it with winget if missing)
REM    2. Create a self-contained virtual environment in the .venv folder
REM       and install the packages the tool needs INTO THAT FOLDER ONLY
REM       (nothing is installed system-wide, so system packages are untouched)
REM    3. Add this folder to your PATH so you can run  statements  anywhere
REM ===========================================================================

echo ==========================================================
echo   Bank Statement -^> Excel Converter : setup
echo ==========================================================
echo.

REM --- This folder, with no trailing backslash (matters for PATH) ------------
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

REM --- 1. Python -------------------------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo Python was not found. Trying to install it with winget...
    echo.
    winget install -e --id Python.Python.3.13 --silent --accept-source-agreements --accept-package-agreements
    if errorlevel 1 (
        echo.
        echo Could not install Python automatically.
        echo Please install it by hand from  https://www.python.org/downloads/
        echo IMPORTANT: on the first screen, tick "Add python.exe to PATH".
        echo Then run install.bat again.
        echo.
        pause
        exit /b 1
    )
    echo.
    echo Python was installed. Please CLOSE this window, open a NEW one,
    echo and run install.bat again so Windows can see Python.
    echo.
    pause
    exit /b 0
)

echo Found Python:
python --version
echo.

REM --- 2. Virtual environment + packages ------------------------------------
REM  Everything goes in .venv so we never touch system-wide packages. This
REM  also avoids the "externally-managed-environment" error that a plain
REM  system-wide  pip install  can raise on newer setups.
set "VENV_PY=%SCRIPT_DIR%\.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo Creating a private environment in  .venv ...
    python -m venv "%SCRIPT_DIR%\.venv"
    if errorlevel 1 (
        echo.
        echo Could not create the virtual environment - see the messages above.
        echo.
        pause
        exit /b 1
    )
)

echo Installing the packages the tool needs (this can take a minute)...
echo.
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install pdfplumber pandas XlsxWriter openpyxl
if errorlevel 1 (
    echo.
    echo Something went wrong installing the packages - see the messages above.
    echo.
    pause
    exit /b 1
)
echo.

REM --- 3. Add this folder to the user PATH -----------------------------------
echo %PATH% | find /I "%SCRIPT_DIR%" >nul
if errorlevel 1 (
    set "USERPATH="
    for /f "tokens=2,*" %%A in ('reg query HKCU\Environment /v PATH 2^>nul') do set "USERPATH=%%B"
    if defined USERPATH (
        setx PATH "!USERPATH!;%SCRIPT_DIR%" >nul
    ) else (
        setx PATH "%SCRIPT_DIR%" >nul
    )
    echo Added this folder to your PATH.
    echo   ^>^> You must open a NEW terminal window for that to take effect. ^<^<
) else (
    echo This folder is already on your PATH.
)
echo.

echo ==========================================================
echo   Setup complete!
echo ==========================================================
echo.
echo To use the tool, open a NEW terminal window and run:
echo.
echo     statements "C:\path\to\folder\of\pdfs"
echo.
echo That writes  Bank_Statements.xlsx  into the folder you run it from.
echo (Add  -o "C:\somewhere\MyBook.xlsx"  to choose a different output file.)
echo.
pause
