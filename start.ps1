$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
Write-Host 'Откройте в Opera: http://localhost:8766'
Write-Host 'Остановка локального сервера: Ctrl+C'
& (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') -X utf8 (Join-Path $PSScriptRoot 'native_server.py')
