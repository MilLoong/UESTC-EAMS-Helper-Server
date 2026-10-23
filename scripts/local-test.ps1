param([switch]$Lan, [string]$Ip = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command python -ErrorAction Stop).Source }
$arguments = @('-m', 'server.dev')
if (-not $Lan) { $arguments += '--public' }
if ($Ip) { $arguments += @('--ip', $Ip) }
Push-Location $root
try { & $python @arguments; if ($LASTEXITCODE -ne 0) { throw 'Server exited with an error' } }
finally { Pop-Location }
