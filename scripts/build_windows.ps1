$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ((Get-Location).Path -ne $repoRoot) {
    throw "Run from the repository root: powershell -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1"
}

@("requirements.txt", "requirements-build.txt", "packaging\CodexTokenMonitor.spec") | ForEach-Object {
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $_))) {
        throw "Missing required build file: $_"
    }
}

python -m PyInstaller --version 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is missing. Run: python -m pip install -r requirements-build.txt"
}

python -c "import customtkinter, tkinter; tkinter.Tcl()"
if ($LASTEXITCODE -ne 0) {
    throw "Python cannot load CustomTkinter and Tcl/Tk. Repair the Python Tk installation before building."
}

foreach ($directory in @("build", "dist")) {
    $path = Join-Path $repoRoot $directory
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}

python -m PyInstaller .\packaging\CodexTokenMonitor.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

$exePath = Join-Path $repoRoot "dist\CodexTokenMonitor\CodexTokenMonitor.exe"
if (-not (Test-Path -LiteralPath $exePath)) {
    throw "Build completed but the executable was not found: $exePath"
}

& $exePath --smoke
if ($LASTEXITCODE -ne 0) {
    throw "Portable smoke failed with exit code $LASTEXITCODE"
}

Write-Output "Windows portable build succeeded: $exePath"
