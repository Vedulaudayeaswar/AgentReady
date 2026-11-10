# Restore a backup into a NEW database (default agentready_restore); never touches the live database.
#   .\scripts\restore.ps1 backups\agentready-....dump [target_db]
# To replace the live database use scripts/restore.sh with --replace-live (see DEPLOYMENT.md).
param([Parameter(Mandatory = $true)][string]$File, [string]$Target = "agentready_restore")
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if ($Target -eq "agentready") { throw "refusing to restore over the live database from PowerShell; see DEPLOYMENT.md" }
if (-not (Test-Path $File)) { throw "no such file: $File" }
$base = Split-Path $File -Leaf
$exists = docker compose exec -T postgres psql -U agentready -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$Target'"
if ($exists) { throw "database $Target already exists; choose another name or drop it first" }
docker compose cp $File "postgres:/tmp/$base"
docker compose exec -T postgres createdb -U agentready -O agentready $Target
docker compose exec -T postgres pg_restore -U agentready -d $Target --no-owner --exit-on-error "/tmp/$base"
if ($LASTEXITCODE -ne 0) { throw "pg_restore failed" }
docker compose exec -T postgres rm -f "/tmp/$base"
Write-Output "restored into database: $Target"
docker compose exec -T postgres psql -U agentready -d $Target -tAc "SELECT 'alembic_version='||version_num FROM alembic_version UNION ALL SELECT 'businesses='||count(*) FROM businesses UNION ALL SELECT 'products='||count(*) FROM products UNION ALL SELECT 'audit_logs='||count(*) FROM audit_logs"
