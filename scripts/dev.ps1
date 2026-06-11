<#
.SYNOPSIS
  Local dev launcher for cli-mcp-tool.

.DESCRIPTION
  Creates a Python venv under server/.venv, installs deps, and runs uvicorn
  with auto-reload. Run from the repository root.

.EXAMPLE
  ./scripts/dev.ps1
#>

$ErrorActionPreference = "Stop"

$repo   = Split-Path -Parent $PSScriptRoot
$server = Join-Path $repo "server"
$venv   = Join-Path $server ".venv"

Push-Location $server
try {
  if (-not (Test-Path $venv)) {
    Write-Host "Creating venv at $venv ..." -ForegroundColor Cyan
    python -m venv .venv
  }

  $py = Join-Path $venv "Scripts/python.exe"
  & $py -m pip install --upgrade pip
  & $py -m pip install -e ".[dev]"

  $env:DEV_RELOAD = "1"
  $env:LOG_LEVEL  = "DEBUG"
  & $py -m app.main
}
finally {
  Pop-Location
}
