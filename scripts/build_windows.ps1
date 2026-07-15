$ErrorActionPreference = "Stop"

function Invoke-IsolatedPortableSmoke {
    param([Parameter(Mandatory = $true)][string]$PortableExe)

    $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd([char[]]"\/")
    $smokePrefix = "$tempRoot$([System.IO.Path]::DirectorySeparatorChar)CodexTokenMonitor-Smoke-"
    $smokeDataDir = [System.IO.Path]::GetFullPath(
        (Join-Path $tempRoot ("CodexTokenMonitor-Smoke-" + [guid]::NewGuid().ToString("N")))
    )
    if (-not $smokeDataDir.StartsWith($smokePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe portable smoke data directory: $smokeDataDir"
    }

    $hadOriginalDataDir = Test-Path Env:CODEX_TOKEN_MONITOR_DATA_DIR
    $originalDataDir = $env:CODEX_TOKEN_MONITOR_DATA_DIR
    try {
        New-Item -ItemType Directory -Path $smokeDataDir | Out-Null
        $env:CODEX_TOKEN_MONITOR_DATA_DIR = $smokeDataDir
        # The executable uses the Windows GUI subsystem, so direct invocation
        # may return before startup completes. Wait for the real process.
        $smokeProcess = Start-Process -FilePath $PortableExe -ArgumentList "--smoke" `
            -Wait -PassThru -WindowStyle Hidden
        $smokeExitCode = $smokeProcess.ExitCode
        if ($smokeExitCode -ne 0) {
            throw "Portable smoke failed with exit code $smokeExitCode"
        }

        $trendDatabase = Join-Path $smokeDataDir "data\usage-history.sqlite3"
        if (-not (Test-Path -LiteralPath $trendDatabase -PathType Leaf)) {
            throw "Portable smoke did not initialize the trend database: $trendDatabase"
        }
        $databaseBytes = [System.IO.File]::ReadAllBytes($trendDatabase)
        if (
            $databaseBytes.Length -lt 16 -or
            [System.Text.Encoding]::ASCII.GetString($databaseBytes, 0, 15) -ne "SQLite format 3" -or
            $databaseBytes[15] -ne 0
        ) {
            throw "Portable smoke initialized an invalid trend database: $trendDatabase"
        }
        return $smokeExitCode
    }
    finally {
        if ($hadOriginalDataDir) {
            $env:CODEX_TOKEN_MONITOR_DATA_DIR = $originalDataDir
        } else {
            Remove-Item Env:CODEX_TOKEN_MONITOR_DATA_DIR -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $smokeDataDir) {
            Remove-Item -LiteralPath $smokeDataDir -Recurse -Force
        }
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ((Get-Location).Path -ne $repoRoot) {
    throw "Run from the repository root: powershell -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1"
}

@("requirements.txt", "requirements-build.txt", "packaging\CodexTokenMonitor.spec") | ForEach-Object {
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $_))) {
        throw "Missing required build file: $_"
    }
}

$venvPython = Join-Path $repoRoot ".venv-build\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython) {
    $venvPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}

& $python -m PyInstaller --version 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is missing. Run: python -m pip install -r requirements-build.txt"
}

foreach ($directory in @("build", "dist")) {
    $path = Join-Path $repoRoot $directory
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}

$venvConfig = Join-Path $repoRoot ".venv-build\pyvenv.cfg"
$basePrefix = if (Test-Path -LiteralPath $venvConfig) {
    $homeLine = Get-Content -LiteralPath $venvConfig -Encoding UTF8 | Where-Object { $_ -match "^home\s*=" } | Select-Object -First 1
    ($homeLine -replace "^home\s*=\s*", "").Trim()
} else {
    (& $python -c "import sys; print(sys.base_prefix)").Trim()
}
$tclRoot = Join-Path $basePrefix "tcl"
if ($tclRoot -match "[^\x00-\x7F]") {
    # Tcl 8.6 can misdecode a non-ASCII library path in some embedded runtimes.
    # Copy only build-time Tcl assets to the repository's ASCII-only build path.
    $buildTclRoot = Join-Path $repoRoot "build\tcl-runtime"
    New-Item -ItemType Directory -Path $buildTclRoot -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $tclRoot "tcl8.6") -Destination $buildTclRoot -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $tclRoot "tk8.6") -Destination $buildTclRoot -Recurse -Force
    $env:TCL_LIBRARY = Join-Path $buildTclRoot "tcl8.6"
    $env:TK_LIBRARY = Join-Path $buildTclRoot "tk8.6"
}

& $python -c "import customtkinter, tkinter; tkinter.Tcl()"
if ($LASTEXITCODE -ne 0) {
    throw "Python cannot load CustomTkinter and Tcl/Tk. Repair the Python Tk installation before building."
}

& $python -m PyInstaller .\packaging\CodexTokenMonitor.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

$exePath = Join-Path $repoRoot "dist\CodexTokenMonitor\CodexTokenMonitor.exe"
if (-not (Test-Path -LiteralPath $exePath)) {
    throw "Build completed but the executable was not found: $exePath"
}

$smokeExitCode = Invoke-IsolatedPortableSmoke -PortableExe $exePath

Write-Output "Windows portable build succeeded: $exePath"
Write-Output "portable_smoke_exit_code=$smokeExitCode"
Write-Output "portable_smoke_database_initialized=yes"
