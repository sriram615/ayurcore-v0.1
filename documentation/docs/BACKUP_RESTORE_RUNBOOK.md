# AyurCore — Backup & Restore Runbook (MVP)

This is the operator-facing procedure for `scripts/backup_db.py`. It assumes you've read (or at least skimmed) the read-only Backup & Restore Audit this implementation is based on. It does not require reading any application code.

Two rules matter more than anything else below:

> **NEVER restore over a running application.** Stop AyurCore first, every time.

> **NEVER use a raw `cp` of `ayurcore.db` as your live backup method.** The database runs in SQLite's WAL mode — a plain file copy taken while the app is running can silently miss committed data sitting in a separate `-wal` file, or catch a page mid-write. Always use `scripts/backup_db.py` (which uses SQLite's own `VACUUM INTO` mechanism), never `cp`, `rsync`, Finder copy/paste, or any other raw file copy of the live database.

---

## 1. Taking a backup

Backups can be taken **while the application is running** — no downtime needed for this step.

```bash
cd /path/to/Ayurveda
python3 scripts/backup_db.py --dest /path/to/backups/ayurcore_$(date +%Y-%m-%d_%H%M%S).db
```

Add `--encrypt` to also produce an encrypted `.enc` copy (see §4 below) — recommended for any backup that will leave this machine.

The script will only report success after:
- the backup file exists
- it opens as a valid SQLite database
- `PRAGMA integrity_check` returns `ok`
- `PRAGMA foreign_key_check` returns no violations

If any of those fail, the script deletes the bad backup file it just made and exits with a non-zero status. **A backup that failed verification was never valid to begin with — don't try to "fix" it, just take a new one.**

The script never prints patient data — only file paths and per-table row *counts*.

## 2. Restore procedure

Follow these steps in order. Do not skip the verification step (2) or the safety-copy step (3), even in an emergency — they're what make a bad restore recoverable instead of a second incident on top of the first.

**Step 1 — Stop AyurCore.**
Stop the `uvicorn`/FastAPI process and the Streamlit UI. Confirm no process still holds the database open.

**Step 2 — Verify the backup you're about to restore, before touching anything live.**
If it's encrypted, decrypt it to a scratch location first:
```bash
export AYURCORE_BACKUP_PASSPHRASE='...'   # the passphrase for THIS backup
python3 -c "
from scripts.backup_db import decrypt_backup
decrypt_backup('/path/to/backups/ayurcore_2026-09-19_120000.db.enc', '/tmp/restore_candidate.db')
"
```
Then verify it, plaintext or not:
```bash
python3 -c "
from scripts.backup_db import verify_backup
r = verify_backup('/tmp/restore_candidate.db')
print('ok:', r.ok, '| reason:', r.reason)
print('row counts:', r.table_row_counts)
"
```
Do not proceed unless `ok: True`.

`verify_backup` checks, in order:
1. the file opens as a SQLite database;
2. `integrity_check` passes;
3. `foreign_key_check` passes;
4. **(2026-09-27)** it's an AyurCore migrated database, and **every table its own recorded schema version requires is present**. An older backup is still accepted; startup migrations upgrade it.
5. **(2026-09-27)** **every clinic's audit hash chain inside the backup verifies**, so a backup of an already-tampered audit trail is rejected.

**Step 3 — Preserve the current (possibly-broken) database as a safety copy.**
Never delete the live file — move it aside:
```bash
mv db/ayurcore.db db/ayurcore.db.pre-restore-safety-copy
```
(If `-wal`/`-shm` files are present next to it, move those aside too, same prefix.)

**Step 4 — Replace the database with the verified backup.**
```bash
cp /tmp/restore_candidate.db db/ayurcore.db
```

**Step 5 — Start the application.**
Start `uvicorn`/FastAPI as normal.

