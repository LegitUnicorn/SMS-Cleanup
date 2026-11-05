# SMS-Cleanup

Reclaim the hundreds of GB wasted by a phone backup app (SMS/MMS/call log →
`sms-YYYYMMDDHHMMSS.xml` / `calls-YYYYMMDDHHMMSS.xml` in Google Drive) that
uploaded a **full** backup every time instead of an incremental one —
**without losing a single message**, including messages that were later
deleted from the phone and now only exist in an old backup.

## TL;DR for each affected person

1. Open [`notebooks/SMS_Cleanup_Colab.ipynb`](notebooks/SMS_Cleanup_Colab.ipynb)
   in Google Colab (*File → Open notebook → GitHub*), signed in to **the Google
   account that owns the backups**.
2. Set `BACKUP_DIR` to the backup folder — a path, or just paste the folder's
   drive.google.com link — and run the cells. Backups in other places (an
   older phone's folder, copies, a folder of `.zip` backups) go in
   `OTHER_FOLDERS`; optional Step 3b finds every folder in My Drive that holds
   backups. Step 3 lists every backup that will be merged before anything runs. If the folder isn't where you said,
   the notebook searches your Drive and lists the folders that hold backups.
   It stops if the signed-in account doesn't own the folder: only the owner can
   free that storage, so pick the owner's account in **both** Google sign-in popups
   (Drive mount and the link lookup).
3. **Merge** writes every message once into a single file in `<folder>-merged/`
   (`sms-…-merged.xml`, plus `calls-…-merged.xml` if you have call logs).
4. **Verify** re-reads every original backup and proves each message is in the
   merged file. Only if it says **VERIFIED** are the originals moved into
   `<folder>-to-delete/`.
5. Delete `<folder>-to-delete/` and **empty the Drive trash**. Keep
   `<folder>-merged/`.

End result: **one file** with every text, MMS and call from every backup.

Nothing is downloaded to anyone's computer; Colab runs next to Drive in
Google's cloud.

## Why not share everyone's folders with one account and fix from there?

It doesn't work the way you'd hope in Google Drive:

* **Storage quota follows the file owner, not the folder.** Files a teammate
  shares with you still count against *their* quota.
* **An editor can't free the owner's space.** In a personal "My Drive", a
  non-owner who deletes a file only removes it from the folder; the file
  becomes an orphan that still counts against the owner's quota (and is now
  hard to find). Only the owner can trash it and empty their trash.
* Transferring ownership (where allowed) is per-file, needs acceptance, and
  would move hundreds of GB of other people's private texts into one account
  that then needs the quota for it.
