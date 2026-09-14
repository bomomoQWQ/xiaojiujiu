# Recovery and Backup

This workspace is designed to survive an unexpected Windows restart or blue screen.

## What is protected

- Source code and architecture documents are versioned by the workspace Git repository.
- `AstrBot/` is an independent upstream checkout and must remain unmodified.
- `scripts/backup.ps1` creates an atomic snapshot on `E:\companion_runtime_backup` and retains the five newest snapshots.
- Generated datasets, checkpoints, adapters, evaluation reports, and SQLite backups must be copied separately because they are intentionally excluded from Git.
- DeepSeek credentials are environment-only and must never appear in source, logs, checkpoints, or backups.

## Before long-running work

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\backup.ps1
```

Commit source checkpoints after tests pass:

```powershell
git add runtime training astrbot_plugin_companion_runtime scripts
# Inspect staged files before committing; secrets must never be staged.
git diff --cached --check
git commit -m "checkpoint: describe completed milestone"
git bundle create E:\companion_runtime_backup\workspace-latest.bundle --all
```

## Runtime database

Never copy a live SQLite database file directly. Use the Runtime backup command/API, which invokes SQLite's online backup API after a WAL checkpoint. Backups should be written first to a temporary name and atomically renamed.

## Training artifacts

- Data generation writes JSONL incrementally and records a checkpoint after every accepted batch.
- Trainer checkpoints should use `save_total_limit=2` and resume from the newest complete `checkpoint-*` directory.
- Write large outputs to a temporary directory and add a completion manifest only after hashes have been computed.
- Keep the model cache on `E:` or WSL ext4; it can be downloaded again and is lower priority than generated data and adapters.

## Restore source

```powershell
mkdir restored
cd restored
git clone E:\companion_runtime_backup\workspace-latest.bundle .
```

Alternatively copy the newest `snapshot-*` directory back into an empty workspace and verify `SHA256.json`.
