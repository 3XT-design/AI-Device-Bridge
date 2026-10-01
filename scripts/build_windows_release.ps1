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

$VenvDir = Join-Path $env:LOCALAPPDATA "AI-Device-Bridge\release-venv"
$Python = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $Python)) {
    py -3.12 -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the virtual environment." }
}

# Keep PySide6's deep package paths out of a potentially nested source ZIP directory.
& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip." }
& $Python -m pip install -e ".[release]"
if ($LASTEXITCODE -ne 0) { throw "Failed to install release dependencies." }

$DistDirectory = Join-Path $ProjectRoot "build\release-dist"
$WorkDirectory = Join-Path $ProjectRoot "build\release-work"
$PortableDirectory = Join-Path $DistDirectory "AI Device Bridge"
Remove-Item $DistDirectory, $WorkDirectory -Recurse -Force -ErrorAction SilentlyContinue
# PyInstaller's built-in PySide6 hooks collect the Qt libraries and plugins
# used by this Qt Widgets app. Collecting all of PySide6 also copies unused
# QML development artifacts and can exceed Windows path limits.
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --name "AI Device Bridge" `
    --paths (Join-Path $ProjectRoot "src") `
    --distpath $DistDirectory `
    --workpath $WorkDirectory `
    --exclude-module PySide6.QtQml `
    --exclude-module PySide6.QtQuick `
    --exclude-module PySide6.QtQuickControls2 `
    --collect-all fastapi `
    --collect-all uvicorn `
    --collect-all cryptography `
    --collect-all httpx `
    --collect-all pydantic `
    --collect-all pydantic_core `
    --collect-all starlette `
    --collect-submodules ai_device_bridge `
    (Join-Path $ProjectRoot "src\ai_device_bridge\__main__.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed to build the Windows application." }

Copy-Item (Join-Path $ProjectRoot "README.md") $PortableDirectory -Force
$ReleaseDirectory = Join-Path $ProjectRoot "release"
New-Item -ItemType Directory -Path $ReleaseDirectory -Force | Out-Null
$PortableZip = Join-Path $ReleaseDirectory "AI-Device-Bridge-portable-windows-x64.zip"
Compress-Archive -Path (Join-Path $PortableDirectory "*") -DestinationPath $PortableZip -Force

$IsccPath = $null
$IsccCommand = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
if ($IsccCommand) {
    $IsccPath = $IsccCommand.Source
} else {
    $Candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    )
    foreach ($Candidate in $Candidates) {
        if (Test-Path $Candidate) {
            $IsccPath = $Candidate
            break
        }
    }
}

if ($IsccPath) {
    & $IsccPath "/O$ReleaseDirectory" (Join-Path $ProjectRoot "installer\AI-Device-Bridge.iss")
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed to create the installer." }
    Write-Host "Installer created in $ReleaseDirectory"
} else {
    Write-Warning "Inno Setup 6 was not found; the portable Windows ZIP was created. Install Inno Setup 6 and rerun to create the installer."
}

Write-Host "Portable application package: $PortableZip"
