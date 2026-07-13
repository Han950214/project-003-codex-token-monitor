$ErrorActionPreference = "Stop"

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
    & $portableExe --smoke
    if ($LASTEXITCODE -ne 0) {
        throw "Portable smoke failed with exit code $LASTEXITCODE"
    }

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
    Write-Output "portable_smoke_exit_code=0"
    Write-Output "installer_exe=$installerExe"
    Write-Output "sha256=$sha256"
    Write-Output "sha256_file=$sha256File"
    Write-Output "inno_compiler=$iscc"
    Write-Output "inno_compiler_version=$compilerVersion"
}
finally {
    Pop-Location
}
