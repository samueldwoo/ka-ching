[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RootDir ".venv\Scripts\python.exe"
$Validator = Join-Path $RootDir "scripts\validate-env.py"

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    throw "Missing .venv. Run scripts\setup-windows.ps1 first."
}

$env:PYTHONUTF8 = "1"
Set-Location -LiteralPath $RootDir
& $VenvPython $Validator $RootDir
if ($LASTEXITCODE -ne 0) {
    throw "The .venv is incomplete or stale. Run scripts\setup-windows.ps1 again."
}
& $VenvPython app.py
exit $LASTEXITCODE
