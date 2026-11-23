$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot
$scriptPath = Join-Path $PSScriptRoot "deploy.py"

Write-Host "ArtWebs NewGen" -ForegroundColor Cyan
Write-Host ""

$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($null -eq $launcher) {
    Write-Host "Python launcher 'py' was not found. Install Python 3.10 or newer." -ForegroundColor Red
} else {
    & py -3 $scriptPath
    if ($LASTEXITCODE -ne 0) {
        Write-Host "The program ended with an error. See the message above." -ForegroundColor Red
    }
}
