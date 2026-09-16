[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $RootDir
$env:PYTHONUTF8 = "1"

foreach ($required in @("app.py", "requirements.txt", "constraints.txt", "scripts\validate-env.py")) {
    $requiredPath = Join-Path $RootDir $required
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Setup could not identify the project root at $RootDir (missing $required)."
    }
}

function Test-CompatiblePython {
    param([string]$Path)
    try {
        & $Path -c "import sys; raise SystemExit(0 if (3, 10) <= sys.version_info[:2] < (3, 15) else 1)" *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Find-CompatiblePython {
    if ($env:KA_CHING_PYTHON) {
        if (Test-CompatiblePython $env:KA_CHING_PYTHON) {
            return $env:KA_CHING_PYTHON
        }
        throw "KA_CHING_PYTHON is not a supported Python 3.10-3.14 interpreter: $env:KA_CHING_PYTHON"
    }

    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($version in @("3.13", "3.14", "3.12", "3.11", "3.10")) {
            $resolved = & $launcher.Source "-$version" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $resolved) {
                $candidate = ($resolved | Select-Object -Last 1).Trim()
                if (Test-CompatiblePython $candidate) { return $candidate }
            }
        }
    }

    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command -and (Test-CompatiblePython $command.Source)) {
        return $command.Source
    }

    $known = @()
    foreach ($version in @("313", "314", "312", "311", "310")) {
        if ($env:LOCALAPPDATA) {
            $known += Join-Path $env:LOCALAPPDATA "Programs\Python\Python$version\python.exe"
        }
        if ($env:ProgramFiles) {
            $known += Join-Path $env:ProgramFiles "Python$version\python.exe"
        }
    }
    foreach ($candidate in $known) {
        if ((Test-Path -LiteralPath $candidate -PathType Leaf) -and
            (Test-CompatiblePython $candidate)) {
            return $candidate
        }
    }
    return $null
}

function Invoke-Checked {
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$File failed with exit code $LASTEXITCODE"
    }
}

function Assert-SafeEnvironmentPath {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "$Path must not be a symbolic link or junction."
    }
    if (-not $item.PSIsContainer) {
        throw "$Path exists but is not a directory. Move it aside and rerun setup."
    }
}

function Remove-SafeDirectory {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    Assert-SafeEnvironmentPath $Path
    Remove-Item -LiteralPath $Path -Recurse -Force
}

function Move-SafeDirectory {
    param([string]$Source, [string]$Destination)
    Assert-SafeEnvironmentPath $Source
    if (Test-Path -LiteralPath $Destination) {
        throw "Cannot move $Source because $Destination already exists."
    }
    [IO.Directory]::Move($Source, $Destination)
}

