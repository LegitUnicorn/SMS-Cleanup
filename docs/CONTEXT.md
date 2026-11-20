# Context prompt for a new chat

Paste everything below the line into a new chat to bring it up to speed.
Update "Current state of my Drive" as things change.

---

## My problem
My phone's SMS/MMS/call-log backup app uploads XML backups to Google Drive
(files named sms-YYYYMMDDHHMMSS.xml and calls-YYYYMMDDHHMMSS.xml, sometimes
zipped as sms-YYYYMMDDHHMMSS.xml.zip). For a long time it saved FULL backups
over and over, which filled hundreds of GB of Drive. Three Google accounts are
affected (mine and two teammates'). The goal is to end up with ONE verified
backup per account, without losing a single message, and then delete the
redundant copies to free the space.

## The tool we built (public repo: github.com/LegitUnicorn/SMS-Cleanup)
Python 3, standard library only. Key commands:
- `smscleanup merge <inputs...> --out-dir DIR`: reads every sms-*/calls-*.xml
  under the inputs (folders searched with subfolders; `folder\*` = only the
  files directly in that folder; .zip backups read in place, never extracted)
  and writes each message once to DIR/sms-<newest ts>-merged.xml and
  DIR/calls-<newest ts>-merged.xml, in the app's own restorable format.
  Duplicates and incrementals are fine: the newest copy of each message wins,
  and messages only found in old backups are kept. Earlier *-merged.xml files
  in DIR are set aside in DIR-previous and merged in, never overwritten.
  Output is re-read and self-checked.
- `smscleanup verify-merged <same inputs> --out-dir DIR [--move-sources-to D]`:
  an independent check. It re-reads every source and classifies each record as
  exact / same message with details differing (read flag, contact name, etc.) /
  MISSING. Only on VERIFIED does it move the originals into D. Without
  --move-sources-to it is read-only.
- `smscleanup wizard` (Windows: double-click windows/SMS-Cleanup.bat): guided
  run. It asks for the main folder and other folders, lists everything it will
  read, then merges, verifies, and moves verified originals to
  <main>-to-delete. It refuses My Drive itself (use `X:\My Drive\*` for loose
  files).
- There's also a Colab notebook (notebooks/SMS_Cleanup_Colab.ipynb, Steps 1-7)
  and a teammate guide: https://claude.ai/artifact/4Y8jupWwtsbEA42ukvKK2o
  (private until I share it).

## My environment now
Windows PC with Google Drive for desktop showing my Drive as X:
(My Drive = `X:\My Drive`), in "stream files" mode. I'm switching from Colab
to running the wizard locally. The Windows .bat/wizard hasn't been tested on
Windows yet.

## Current state of my Drive
- `X:\My Drive\SMS-RecentMerge` holds two earlier merged sets:
  - sms-20250414013013-merged.xml (3.82 GB) + calls-20220104000949-merged.xml:
    the first cleanup (backups up to 2025-04-13, plus 2021-22 backups):
    33,391 SMS, 65,066 MMS, 1,127 calls.
  - sms-20260221013057-merged.xml (7.24 GB) + calls-20260221013057-merged.xml:
    merged from 302 zipped backups (Sep 2025 to Feb 2026): 13,825 SMS,
    82,086 MMS, 2,722 calls.
- `X:\My Drive\SMSProBackup-to-delete`: those 302 zips (35.4 GB). Already
  VERIFIED as fully contained in the SMS-RecentMerge files, so it's safe to
  delete (then empty the Drive trash).
- Loose sms-*.xml / calls-*.xml files in the root of My Drive (e.g.
  sms-20250824005235.xml, calls-20221023000904.xml): probably duplicates or
  small incremental backups.
- The phone app currently makes a full backup on the 1st of each month plus
  small daily incrementals.

## What I want to do next
1. Delete SMSProBackup-to-delete and empty the Drive trash (if not done yet).
2. Run the wizard with main folder `X:\My Drive\SMS-RecentMerge` and other
   folder `X:\My Drive\*`, check the list, merge, verify. Result: one backup in
   `X:\My Drive\SMS-RecentMerge-merged`.
3. Wait until Google Drive for desktop has finished uploading the merged files,
   THEN delete SMS-RecentMerge-to-delete and empty the trash. Keep a second copy
   of the -merged folder somewhere else.
4. Help my two teammates do the same in their own accounts.
5. Maybe tune the phone app's retention so this doesn't build up again.

## Rules
- Never delete anything until a verify says VERIFIED, and the merged file has
  finished uploading.
- The merged files may be the ONLY copy of some messages. Never overwrite or
  delete them.
- Don't point the tool at My Drive itself (it would sweep the whole Drive). Use
  `X:\My Drive\*` for loose files only.