**Step 6 — Let migrations run.**
This happens automatically — every AyurCore startup calls `run_migrations()` before serving a request. If the restored backup is from an older code version than what you're running, you'll see `event=migrations_applied_on_startup versions=[...]` in the log; that's expected and correct. If the restored backup is from a *newer* migration than the code you're running (you're rolling back code, not just data), the app will fail loudly with `MigrationChecksumMismatchError` or a missing-table error rather than silently running against a schema it doesn't understand — if you hit that, you're restoring against the wrong code version, not a backup problem.

**Step 7 — Verify audit integrity.**
```bash
python3 -c "
import sys; sys.path.append('.')
from backend.audit_store import verify_audit_integrity
report = verify_audit_integrity()   # all clinics; pass a clinic_id to scope to one
print('valid:', report.valid, '| checked:', report.checked_count, '| violations:', report.violations)
"
```
Expect `valid: True`.

**Step 8 — Verify normal prescription operation.**
Through the actual running application: log in as a real user, run one full preview → confirm cycle for a **new** prescription, and confirm inventory deducted correctly and a new audit event appended (chained to the last restored event, not a fresh genesis). Only once this succeeds is the restore actually complete — "the file opened" is not the same as "the system works."

## 3. If something goes wrong mid-restore

If Step 4 or later goes wrong before Step 8 succeeds, you still have `db/ayurcore.db.pre-restore-safety-copy` from Step 3 — move it back into place (`mv db/ayurcore.db.pre-restore-safety-copy db/ayurcore.db`) and you're back to exactly where you started, no worse off. This is why Step 3 is never optional, even under time pressure.

## 4. Encryption

Backups contain real patient data (see §5) and must be encrypted before they leave this machine (copied to another drive, uploaded anywhere, etc.).

Encryption uses the system `openssl` binary — **not** a new Python package. AES-256-CBC with PBKDF2 key derivation. Before relying on this, confirm `openssl` is actually present:
```bash
which openssl && openssl version
```
On macOS, the system `openssl` at `/usr/bin/openssl` may be an older LibreSSL build rather than modern OpenSSL, and very old LibreSSL builds can lack `-pbkdf2` support. If `python3 scripts/backup_db.py --dest ... --encrypt` fails with an openssl option error, install a current OpenSSL (e.g. `brew install openssl`) and either put it first on `PATH` or point the script at it — do not fall back to an unencrypted backup as a workaround.

**The passphrase is never a command-line argument, never hard-coded, and never logged.** It's read only from the `AYURCORE_BACKUP_PASSPHRASE` environment variable:
```bash
export AYURCORE_BACKUP_PASSPHRASE='choose a strong passphrase, store it in a password manager'
python3 scripts/backup_db.py --dest /path/to/backups/ayurcore_$(date +%Y-%m-%d_%H%M%S).db --encrypt
```
This produces `<dest>.enc` and removes the plaintext intermediate file automatically (pass `--keep-plaintext` to keep both, which is not recommended for anything that will leave this machine).

**Store the passphrase separately from the backup files themselves** — a password manager, not a text file in the same backup folder. A backup you can't decrypt is as good as no backup; a backup whose passphrase sits next to it is as good as unencrypted.

## 5. What's in a backup (security/privacy)

A backup is a full copy of the production database and must be handled with the same care as the live system — never treated as a lesser artifact. It contains real patient names, phone numbers, and patient IDs (`prescriptions`, `prescription_previews`), password hashes and session-token hashes (not plaintext, but still sensitive), and — per this project's own idempotency design — a replayed copy of patient names inside `idempotency_keys.response_body_json`. Restrict access to backup files and their passphrases to the same people who'd be allowed to access the live database directly.

## 6. Recommended cadence (documented only — nothing below is installed automatically)

- **Frequency:** daily, as an MVP baseline.
- **Retention:** 7 daily + 4 weekly as a starting point — adjust once you have a real sense of how often you'd actually want to reach back.
- **Location:** a volume separate from the one `db/ayurcore.db` lives on — a local disk failure must not take out both the live database and its only backup.
- **Trigger:** automatic for the daily cadence, plus run it manually before anything risky (a code deploy, a migration, an OS upgrade).

### Example macOS `launchd` schedule (documented only — not installed by this task)

This is a reference example for whoever sets up scheduling, not something this implementation phase installs. It is **not** loaded into `launchd` automatically — if you want it running, you install it yourself.

`~/Library/LaunchAgents/com.ayurcore.backup.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.ayurcore.backup</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>bash</string>
    <string>-c</string>
    <string>cd /path/to/Ayurveda &amp;&amp; AYURCORE_BACKUP_PASSPHRASE="$(security find-generic-password -a ayurcore -s ayurcore-backup -w)" python3 scripts/backup_db.py --dest "/path/to/backups/ayurcore_$(date +\%Y-\%m-\%d_\%H\%M\%S).db" --encrypt</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>2</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/path/to/backups/backup.log</string>
  <key>StandardErrorPath</key>
  <string>/path/to/backups/backup.err.log</string>
</dict>
</plist>
```
(The `security find-generic-password` call is one reasonable way to keep the passphrase out of the plist file itself, using macOS's own Keychain rather than a new secrets mechanism — install it with `launchctl load ~/Library/LaunchAgents/com.ayurcore.backup.plist` only if and when you decide to turn this on. Retention/cleanup of old backup files is a separate, equally simple script this runbook deliberately leaves undesigned until it's actually needed.)

## 7. Manual Mac verification commands

See the implementation status report for the exact copy-pasteable command set (taking a backup while the app runs, verifying it, inspecting row counts, an isolated restore rehearsal, migration verification, audit verification, and confirming a corrupted backup is rejected).

## 8. Audit anchors (detecting deleted or rewritten audit events)

**Why an anchor is needed:**
- **Truncation goes unnoticed without one.** The hash chain detects an edited event, but a chain can't tell that its most recent events were deleted: what remains still verifies.
- **The in-database triggers can be removed.** Since migration 014, `audit_events` refuses `UPDATE`/`DELETE`, but anyone who can write the database file can drop those triggers.

`scripts/audit_anchor.py` keeps signed checkpoints **outside** the database.

**Take a checkpoint** (append-only; one signed line per clinic):
```bash
export AYURCORE_AUDIT_ANCHOR_KEY='...'   # at least 32 characters; keep it off the database machine where possible
python3 scripts/audit_anchor.py checkpoint --anchor-file /secure/place/ayurcore_audit_anchors.jsonl
```

**Verify** (read-only; checks every checkpoint ever written):
```bash
python3 scripts/audit_anchor.py verify --anchor-file /secure/place/ayurcore_audit_anchors.jsonl
```
Exit code 0 means every checkpoint still matches. A non-zero exit lists each violation:
- a missing anchored event (tail truncated or deleted);
- a changed anchored hash (chain rewritten);
- a changed event count before the anchor;
- a broken chain;
- a checkpoint line whose signature doesn't verify (anchor file edited, or the wrong key).

**Operating rules:**
- **Location:** keep the anchor file where the database's operator can't silently rewrite it, such as another machine or append-only (WORM) storage. It contains only clinic ids, counts, event ids and hashes; no patient data.
- **Cadence:** events written after the latest checkpoint are protected only by the triggers and the chain. Checkpoint as often as that window matters, for example hourly and after each clinic session, and run `verify` as part of every restore (after Step 7).
- **After a restore:** the restored database legitimately ends at the backup's last event. `verify` will report checkpoints taken *after* that backup as missing events; that is expected. Take a fresh checkpoint once Step 8 succeeds.

