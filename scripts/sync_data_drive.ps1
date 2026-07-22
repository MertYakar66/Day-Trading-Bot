<#
  sync_data_drive.ps1 — keep THIS device's repo data in sync with the Google Drive store.

  Model: Google Drive is the hub. Run this on EACH device; Drive propagates changes
  between devices automatically. The only manual piece is syncing the repo's local
  data folders <-> the Drive store, which is what this script does.

  Safety: uses `rclone copy --update` (newer-wins) — it NEVER deletes. A sync must
  not be able to destroy market data. Deletions are not propagated by design.

  Requires: rclone (https://rclone.org) configured with a Google Drive remote,
  OR a Google Drive Desktop mount path (e.g. 'G:\My Drive\...') for -Remote.

  Usage:
    # dry-run first, always:
    powershell -File scripts\sync_data_drive.ps1 -Direction both -DryRun
    # pull latest down before a work session:
    powershell -File scripts\sync_data_drive.ps1 -Direction pull
    # push new local data up after producing:
    powershell -File scripts\sync_data_drive.ps1 -Direction push
    # full two-way reconcile:
    powershell -File scripts\sync_data_drive.ps1 -Direction both
#>
param(
  [ValidateSet('pull','push','both')] [string]$Direction = 'both',
  [string]$RepoRoot = 'C:\Users\merty\Desktop\Day-Trading-Bot',
  # rclone remote:path  (e.g. 'gdrive:The_Works/Projects/Day_Trading_Bot')
  # OR a local Drive Desktop mount path (e.g. 'G:\My Drive\The_Works\Projects\Day_Trading_Bot').
  # rclone handles both the same way.
  [string]$Remote = '<SET-ME>:The_Works/Projects/Day_Trading_Bot',
  [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

if ($Remote -like '*<SET-ME>*') {
  Write-Error "Set -Remote to your rclone remote:path or Drive mount path before running."
  exit 1
}

# repo-relative folder  ->  Drive subfolder (current flattened '_local_archive' layout).
# If you later regularise the Drive store to mirror repo paths, update the Drive column.
$Map = @(
  @{ Repo = 'data_store';                Drive = '_local_archive/data_store' },
  @{ Repo = 'data_raw';                  Drive = '_local_archive/data_raw' },
  @{ Repo = 'vendor/swe/data';           Drive = '_local_archive/vendor_swe_data' },
  @{ Repo = 'vendor/swe/data_processed'; Drive = '_local_archive/vendor_swe_data_processed' },
  @{ Repo = 'vendor/swe/data_raw';       Drive = '_local_archive/vendor_swe_data_raw' }
  # TODO: the 635-file / 756 MiB intraday set lives at the primary store root, not under
  # _local_archive. Confirm its exact Drive subpath and add it here, e.g.:
  # @{ Repo = 'data_store'; Drive = '<confirmed-intraday-subpath>' }
)

# Secrets / junk that must never move in EITHER direction.
$Excludes = @(
  'creds.txt','config.toml','.env','.envrc',
  '*.pem','*.key','*.crt','*.p12','*.pfx',
  '*_credentials.json','service_account*.json',
  '__pycache__/**','.venv/**','venv/**','node_modules/**','.git/**','*.pyc'
)
$excludeArgs = @()
foreach ($e in $Excludes) { $excludeArgs += @('--exclude', $e) }

$stamp   = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$logDir  = Join-Path $RepoRoot 'docs\sync_logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log     = Join-Path $logDir "sync_$stamp.log"

function Sync-One($src, $dst, $label) {
  $a = @('copy', $src, $dst, '--update', '--checksum', '--create-empty-src-dirs', '--progress', '--stats-one-line') + $excludeArgs
  if ($DryRun) { $a += '--dry-run' }
  Write-Host ""
  Write-Host "[$label]  $src  ->  $dst"
  "== [$label] $src -> $dst ==" | Out-File -FilePath $log -Append -Encoding utf8
  & rclone @a 2>&1 | Tee-Object -FilePath $log -Append
}

Write-Host "Sync direction=$Direction  dryrun=$DryRun  remote=$Remote"
foreach ($m in $Map) {
  $local  = Join-Path $RepoRoot ($m.Repo -replace '/', '\')
  $remote = "$Remote/$($m.Drive)"
  # PULL first (bring newer remote data down), then PUSH (send newer local data up).
  if ($Direction -eq 'pull' -or $Direction -eq 'both') { Sync-One $remote $local  "PULL $($m.Repo)" }
  if ($Direction -eq 'push' -or $Direction -eq 'both') { Sync-One $local  $remote "PUSH $($m.Repo)" }
}

Write-Host ""
Write-Host "Done. Log: $log"
if (-not $DryRun) {
  Write-Host "If using Drive Desktop, let the tray finish 'sync complete' before running on the other device."
}
