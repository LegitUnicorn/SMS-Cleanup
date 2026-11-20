"""Guided run on your own computer, e.g. Windows with Google Drive for desktop (X:).

    python -m smscleanup wizard        (Windows: py -m smscleanup wizard)

Asks for the folders, shows every backup it will read, then merges, verifies
and gathers the verified originals into one folder to delete -- the same
steps as the Colab notebook, with the same safety checks.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Callable, List, Optional

from .cleanup import human
from .merge import earlier_merged, merge, previous_dir, verify_merged
from .sources import collect_inputs, describe_sources, find_backup_folders, source_size, top_level_dir

DRIVE_ROOT_NAMES = {"my drive", "mydrive"}
LINE = "-" * 72

Ask = Callable[[str], str]


def clean_path(p: str) -> str:
    """Accept what people paste: surrounding quotes (Windows 'Copy as path'), spaces."""
    p = p.strip().strip('"').strip("'").strip()
    top = top_level_dir(p)
    if top is not None:
        return clean_path(top) + os.sep + "*"
    if len(p) > 3:
        p = p.rstrip("/\\")
    return p


def _is_drive_root(p: str) -> bool:
    a = os.path.abspath(p)
    return os.path.dirname(a) == a or os.path.basename(a).lower() in DRIVE_ROOT_NAMES


def check_main(p: str) -> Optional[str]:
    if not p:
        return "Please type a folder."
    if top_level_dir(p) is not None:
        return "The main folder can't end in *. Give the folder itself."
    if not os.path.isdir(p):
        return "That folder doesn't exist: %s" % p
    if _is_drive_root(p):
        return (
            "The main folder can't be the whole drive or My Drive itself. Pick the backup folder "
            "(e.g. X:\\My Drive\\SMS). For backup files loose in My Drive, add  X:\\My Drive\\*  "
            "as an OTHER folder in the next step."
        )
    return None


def check_other(p: str) -> Optional[str]:
    top = top_level_dir(p)
    folder = top if top is not None else p
    if not os.path.exists(folder):
        return "Not found: %s" % folder
    if top is None and os.path.isdir(p) and _is_drive_root(p):
        return (
            "That would search your WHOLE drive, every folder in it. For only the backup files "
            "lying directly in it, add  %s  instead." % (p.rstrip("/\\") + os.sep + "*")
        )
    return None


def folder_report(root: str, inputs: List[str], out_dir: str) -> List[str]:
    """Every folder under ``root`` with backups, marked included / NOT included."""
    whole = [os.path.realpath(p) for p in inputs if top_level_dir(p) is None]
    loose = [os.path.realpath(top_level_dir(p)) for p in inputs if top_level_dir(p)]
    out_real = os.path.realpath(out_dir)
    lines = []
    for d, c in sorted(find_backup_folders(root, skip_suffixes=()).items()):
        real = os.path.realpath(d)
        what = ", ".join("%d .%s" % (n, k) for k, n in sorted(c.items()))
        if real in (out_real, os.path.realpath(previous_dir(out_dir))):
            label = "included    "
        elif real.endswith("-to-delete"):
            label = "to delete   "
        elif real in loose or any(real == p or real.startswith(p + os.sep) for p in whole):
            label = "included    "
        else:
            label = "NOT included"
        note = ""
        if label == "NOT included" and real.endswith(("-merged", "-merged-previous")):
            note = "  <- an earlier MERGED result: add it to end up with one file"
        if real == os.path.realpath(root) and label == "NOT included":
            note = "  <- loose files: add  %s" % (d.rstrip("/\\") + os.sep + "*")
        lines.append("  %s  %s  (%s)%s" % (label, d, what, note))
    return lines or ["  (no folders with sms-/calls- backups found under %s)" % root]


def _yes(ask: Ask, question: str) -> bool:
    return ask(question + " [y/N] ").strip().lower() in ("y", "yes")


def wizard(ask: Optional[Ask] = None, tmp_dir: Optional[str] = None) -> int:
    if ask is None:
        ask = lambda prompt: input(prompt)  # noqa: E731  (looked up at call time)
    print(LINE)
    print("SMS backup cleanup -- guided run")
    print("Every message from every backup goes into ONE merged file per type. Nothing is")
    print("deleted: verified originals are only MOVED into a '-to-delete' folder for you.")
    print(LINE)

    # 1. main folder
    while True:
        main = clean_path(ask("\n1) Main backup folder (where the backup app saves), e.g.  X:\\My Drive\\SMS\n   > "))
        err = check_main(main)
        if not err:
            break
        print("   !! " + err)
    out_dir = main + "-merged"
    trash = main + "-to-delete"
    old_work = main + "-cleanup"
    drive_root = os.path.dirname(os.path.abspath(main))

    # 2. other folders
    others: List[str] = []
    print("\n2) Other folders with backups to include (older phones, copies, .zip folders).")
    print("   One per line; press Enter on an empty line when done.")
    print("   Folders are searched WITH their subfolders. To take only the files lying")
    print("   directly in a folder, end it with \\*   e.g.  X:\\My Drive\\*")
    print("   Type ?  to list every folder in %s that holds backups." % drive_root)
    while True:
        line = ask("   > ").strip()
        if not line:
            break
        if line == "?":
            current = [main] + others + ([old_work] if os.path.isdir(old_work) else [])
            print("\n".join(folder_report(drive_root, current, out_dir)))
            continue
        p = clean_path(line)
        err = check_other(p)
        if err:
            print("   !! " + err)
            continue
        others.append(p)

    inputs = [main] + others + ([old_work] if os.path.isdir(old_work) else [])

    # 3. what will be read
    skipped: List[str] = []
    sources = collect_inputs(inputs, exclude=[out_dir, trash], problems=skipped)
    print("\n" + LINE)
    if not sources:
        print("No sms-/calls- backups found in those folders. Nothing to do.")
        return 1
    print("3) These backups will be read (subfolders and .zip files included):")
    print("\n".join(describe_sources(sources)))
    if skipped:
        print("\nSKIPPED and left where they are:")
        for p in skipped:
            print("    " + p)
    prior = earlier_merged(out_dir)
    if prior:
        print("\nYour earlier merged file(s) are included too (set aside first, never overwritten):")
        for f in prior:
            print("    " + f)
    print("\nMerged file(s) go to:      %s   (KEEP this folder)" % out_dir)
    print("Verified originals go to:  %s   (you delete this later)" % trash)

    # local disk space: the merged file is built in the temp folder, and Google
    # Drive for desktop also keeps what it reads/writes in its cache on this PC
    tmp = tmp_dir or tempfile.gettempdir()
    by_group = {}
    for g, _, s in sources:
        by_group[g] = max(by_group.get(g, 0), source_size(s))
    need = int(sum(by_group.values()) * 1.5) + sum(os.path.getsize(f) for f in prior)
    free = shutil.disk_usage(tmp).free
    print("\nThe merged file is built in %s first: about %s needed, %s free there." % (tmp, human(need), human(free)))
    if free < need * 2:
        print("!! That's tight: Google Drive for desktop also caches files on this PC while it")
        print("   reads them and uploads the merged file. Free some space first if you can.")

    print(LINE)
    if not _yes(ask, "4) Start the merge?"):
        print("Stopped. Nothing was changed.")
        return 1

    ok = merge(inputs, out_dir, tmp_dir=tmp_dir)
    if not ok and not _yes(
        ask, "\nThe merge reported problems (see above). Run the verify step anyway? It only moves files it fully verified."
    ):
        print("Stopped after the merge. Nothing was moved or deleted.")
        return 1

    print("\n" + LINE)
    print("5) Verifying: re-reading every original and checking it against the merged file...")
    verified = verify_merged(inputs, out_dir, move_sources_to=trash)

    print("\n" + LINE)
    if verified:
        print("DONE -- VERIFIED.")
    else:
        print("NOT fully verified -- read the summary above. Do NOT delete anything that wasn't moved.")
    print("\nBefore deleting anything:")
    print("  * Wait until Google Drive for desktop has finished UPLOADING the merged file(s) in")
    print("    %s  (the Drive icon in the taskbar says it's up to date / the" % out_dir)
    print("    file shows as synced in drive.google.com). Until then the only copy is on this PC.")
    print("Then:")
    print("  * delete  %s  (in drive.google.com or File Explorer)," % trash)
    print("  * and EMPTY THE DRIVE TRASH in drive.google.com -- space is only freed then.")
    print("Keep  %s : that is your backup." % out_dir)
    return 0 if verified else 1
