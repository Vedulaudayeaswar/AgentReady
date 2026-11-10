# PostgreSQL backup for Windows PowerShell (same as backup.sh).
#   .\scripts\backup.ps1        -> backups\agentready-YYYYMMDD-HHMMSS.dump
# The dump is written inside the container and copied out, so no binary data passes through the
# PowerShell pipeline (which would corrupt it).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
$name = "agentready-$stamp.dump"
New-Item -ItemType Directory -Force backups | Out-Null
docker compose exec -T postgres pg_dump -U agentready -d agentready -Fc -Z 6 -f "/tmp/$name"
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed" }
$tables = docker compose exec -T postgres sh -c "pg_restore --list /tmp/$name | grep -c 'TABLE DATA'"
if ($LASTEXITCODE -ne 0) { throw "backup archive is not readable" }
docker compose cp "postgres:/tmp/$name" "backups/$name"
docker compose exec -T postgres rm -f "/tmp/$name"
$size = (Get-Item "backups/$name").Length
Write-Output "archive verified: $tables tables with data"
Write-Output "backup written: backups\$name ($size bytes)"
