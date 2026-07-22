$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "The Windows application must be built on Windows."
}

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistDir = Join-Path $RootDir "dist\windows"
$WorkDir = Join-Path $RootDir "build\native-windows"
$BackendWorkDir = Join-Path $WorkDir "backend"
$BackendDistDir = Join-Path $WorkDir "backend-dist"
$Project = Join-Path $RootDir "native-windows\TimeLapseNative.csproj"
$BackendEntryPoint = Join-Path $RootDir "timelapse\native_backend.py"
$Artifact = Join-Path $DistDir "timelapse.exe"
$Runtime = if ($env:TIMELAPSE_WINDOWS_RUNTIME) { $env:TIMELAPSE_WINDOWS_RUNTIME } else { "win-x64" }

foreach ($Command in @("uv", "dotnet")) {
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "$Command is required on the build machine."
    }
}
if (-not (Test-Path -PathType Leaf $Project)) {
    throw "Native Windows project does not exist: $Project"
}

Set-Location $RootDir
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $WorkDir, $DistDir
New-Item -ItemType Directory -Force -Path $DistDir, $BackendWorkDir, $BackendDistDir | Out-Null

Write-Host "Synchronizing backend build dependencies..."
& uv sync --group dev
if ($LASTEXITCODE -ne 0) {
    throw "uv sync failed with exit code $LASTEXITCODE."
}

$PyInstallerArgs = @(
    "run", "pyinstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--console",
    "--name", "timelapse-backend",
    "--distpath", $BackendDistDir,
    "--workpath", $BackendWorkDir,
    "--specpath", $BackendWorkDir,
    "--paths", $RootDir,
    "--collect-submodules", "uiprotect.data",
    "--collect-submodules", "uiprotect.devices",
    "--collect-submodules", "uiprotect.events",
    "--exclude-module", "PySide6",
    "--exclude-module", "shiboken6",
    $BackendEntryPoint
)

Write-Host "Building the embedded Python export backend..."
& uv @PyInstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}
$BuiltBackend = Join-Path $BackendDistDir "timelapse-backend.exe"
if (-not (Test-Path -PathType Leaf $BuiltBackend)) {
    throw "PyInstaller completed without creating $BuiltBackend."
}

$HealthRequest = '{"id":"build-health","command":"health"}'
$HealthOutput = @($HealthRequest | & $BuiltBackend)
$HealthExitCode = $LASTEXITCODE
$HealthEvents = @(
    foreach ($Line in $HealthOutput) {
        try {
            $Line | ConvertFrom-Json -ErrorAction Stop
        }
        catch {
            Write-Warning "Ignoring non-JSON backend health output: $Line"
        }
    }
)
$CompletedHealthEvents = @(
    $HealthEvents | Where-Object { $_.id -eq "build-health" -and $_.event -eq "complete" }
)
if ($HealthExitCode -ne 0 -or $CompletedHealthEvents.Count -eq 0) {
    Write-Error "Backend health output: $($HealthOutput -join [Environment]::NewLine)"
    throw "The packaged backend health check failed."
}

Write-Host "Publishing the single-file native WPF application ($Runtime)..."
& dotnet publish $Project `
    --configuration Release `
    --runtime $Runtime `
    --self-contained true `
    --output $DistDir `
    -p:PublishSingleFile=true `
    -p:IncludeNativeLibrariesForSelfExtract=true `
    "-p:BackendExecutable=$BuiltBackend" `
    -p:DebugType=None `
    -p:DebugSymbols=false
if ($LASTEXITCODE -ne 0) {
    throw "dotnet publish failed with exit code $LASTEXITCODE."
}

if (-not (Test-Path -PathType Leaf $Artifact)) {
    throw "dotnet publish completed without creating $Artifact."
}
$PublishedFiles = @(Get-ChildItem -Path $DistDir -File -Recurse)
if ($PublishedFiles.Count -ne 1 -or $PublishedFiles[0].FullName -ne $Artifact) {
    $UnexpectedFiles = $PublishedFiles.FullName -join [Environment]::NewLine
    throw "The Windows build must contain exactly one distributable executable. Found:$([Environment]::NewLine)$UnexpectedFiles"
}

Write-Host ""
Write-Host "Build complete: $Artifact"
Write-Host "This is the only file that needs to be distributed."
Write-Host "It contains the native .NET UI, .NET runtime, Python backend, and all runtime dependencies."
