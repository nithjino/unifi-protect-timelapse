$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "The Windows executable must be built on Windows."
}

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistDir = Join-Path $RootDir "dist\windows"
$WorkDir = Join-Path $RootDir "build\pyinstaller-windows"
$SpecDir = Join-Path $WorkDir "spec"
$EntryPoint = Join-Path $RootDir "timelapse\gui.py"
$DefaultIcon = Join-Path $RootDir "assets\icons\timelapse.ico"
$RuntimeIcon = Join-Path $RootDir "assets\icons\timelapse.png"
$IconPath = if ($env:TIMELAPSE_ICON) { $env:TIMELAPSE_ICON } else { $DefaultIcon }
$Artifact = Join-Path $DistDir "timelapse.exe"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required on the build machine. Install it from https://docs.astral.sh/uv/."
}

Set-Location $RootDir
New-Item -ItemType Directory -Force -Path $DistDir, $WorkDir, $SpecDir | Out-Null

Write-Host "Synchronizing build dependencies..."
& uv sync --group dev
if ($LASTEXITCODE -ne 0) {
    throw "uv sync failed with exit code $LASTEXITCODE."
}

$PyInstallerArgs = @(
    "run", "pyinstaller",
    "--noconfirm",
    "--clean",
    "--windowed",
    "--onefile",
    "--name", "timelapse",
    "--distpath", $DistDir,
    "--workpath", $WorkDir,
    "--specpath", $SpecDir,
    "--paths", $RootDir,
    "--collect-submodules", "uiprotect.data",
    "--collect-submodules", "uiprotect.devices",
    "--collect-submodules", "uiprotect.events",
    "--hidden-import", "keyring.backends.Windows",
    "--add-data", "${RuntimeIcon};timelapse_assets",
    "--icon", $IconPath
)

if (-not (Test-Path -PathType Leaf $RuntimeIcon)) {
    throw "Bundled application icon does not exist: $RuntimeIcon"
}
if (-not (Test-Path -PathType Leaf $IconPath)) {
    throw "Windows icon does not exist: $IconPath"
}

$PyInstallerArgs += $EntryPoint

Write-Host "Building timelapse.exe..."
& uv @PyInstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

if (-not (Test-Path -PathType Leaf $Artifact)) {
    throw "PyInstaller completed without creating $Artifact."
}

Write-Host ""
Write-Host "Build complete: $Artifact"
Write-Host "The executable contains Python, Qt, and all runtime dependencies."
