# Data Sync — Keeping Both Devices on the Latest Dataset

**Goal:** every device always has the latest dataset. **Rule:** git = code + manifest,
Google Drive = data. This doc is the process that keeps the two devices' data identical.

## The model (why this is simple)

Google Drive is the **hub**. You do **not** sync device-to-device directly. Each device
syncs its local repo data folders ↔ the Drive store, and Google Drive replicates the
store to every device on the account.

```
   Personal PC  ──sync──►  Google Drive store  ──sync──►  Lab machine
        ▲                    (The_Works\...\Day_Trading_Bot)          ▲
        └───────────────── Drive propagates automatically ───────────┘
```

The only manual piece per device is `scripts/sync_data_drive.ps1`, which reconciles the
repo's local data folders with the Drive store.

## Safety guarantees (built into the script)

- **Never deletes.** Uses `rclone copy --update` (newer-wins). Deletions are *not*
  propagated — a sync can never destroy market data, including the perishable option tape.
- **Secrets never move.** `creds.txt`, `config.toml`, `.env`, `*.pem/*.key/*.crt/*.p12/*.pfx`,
  `*_credentials.json`, `service_account*.json` are excluded in both directions.
- **Every run is logged** to `docs/sync_logs/sync_<timestamp>.log`.
- **`--checksum`** verification (Drive MD5) decides equality, not just timestamps.

## One-time setup per device

1. Install [rclone](https://rclone.org) and configure a Google Drive remote
   (`rclone config`) — OR ensure Google Drive Desktop is mounted (e.g. `G:\My Drive`).
2. Open `scripts/sync_data_drive.ps1` and set `-Remote` (or edit the default) to either:
   - your rclone remote path, e.g. `gdrive:The_Works/Projects/Day_Trading_Bot`, or
   - your Drive mount path, e.g. `G:\My Drive\The_Works\Projects\Day_Trading_Bot`.
3. Confirm `-RepoRoot` points at the clone on that device.

## Daily discipline (avoids conflicts)

Because the sync is newer-wins and additive, conflicts are rare — but keep this order:

1. **Before working** on a device: `... -Direction pull` (get the other device's latest).
2. **After producing data** (a pull, a backtest, new features): `... -Direction push`.
3. **Let Drive finish** ("sync complete" in the tray, or rclone returns) **before** running
   the sync on the *other* device.
4. Never edit the same file on both devices at once between syncs — newer-wins would keep
   only one copy.

## Commands

```powershell
# ALWAYS dry-run first to see what would move:
powershell -File scripts\sync_data_drive.ps1 -Direction both -DryRun

# Pull latest down / push new up / full reconcile:
powershell -File scripts\sync_data_drive.ps1 -Direction pull
powershell -File scripts\sync_data_drive.ps1 -Direction push
powershell -File scripts\sync_data_drive.ps1 -Direction both
```

## Folder map (repo ↔ Drive)

Current Drive layout is flattened under `_local_archive\` (see `docs/DATA_MANIFEST.md`).
The script carries this exact map:

| Repo path | Drive subfolder |
|-----------|-----------------|
| `data_store/` | `_local_archive/data_store` |
| `data_raw/` | `_local_archive/data_raw` |
| `vendor/swe/data/` | `_local_archive/vendor_swe_data` |
| `vendor/swe/data_processed/` | `_local_archive/vendor_swe_data_processed` |
| `vendor/swe/data_raw/` | `_local_archive/vendor_swe_data_raw` |

## Two open items to resolve for a clean go-forward sync

1. **Intraday set location.** The 635-file / 756 MiB intraday store (option chain/tape,
   `bars_1m`, ticks, events, `spread_stats`) sits at the **primary store root**, not under
   `_local_archive`. Confirm its exact Drive subpath and add it to the map in the script,
   or it won't be kept in sync.
2. **Consider regularising the Drive layout** to mirror repo paths (drop the flattened
   `_local_archive` names). Not required — the map handles it — but a mirrored store makes
   the sync trivially auditable and lets a fresh clone restore by copy-in-place.

_Last updated: 2026-07-22._