* It concentrates very personal data (everyone's texts and MMS photos) in one
  place for no technical benefit.

So the shared piece is **this repo (the code)**; each person runs it inside
their own account. That is also the only way to actually free *their* storage.

(If everyone is on the same Google Workspace and an admin wants to do it
centrally, the files would first have to be moved into a Shared Drive — a
bigger, slower operation than just running the notebook three times.)

## How it works: merge (the notebook's approach)

```
sms-…0101.xml  sms-…0201.xml  …  sms-…0414.xml      (44 full backups, 125 GB)
        \            |                 /
         └──── merge: each message written once ────▶  sms-…0414-merged.xml  (~3 GB)
                (newest copy wins)
         ┌──── verify-merged: re-read all 44, check every record ─┘
         ▼
   VERIFIED → originals moved to <folder>-to-delete/  → you delete + empty trash
```

* **merge** reads every `sms-*.xml` / `calls-*.xml` under the inputs (folders
  are searched recursively), including backups stored inside `.zip` files
  (read in place, nothing is extracted; password-protected or unreadable zips
  are reported and left alone), newest first, and writes each message once, using
  the copy from the newest backup (latest read status / contact name). Messages
  that were deleted from the phone and only survive in old backups are
  included. Output is the backup app's own XML format, so it restores like any
  backup. When done it re-reads the new file and checks every record came out
  byte-identical. Originals are not touched.
* **verify-merged** is the independent check: it re-reads every original and
  classifies each record as an exact copy in the merged file, the same message
  with some details differing (listed per attribute), or **missing** (saved to
  `verify-missing-*.xml`; nothing is moved). With `--move-sources-to` it moves
  the originals into one folder only after VERIFIED. If a source file can't be
  read to the end (Drive I/O errors) but nothing is missing, that file stays
  put and only the fully verified files are moved.

## Alternative: keep the original files in place (analyze / apply)

Instead of writing a new file, this keeps the newest full backup (and any real
incremental backups) untouched and removes the rest:

```
newest ─────────────────────────────────────────────────▶ oldest
sms-…0302 (incremental)  sms-…0301 (full)  sms-…0201 (full)  sms-…0101 (full)
   100% new → KEEP         ~100% new → KEEP   0% new → REMOVE   0.01% new → REMOVE
                                                                 └─ those few messages
                                                                    → recovered/sms-recovered.xml
```

1. **analyze** (read-only on the backup folder): stream every file newest →
   oldest, compute a stable fingerprint for every SMS / MMS / call, and keep a
   running set of what's been seen.
   * Keep a file if ≥ 50 % of its records are not in any newer file
     (`--keep-ratio`). This keeps the newest full backup *and* any real
     incremental backups automatically, whichever mix you have.
   * Otherwise mark it superseded and copy its few unique records (messages
     deleted from the phone later) into a small recovered fragment.
   * Progress is committed per file to `state.json`, so it survives Colab
     disconnects and can be run in several sessions (`--max-hours`).
2. **apply**:
   * writes `recovered/sms-recovered.xml` / `calls-recovered.xml` in the
     backup app's own XML dialect (restorable with the app);
   * **re-parses that file from disk** and proves that every record of every
     superseded file is in a kept file or in the recovered file — aborts
     otherwise;
   * checks kept files are unchanged, then **moves** superseded files to
     `<backup_dir>-cleanup/superseded/` (a rename on Drive — no re-upload).
     `--delete` sends them straight to the Drive trash instead.
3. **verify** — an independent second opinion that ignores everything
   `analyze` decided. It re-reads from disk everything that *remains* (backup
   folder + `recovered/`) and everything that was *moved* (`superseded/`), and
   classifies every moved record as an exact copy that remains, the same
   message with some details differing (listed by attribute, e.g. `read`,
   `contact_name`), or **missing**. Missing records are written to
   `verify-missing-*.xml` and the run ends NOT VERIFIED.
4. **You** delete the `superseded` folder and empty the trash — only after
   `verify` says VERIFIED.

### What counts as "the same message"

Fingerprints ignore fields that legitimately change between backups
(`read`, `contact_name`, `readable_date`, `sub_id`, on-phone file paths) and
normalise phone numbers (`+1 (555) 123-4567` == `5551234567`). Content is part
of the identity: if two copies of an MMS differ (e.g. one has the attachment
and one doesn't), *both* are kept. The tool errs on the side of keeping data.

### Practical details

* **Huge files**: everything is streamed (bounded memory); a 30 GB file is fine.
  Parsing runs at ~70 MB/s; on Colab Drive read speed is usually the limit,
  so expect a few hours for a few hundred GB — just re-run the cell if it
  stops.
* **Emoji**: the backup app writes emoji as UTF-16 surrogate pairs
  (`&#55357;&#56832;`), which standard XML parsers reject. The reader repairs
  that on the fly; the recovered file is written back in the app's dialect.
* **Truncated / corrupt uploads** are reported and left alone (never removed).
  Drive `Input/output error`s mid-read are retried automatically (3 tries); if a
  file still fails it is kept, and `analyze --retry-errors` re-reads just those
  files later.
* **Space is only freed after emptying the Drive trash.**
* Make sure the app is now set to incremental / "only new messages" backups
  (or a sensible "keep last N backups" retention), otherwise the problem comes
  back. Keep the merged / `recovered` files *outside* the app's backup folder so
  its retention setting can never delete them.

## Running it elsewhere

Python ≥ 3.8, standard library only.

```bash
pip install git+https://github.com/LegitUnicorn/SMS-Cleanup.git
smscleanup merge         /path/to/SMSBackupRestore /path/to/old-phone /path/to/zips --out-dir /path/to/merged
smscleanup verify-merged /path/to/SMSBackupRestore /path/to/old-phone /path/to/zips --out-dir /path/to/merged \
                         --move-sources-to /path/to/to-delete   # moves only if VERIFIED

# alternative flow (keep originals in place)
smscleanup analyze /path/to/SMSBackupRestore          # read-only, resumable
smscleanup report  /path/to/SMSBackupRestore
smscleanup apply   /path/to/SMSBackupRestore          # asks for confirmation; moves only
smscleanup verify  /path/to/SMSBackupRestore          # independent re-read; exit 0 = VERIFIED
smscleanup inspect /path/to/SMSBackupRestore/sms-20250831004537.xml
```

On a Linux/macOS machine without Colab, mount Drive with
[rclone](https://rclone.org/drive/) so files are streamed, not synced:

```bash
rclone mount gdrive: ~/gdrive --vfs-cache-mode minimal &
smscleanup analyze ~/gdrive/SMSBackupRestore
```

(Avoid "Google Drive for desktop" in mirror mode — it would download
everything first.)

## Development

```bash
pip install pytest && python -m pytest -q
```

Never commit real backups: `*.xml` is git-ignored.
