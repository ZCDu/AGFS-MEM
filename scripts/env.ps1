# env.ps1 - single entry point for working with the API from PowerShell.
#
# Dot-source it (note the leading dot and space):
#
#     . .\env.ps1
#     . .\env.ps1 -UserId assess
#
# Running it as `.\env.ps1` will appear to work and then leave nothing
# defined, because a script gets its own scope and discards it on exit.
#
# This used to only set $U and $J, while the `mem` command lived separately
# in memory.psm1 — so following one instruction and then the other left you
# with variables but no command, or a command but no variables. It now sets
# up both.

param(
    [string]$UserId  = 'demo',
    [string]$BaseUrl = 'http://127.0.0.1:8000'
)

$modulePath = Join-Path $PSScriptRoot 'memory.psm1'
if (Test-Path $modulePath) {
    Import-Module $modulePath -Force -Global
    Connect-Memory -BaseUrl $BaseUrl -UserId $UserId
} else {
    Write-Host "  memory.psm1 not found next to env.ps1 - 'mem' will be unavailable." -ForegroundColor Yellow
    Write-Host "  You are probably running an older extract of the zip." -ForegroundColor Yellow
}

# Kept for the raw Invoke-RestMethod examples in the README, which use these
# directly rather than going through `mem`.
$global:U = "$($BaseUrl.TrimEnd('/'))/v1/users/$UserId"
$global:J = 'application/json'

Write-Host "  `$U   = $global:U"
Write-Host "  `$J   = $global:J"
Write-Host "  mem  = $(if (Get-Command mem -ErrorAction SilentlyContinue) { 'ready' } else { 'NOT AVAILABLE' })"
