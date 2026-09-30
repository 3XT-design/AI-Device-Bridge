$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python launcher 'py' was not found. Install Python 3.12 first."
}

py -3.12 --version
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.12 was not found. Install Python 3.12 and try again."
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the virtual environment." }
}

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip." }

& $Python -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "Failed to install project dependencies." }

Write-Host "Development environment is ready. Run .\scripts\run_windows.ps1 to start the app."
