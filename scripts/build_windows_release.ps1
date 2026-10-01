$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$BasePython = "python"
$PythonArguments = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $BasePython = "py"
    $PythonArguments = @("-3.12")
}
& $BasePython @PythonArguments -c "import sys; assert sys.version_info[:2] == (3, 12)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.12 was not found. Install Python 3.12 and try again."
}

$VenvDir = Join-Path $env:LOCALAPPDATA "AI-Device-Bridge\release-venv"
$Python = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $Python)) {
    & $BasePython @PythonArguments -m venv $VenvDir
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
New-Item -ItemType Directory -Path $WorkDirectory -Force | Out-Null
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
    --specpath $WorkDirectory `
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
$InstallerPath = Join-Path $ReleaseDirectory "AI-Device-Bridge-Setup.exe"
Remove-Item $InstallerPath -Force -ErrorAction SilentlyContinue
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
    if (-not (Test-Path $InstallerPath)) { throw "Installer output was not found." }
    Write-Host "Installer created in $ReleaseDirectory"
} else {
    Write-Warning "Inno Setup 6 was not found; the portable Windows ZIP was created. Install Inno Setup 6 and rerun to create the installer."
}

Write-Host "Portable application package: $PortableZip"
$Artifacts = @($PortableZip)
if (Test-Path $InstallerPath) { $Artifacts += $InstallerPath }
$Checksums = foreach ($Artifact in $Artifacts) {
    $Hash = (Get-FileHash -LiteralPath $Artifact -Algorithm SHA256).Hash.ToLowerInvariant()
    "$Hash  $(Split-Path $Artifact -Leaf)"
}
$Checksums | Set-Content (Join-Path $ReleaseDirectory "SHA256SUMS.txt") -Encoding ascii
