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
. (Join-Path $PSScriptRoot "installer_helpers.ps1")

$version = Get-ProjectVersion -RepoRoot $repoRoot
$iscc = Find-InnoSetupCompiler
$buildScript = Join-Path $repoRoot "scripts\build_windows.ps1"
$installerScript = Join-Path $repoRoot "installer\CodexTokenMonitor.iss"
$portableDir = Join-Path $repoRoot "dist\CodexTokenMonitor"
$portableExe = Join-Path $portableDir "CodexTokenMonitor.exe"
$installerDir = Join-Path $repoRoot "dist\installer"
$installerExe = Join-Path $installerDir "CodexTokenMonitor-Setup-$version.exe"
$sha256File = Join-Path $installerDir "CodexTokenMonitor-Setup-$version.sha256"
$windowsPowerShell = (Get-Command powershell.exe -ErrorAction Stop).Source

Push-Location $repoRoot
try {
    & $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $buildScript
    if ($LASTEXITCODE -ne 0) {
        throw "Portable build failed with exit code $LASTEXITCODE"
    }
    if (-not (Test-Path -LiteralPath $portableExe -PathType Leaf)) {
        throw "Portable executable is missing: $portableExe"
    }
    Assert-X64PortableExecutable -Path $portableExe
    $smokeExitCode = Invoke-IsolatedPortableSmoke -PortableExe $portableExe

    New-Item -ItemType Directory -Path $installerDir -Force | Out-Null
    & $iscc "/DAppVersion=$version" "/DPortableSourceDir=$portableDir" $installerScript
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup compilation failed with exit code $LASTEXITCODE"
    }
    if (-not (Test-Path -LiteralPath $installerExe -PathType Leaf)) {
        throw "Installer output is missing: $installerExe"
    }
    if ((Get-Item -LiteralPath $installerExe).Length -le 0) {
        throw "Installer output is empty: $installerExe"
    }

    $sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $installerExe).Hash.ToLowerInvariant()
    Set-Content -LiteralPath $sha256File -Value "$sha256 *$(Split-Path -Leaf $installerExe)" -Encoding ASCII

    $compilerVersion = (& $iscc /? | Select-Object -First 1).Trim()
    Write-Output "version=$version"
    Write-Output "portable_exe=$portableExe"
    Write-Output "portable_smoke_exit_code=$smokeExitCode"
    Write-Output "portable_smoke_database_initialized=yes"
    Write-Output "installer_exe=$installerExe"
    Write-Output "sha256=$sha256"
    Write-Output "sha256_file=$sha256File"
    Write-Output "inno_compiler=$iscc"
    Write-Output "inno_compiler_version=$compilerVersion"
}
finally {
    Pop-Location
}
