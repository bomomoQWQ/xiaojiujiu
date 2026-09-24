# Recovery and Backup

This workspace is designed to survive an unexpected Windows restart or blue screen.

## What is protected

- Source code and architecture documents are versioned by the workspace Git repository.
- `AstrBot/` is an independent upstream checkout and must remain unmodified.
- `scripts/backup.ps1` creates an atomic snapshot on `E:\companion_runtime_backup` and retains the five newest snapshots. Each snapshot copies `runtime/`, `astrbot_plugin_companion_runtime/`, `scripts/`, `archive/`, the top-level documents and `.gitignore`, writes a `SHA256.json` manifest, and bundles the whole Git history as `workspace.bundle` (verify with `git bundle verify <snapshot>\workspace.bundle`).
- **`scripts/backup.ps1` must keep its UTF-8 BOM.** Windows PowerShell 5.1 (`powershell.exe`, which the file is documented to be run with) decodes a BOM-less script as the ANSI code page; the default `$Source` is a non-ASCII path, so the mangled literal makes every `Test-Path` fail and the script silently produces an **empty snapshot** while still printing a snapshot path. If you edit this file, re-save it with a BOM and check that the new `snapshot-*` directory is not empty.
- The abandoned local-model material (`archive/local_model_training/`, `archive/local_model/`) is archival only: it is not imported, packaged or tested. Its datasets and checkpoints are excluded from Git and are **not** worth copying to a new machine — `archive/README.md` records why the route was dropped. SQLite backups must still be copied separately.
- DeepSeek credentials are environment-only and must never appear in source, logs, checkpoints, or backups.

## Before long-running work

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\backup.ps1
```

Commit source checkpoints after tests pass:

```powershell
git add runtime astrbot_plugin_companion_runtime scripts archive
# Inspect staged files before committing; secrets must never be staged.
git diff --cached --check
git commit -m "checkpoint: describe completed milestone"
git bundle create E:\companion_runtime_backup\workspace-latest.bundle --all
```

## Runtime database

Never copy a live SQLite database file directly. Use the Runtime backup command/API, which invokes SQLite's online backup API after a WAL checkpoint. Backups should be written first to a temporary name and atomically renamed.

## Archived local-model artifacts

The local generative model route was abandoned; `training/` no longer exists at the repository root. What survives lives under `archive/` and is **not** part of any build, test or runtime path:

- `archive/local_model_training/` — the former `training/` tree (configs, data, schemas, src, tests, requirements). Data generation writes JSONL incrementally and records a checkpoint after every accepted batch; trainer checkpoints used `save_total_limit=2` and resumed from the newest complete `checkpoint-*` directory. Keep the schemas and evaluation metrics as a provider-independent correctness baseline (see `archive/README.md`); the *training* half is dead.
- `archive/local_model/` — the removed Runtime module (`local_llm.py`, `test_local_llm.py`) and the local-model experiment scripts (`archive/local_model/scripts/`), including the GGUF / grammar / CPU-contention probes that produced the measurements cited in `archive/README.md`.
- If archival material is ever regenerated, write large outputs to a temporary directory and add a completion manifest only after hashes have been computed, and keep any model cache on `E:` or WSL ext4 — it can be downloaded again and is lower priority than generated data.

Restoring the removed Runtime code (if that decision is ever revisited) means restoring from Git history, not copying from `archive/`: `archive/local_model/local_llm.py` is a frozen copy, not a live module.

## Retained model weights (deliberately not deleted)

The route was abandoned, but the downloaded and trained weights were kept on purpose so the decision remains reversible. They are **not** referenced by any code and are safe to delete whenever the disk is needed:

| Location | Size | Contents |
| --- | ---: | --- |
| `E:\models\Qwen3.5-2B` | 4.25 GB | official base weights, with a `SHA256.json` manifest |
| `E:\llama.cpp` | 0.23 GB | llama.cpp checkout plus a CPU-only build (`llama-server`, `llama-quantize`, `llama-bench`) |
| WSL `/root/models` | 4.3 GB | the same base weights, copied to ext4 for faster training reads |
| WSL `/root/companion-training` | 22 GB | Python 3.12 venv (~5.7 GB) and training outputs (~16 GB: merged model, f16/Q4_K_M/Q3_K_M/Q2_K GGUF, LoRA adapter, evaluation reports) |
| `E:\companion_runtime_backup\generated`, `...\training_artifacts` | < 1 GB | the DeepSeek-generated, validated dataset plus smoke-test artifacts. **Keep this** — it cost real API spend and cannot be regenerated for free |

None of these paths is on any build, test or runtime path. The reserve copy on `E:\companion_runtime_backup` holds only source, so a machine rebuild does not need to carry the weights over.

## Restore source

```powershell
mkdir restored
cd restored
git clone E:\companion_runtime_backup\workspace-latest.bundle .
```

Alternatively copy the newest `snapshot-*` directory back into an empty workspace and verify `SHA256.json`.
