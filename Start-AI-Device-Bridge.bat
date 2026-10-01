@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "PROJECT_DIR=%~dp0"
if not exist "%PROJECT_DIR%pyproject.toml" (
    if exist "%PROJECT_DIR%ai-device-bridge\pyproject.toml" (
        set "PROJECT_DIR=%PROJECT_DIR%ai-device-bridge\"
    ) else (
        echo Could not find ai-device-bridge\pyproject.toml next to this launcher.
        goto :failed
    )
)

cd /d "%PROJECT_DIR%" || goto :failed
set "VENV_PY=%PROJECT_DIR%.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo Creating Python environment with Python 3.12...
    py -3.12 -m venv ".venv" >nul 2>&1
    if not exist "%VENV_PY%" (
        echo Trying Python 3.13...
        py -3.13 -m venv ".venv" >nul 2>&1
    )
    if not exist "%VENV_PY%" (
        echo Trying the default Python installation...
        python -c "import sys; assert sys.version_info.major == 3 and sys.version_info.minor in [12, 13]" >nul 2>&1
        if not errorlevel 1 python -m venv ".venv"
    )
)

if not exist "%VENV_PY%" (
    echo Python 3.12 or 3.13 is required. Install it, then run this file again.
    goto :failed
)

"%VENV_PY%" -c "import sys; assert sys.version_info.major == 3 and sys.version_info.minor in [12, 13]"
if errorlevel 1 (
    echo The existing .venv uses an unsupported Python version.
    echo Rename the .venv folder, then run this file again.
    goto :failed
)

echo Installing or updating AI Device Bridge dependencies...
"%VENV_PY%" -m pip install --disable-pip-version-check -e "."
if errorlevel 1 goto :failed

echo Starting AI Device Bridge...
"%VENV_PY%" -m ai_device_bridge
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo Startup failed. The messages above show what needs attention.
pause
exit /b 1
