# Data Manifest — Google Drive ↔ Repo Mapping

**Architecture:** git holds **code + this manifest**; Google Drive holds the **data**.
This file is the authoritative record of what data exists, where it lives on Drive,
and which repo path it maps back to — so a fresh clone can be re-attached to its data
without guesswork.

- **Drive root (primary store):** `G:\My Drive\The_Works\Projects\Day_Trading_Bot\`
- **Source machine / repo:** `C:\Users\merty\Desktop\Day-Trading-Bot`
- **Last migration + verification:** 2026-07-21 (MD5 / checksum, byte-for-byte)
- **Local deletion status:** none — all originals still on the source machine.

> Note on layout: the Drive store does **not** mirror repo-relative paths. Additions are
> consolidated under `_local_archive\` with flattened folder names (e.g. repo
> `vendor/swe/data_processed/` → Drive `_local_archive\vendor_swe_data_processed\`).
> Use the table below as the re-attach map; do not assume a 1:1 path mirror.

---

## Datasets on Drive

| # | Dataset | Repo path (source) | Drive location (under primary store root) | Files | Size | Verified | Migrated |
|---|---------|--------------------|--------------------------------------------|-------|------|----------|----------|
| 1 | Intraday market data — option chain/tape, `bars_1m`, ticks, events, `spread_stats` (**perishable**) | `data_store/` (intraday shards) | primary store root — *confirm exact subpath against Drive tree* | 635 | 756 MiB | ✅ checksum (rclone) | earlier, 2026-07-21 |
| 2 | Day-bot store additions (signal logs, ledger, small features) | `data_store/` | `_local_archive\data_store\` | 49 | 174,577 B | ✅ MD5 | 2026-07-21 (this run) |
| 3 | Raw vendor pulls (IBKR / Theta) | `data_raw/` | `_local_archive\data_raw\` | 3,306 | *(unrecorded — confirm)* | ✅ (earlier) | earlier |
| 4 | SWE Bloomberg workbooks + reference data | `vendor/swe/data/` | `_local_archive\vendor_swe_data\` | 73 | 257 MB | ✅ (earlier) | earlier |
| 5 | SWE processed (IV surface, vol indices, options flow) | `vendor/swe/data_processed/` | `_local_archive\vendor_swe_data_processed\` | 2 | 166,352 B | ✅ MD5 | 2026-07-21 (this run) |
| 6 | SWE raw pulls | `vendor/swe/data_raw/` | `_local_archive\vendor_swe_data_raw\` | 12 | 2,373,076 B | ✅ MD5 | 2026-07-21 (this run) |

**This-run additions (rows 2, 5, 6):** 63 files / ~2.59 MiB, all MD5-matched, robocopy 0 FAILED.
Per-run detail: `_local_archive\_MANIFEST_2026-07-21_additions.txt`.

---

## NOT in this store (by design — separate backup concern)

- **Standalone smart-wheel-engine** (~400 MB Bloomberg CSVs, plus `swe-exec1` / `swe-exec3`
  and nested clones). This includes the `broad_pull` regime/vol series the engine reads
  (VIX-family, credit OAS, macro rates, real yields). It is a **separate project with its
  own backup** — "Day-Trading-Bot data is on Drive" does **not** cover the SWE Bloomberg larder.
- **Personal `Downloads/` and `Documents/`** — 240+ unrelated files (coursework, sales data,
  etc.). Intentionally never swept.

## Not present on the source machine (no data to migrate)

`data_processed/` (repo root), `data/`, `tradingview/`, `studies/premium_correction/output/` —
these directories do not exist in this repo checkout.

---

## Security — excluded from Drive (verified clean)

Pre-copy scan came back clean; exclusions also applied to robocopy/rsync as belt-and-suspenders.
**Never copied:** `creds.txt`, `config.toml`, `.env`, `.envrc`, `*.pem`, `*.key`, `*.crt`,
`*.p12`, `*.pfx`, `*_credentials.json`, `service_account*.json`. No `Theta/` install exists in
the repo (the ThetaTerminal app + its `creds.txt` are not present here).

---

## Open item — not closed by migration

The migration **preserved existing data**. The audit's P0 fresh pull — new expired-option
intraday + ticks for the **2026-07-03 → 2026-07-20** gap — is a separate Python pull racing
the ~45-day perishability wall. Migration complete ≠ that pull done.

---

## Re-attach procedure (fresh clone)

1. Clone the repo (code only; data folders are gitignored and empty).
2. From Drive `_local_archive\<flattened-name>\`, copy each dataset back to its **Repo path**
   column above (e.g. `_local_archive\vendor_swe_data_processed\` → `vendor/swe/data_processed/`).
3. Row 1 (intraday) restores from the primary store root — confirm its exact subpath first.
4. Re-run any checksum/manifest verification before trusting the restored store.

_Last updated: 2026-07-21._
