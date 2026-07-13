function Get-ProjectVersion {
    param([Parameter(Mandatory = $true)][string]$RepoRoot)

    $versionFile = Join-Path $RepoRoot "app\version.py"
    if (-not (Test-Path -LiteralPath $versionFile)) {
        throw "Version source is missing: $versionFile"
    }
    $matches = @(Get-Content -LiteralPath $versionFile -Encoding UTF8 | Where-Object {
        $_ -match '^__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$'
    })
    if ($matches.Count -ne 1) {
        throw "Version source must contain exactly one semantic __version__ assignment: $versionFile"
    }
    if ($matches[0] -notmatch '^__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$') {
        throw "Invalid application version in $versionFile"
    }
    return $Matches[1]
}

function Find-InnoSetupCompiler {
    $candidates = @()
    if ($env:INNO_SETUP_COMPILER) {
        $candidates += $env:INNO_SETUP_COMPILER
    }
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) {
        $candidates += $command.Source
    }
    $candidates += @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw "issue=inno_setup_compiler_missing; install Inno Setup 6 or set INNO_SETUP_COMPILER"
}

function Assert-X64PortableExecutable {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $reader = [System.IO.BinaryReader]::new($stream)
        $stream.Position = 0x3c
        $peOffset = $reader.ReadInt32()
        $stream.Position = $peOffset
        if ($reader.ReadUInt32() -ne 0x00004550) {
            throw "Portable executable has an invalid PE signature: $Path"
        }
        $machine = $reader.ReadUInt16()
        if ($machine -ne 0x8664) {
            throw ("Portable executable is not Windows x64 (machine=0x{0:X4}): {1}" -f $machine, $Path)
        }
    }
    finally {
        $stream.Dispose()
    }
}
