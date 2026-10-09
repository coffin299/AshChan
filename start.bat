@echo off
REM ============================================================
REM AshChan Bot launcher (Windows)
REM - Requires Python 3.12 (via the "py" launcher)
REM - Creates .venv and installs requirements on first run
REM - Generates config.yaml from config.default.yaml on first run
REM ============================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "HASH_FILE=%VENV_DIR%\.req_hash"

echo [AshChan] Starting launcher...

REM --- Check that Python 3.12 is available ---
py -3.12 --version >nul 2>&1
if errorlevel 1 (
    echo [AshChan] ERROR: Python 3.12 is required but was not found.
    echo [AshChan] Install it from https://www.python.org/downloads/
    echo [AshChan]   or run: winget install Python.Python.3.12
    pause
    exit /b 1
)

REM --- Recreate .venv if it was built with another Python version ---
if exist "%VENV_PY%" (
    "%VENV_PY%" -B -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
    if errorlevel 1 (
        echo [AshChan] Existing .venv is not Python 3.12. Recreating it...
        rmdir /s /q "%VENV_DIR%"
    )
)

REM --- Create .venv if missing ---
if not exist "%VENV_PY%" (
    echo [AshChan] Creating virtual environment with Python 3.12...
    py -3.12 -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [AshChan] ERROR: Failed to create the virtual environment.
        pause
        exit /b 1
    )
    echo [AshChan] Upgrading pip...
    "%VENV_PY%" -B -m pip install -U pip
    if errorlevel 1 (
        echo [AshChan] ERROR: Failed to upgrade pip.
        pause
        exit /b 1
    )
)

REM --- Install requirements only when requirements.txt has changed ---
set "NEW_HASH="
for /f "skip=1 delims=" %%H in ('certutil -hashfile requirements.txt SHA256') do (
    if not defined NEW_HASH set "NEW_HASH=%%H"
)
set "OLD_HASH="
if exist "%HASH_FILE%" set /p OLD_HASH=<"%HASH_FILE%"

if not "!NEW_HASH!"=="!OLD_HASH!" (
    echo [AshChan] Installing required packages...
    "%VENV_PY%" -B -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [AshChan] ERROR: Failed to install required packages.
        pause
        exit /b 1
    )
    > "%HASH_FILE%" echo !NEW_HASH!
) else (
    echo [AshChan] Packages are up to date.
)

REM --- Generate config.yaml on first run ---
if not exist "config.yaml" (
    copy /y "config.default.yaml" "config.yaml" >nul
    if errorlevel 1 (
        echo [AshChan] ERROR: Failed to create config.yaml.
        pause
        exit /b 1
    )
    echo [AshChan] config.yaml has been created from config.default.yaml.
    echo [AshChan] Please edit config.yaml ^(discord.token, llm.api_key, etc.^)
    echo [AshChan] and run start.bat again.
    pause
    exit /b 0
)

REM --- Run the bot without writing __pycache__ ---
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUTF8=1"
echo [AshChan] Launching bot...
"%VENV_PY%" -B -m ash
set "EXIT_CODE=%errorlevel%"

echo [AshChan] Bot exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