function Test-EnvironmentHealth {
    param([string]$Path)
    $python = Join-Path $Path "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { return $false }
    try {
        & $python (Join-Path $RootDir "scripts\validate-env.py") $RootDir *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Test-EnvironmentCompleted {
    param([string]$Path)
    $python = Join-Path $Path "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { return $false }
    try {
        & $python (Join-Path $RootDir "scripts\validate-env.py") --completed $RootDir *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Adopt-LegacyEnvironment {
    param([string]$Path)
    $python = Join-Path $Path "Scripts\python.exe"
    $marker = Join-Path $Path ".ka-ching-environment.json"
    if ((Test-Path -LiteralPath $python -PathType Leaf) -and
        -not (Test-Path -LiteralPath $marker -PathType Leaf)) {
        & $python (Join-Path $RootDir "scripts\validate-env.py") --write $RootDir *> $null
    }
}

function Enter-SetupLock {
    param([string]$Path)
    for ($attempt = 0; $attempt -lt 3; $attempt++) {
        if (Test-Path -LiteralPath $Path) {
            $item = Get-Item -LiteralPath $Path -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "$Path must not be a symbolic link."
            }
            if ($item.PSIsContainer) {
                throw "$Path exists but is not a file."
            }
        }
        try {
            $stream = [IO.File]::Open(
                $Path,
                [IO.FileMode]::CreateNew,
                [IO.FileAccess]::ReadWrite,
                [IO.FileShare]::None
            )
            $bytes = [Text.Encoding]::UTF8.GetBytes("$PID`n")
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush()
            return $stream
        }
        catch [IO.IOException] {
            try {
                $probe = [IO.File]::Open(
                    $Path,
                    [IO.FileMode]::Open,
                    [IO.FileAccess]::ReadWrite,
                    [IO.FileShare]::None
                )
                $probe.Dispose()
                Remove-Item -LiteralPath $Path -Force
                continue
            }
            catch [IO.IOException] {
                throw "Another setup is already running. Wait for it to finish."
            }
        }
    }
    throw "Could not acquire the setup lock. Wait for other setup runs to finish."
}

function Exit-SetupLock {
    param([IO.FileStream]$Stream, [string]$Path)
    if ($Stream) { $Stream.Dispose() }
    if (Test-Path -LiteralPath $Path) {
        try {
            Remove-Item -LiteralPath $Path -Force
        }
        catch {
            Write-Warning "Setup completed but its lock could not be removed."
        }
    }
}

$LockPath = Join-Path $RootDir ".ka-ching-setup.lock"
$LockStream = Enter-SetupLock $LockPath
$VenvDir = Join-Path $RootDir ".venv"
$BackupDir = Join-Path $RootDir ".venv.previous"
$EnvironmentPathsReady = $false
$SetupComplete = $false

try {
    foreach ($path in @($VenvDir, $BackupDir)) {
        Assert-SafeEnvironmentPath $path
    }
    $EnvironmentPathsReady = $true

    $Python = Find-CompatiblePython
    if (-not $Python) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if (-not $winget) {
            throw "Python 3.10-3.14 is required. Install Python 3.13 from python.org or install App Installer (winget), then rerun setup."
        }

        Write-Host "Installing Python 3.13 for the current user..."
        $wingetArgs = @(
            "install", "--id", "Python.Python.3.13", "--exact", "--scope", "user",
            "--accept-package-agreements", "--accept-source-agreements"
        )
        & $winget.Source @wingetArgs
        $wingetExit = $LASTEXITCODE
        $Python = Find-CompatiblePython
        if (-not $Python) {
            throw "Python installation did not become available (winget exit $wingetExit). Open a new PowerShell window and rerun setup."
        }
    }

    $Version = & $Python --version
    Write-Host "Using $Version at $Python"

    # Resolve state left between moving the validated environment aside and
    # completing its replacement. Adopt legacy environments only when their
    # exact installed versions pass the current validator.
    Adopt-LegacyEnvironment $VenvDir
    Adopt-LegacyEnvironment $BackupDir
    if (Test-EnvironmentHealth $VenvDir) {
        Remove-SafeDirectory $BackupDir
    }
    elseif (Test-EnvironmentHealth $BackupDir) {
        Remove-SafeDirectory $VenvDir
        Move-SafeDirectory $BackupDir $VenvDir
        Write-Host "Recovered the previous validated .venv after an interrupted setup."
    }
    elseif (Test-EnvironmentCompleted $VenvDir) {
        Remove-SafeDirectory $BackupDir
    }
    elseif (Test-EnvironmentCompleted $BackupDir) {
        Remove-SafeDirectory $VenvDir
        Move-SafeDirectory $BackupDir $VenvDir
        Write-Host "Recovered the previous completed .venv after an interrupted setup."
    }
    else {
        Remove-SafeDirectory $VenvDir
        Remove-SafeDirectory $BackupDir
    }

    if (Test-EnvironmentCompleted $VenvDir) {
        Move-SafeDirectory $VenvDir $BackupDir
    }

    Invoke-Checked -File $Python -Arguments @("-m", "venv", $VenvDir)
    $VenvPython = Join-Path $VenvDir "Scripts\python.exe"
    Invoke-Checked -File $VenvPython -Arguments @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-Checked -File $VenvPython -Arguments @("-m", "pip", "install", "-r", "requirements.txt", "-c", "constraints.txt")
    Invoke-Checked -File $VenvPython -Arguments @((Join-Path $RootDir "scripts\validate-env.py"), "--write", $RootDir)
    $SetupComplete = $true

    try {
        Remove-SafeDirectory $BackupDir
    }
    catch {
        Write-Warning "Setup succeeded, but .venv.previous could not be removed. The next setup will retry cleanup."
    }

    $Poppler = Get-Command pdftotext -ErrorAction SilentlyContinue
    $PopplerAvailable = $false
    if ($Poppler) {
        try {
            & $Poppler.Source -v *> $null
            $PopplerAvailable = $LASTEXITCODE -eq 0
        }
        catch {
            $PopplerAvailable = $false
        }
    }
    if ($PopplerAvailable) {
        Write-Host "Poppler pdftotext: available"
    }
    else {
        Write-Host "Poppler pdftotext: unavailable; PyMuPDF and pypdf will be used locally."
    }

    Write-Host "`nSetup complete. Start the app with:"
    Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-windows.ps1`n"
}
finally {
    if ($EnvironmentPathsReady -and -not $SetupComplete) {
        try {
            if (-not (Test-EnvironmentCompleted $VenvDir) -and
                (Test-EnvironmentCompleted $BackupDir)) {
                Remove-SafeDirectory $VenvDir
                Move-SafeDirectory $BackupDir $VenvDir
                Write-Warning "Setup failed; restored the previous completed .venv."
            }
        }
        catch {
            Write-Warning "Setup failed and automatic environment cleanup was incomplete: $_"
        }
    }
    Exit-SetupLock $LockStream $LockPath
}
