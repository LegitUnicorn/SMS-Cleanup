from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import __version__, cleanup


def _default_work_dir(backup_dir: str) -> str:
    backup_dir = os.path.abspath(backup_dir.rstrip("/\\"))
    return backup_dir + "-cleanup"


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="smscleanup",
        description="Find and remove redundant full SMS/MMS/call-log backups without losing a single message.",
    )
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="read every backup and decide what to keep (read-only for the backup folder)")
    a.add_argument("backup_dir")
    a.add_argument("--work-dir", help="where state/report/recovered files go (default: <backup_dir>-cleanup)")
    a.add_argument(
        "--keep-ratio",
        type=float,
        default=0.5,
        help="keep a file if at least this fraction of its records are not in any newer file (default 0.5)",
    )
    a.add_argument("--max-files", type=int, help="stop after this many files (resume by re-running)")
    a.add_argument("--max-hours", type=float, help="stop starting new files after this many hours")
    a.add_argument("--extras-cap-mb", type=int, default=1024, help="local spool cap for unique records (default 1024)")
    a.add_argument("--tmp-dir", help="local temp dir for spooling (default: system temp)")
    a.add_argument(
        "--retry-errors",
        action="store_true",
        help="re-read files that failed with read errors in an earlier run",
    )
    a.add_argument(
        "--read-attempts", type=int, default=3, help="tries per file when Drive returns an I/O error (default 3)"
    )

    r = sub.add_parser("report", help="print the report from the last analyze")
    r.add_argument("backup_dir")
    r.add_argument("--work-dir")

    ap = sub.add_parser("apply", help="verify, write recovered file(s), then move superseded backups out")
    ap.add_argument("backup_dir")
    ap.add_argument("--work-dir")
    ap.add_argument("--delete", action="store_true", help="delete instead of moving to <work-dir>/superseded")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    v = sub.add_parser(
        "verify",
        help="after apply: re-read everything from disk and prove every removed message still exists",
    )
    v.add_argument("backup_dir")
    v.add_argument("--work-dir")
    v.add_argument("--read-attempts", type=int, default=3)

    m = sub.add_parser(
        "merge",
        help="combine all backups into ONE deduplicated file per type (sms / calls)",
    )
    m.add_argument("inputs", nargs="+", help="backup files or folders (searched recursively)")
    m.add_argument("--out-dir", required=True, help="where the merged file(s) are written")
    m.add_argument("--tmp-dir", help="local temp dir for building the file (default: system temp)")
    m.add_argument("--read-attempts", type=int, default=3)

    vm = sub.add_parser(
        "verify-merged",
        help="re-read every source backup and prove each message is in the merged file",
    )
    vm.add_argument("inputs", nargs="+", help="the same inputs given to merge")
    vm.add_argument("--out-dir", required=True, help="folder holding the *-merged.xml file(s)")
    vm.add_argument("--read-attempts", type=int, default=3)
    vm.add_argument(
        "--move-sources-to",
        help="only if VERIFIED: move all source backups into this folder, so you can delete one folder",
    )

    i = sub.add_parser("inspect", help="print counts/date range for one backup file")
    i.add_argument("file")

    args = p.parse_args(argv)

    if args.cmd == "inspect":
        cleanup.inspect(args.file)
        return 0
    if args.cmd == "merge":
        from .merge import merge

        return 0 if merge(args.inputs, args.out_dir, tmp_dir=args.tmp_dir, attempts=args.read_attempts) else 1
    if args.cmd == "verify-merged":
        from .merge import verify_merged

        ok = verify_merged(
            args.inputs, args.out_dir, attempts=args.read_attempts, move_sources_to=args.move_sources_to
        )
        return 0 if ok else 1

    if not os.path.isdir(args.backup_dir):
        print("not a directory: %s" % args.backup_dir, file=sys.stderr)
        return 2
    work_dir = args.work_dir or _default_work_dir(args.backup_dir)

    if args.cmd == "analyze":
        if not 0 < args.keep_ratio <= 1:
            p.error("--keep-ratio must be in (0, 1]")
        cleanup.analyze(
            args.backup_dir,
            work_dir,
            keep_ratio=args.keep_ratio,
            max_files=args.max_files,
            max_hours=args.max_hours,
            extras_cap_mb=args.extras_cap_mb,
            tmp_dir=args.tmp_dir,
            retry_errors=args.retry_errors,
            read_attempts=args.read_attempts,
        )
    elif args.cmd == "report":
        state = cleanup.WorkDir(work_dir).load_state()
        print(cleanup.build_report(state))
    elif args.cmd == "verify":
        from .verify import verify

        return 0 if verify(args.backup_dir, work_dir, attempts=args.read_attempts) else 1
    elif args.cmd == "apply":
        if not args.yes:
            ans = input(
                "This will %s superseded backups from %s. Type 'yes' to continue: "
                % ("DELETE" if args.delete else "move", args.backup_dir)
            )
            if ans.strip().lower() != "yes":
                print("aborted")
                return 1
        cleanup.apply(args.backup_dir, work_dir, delete=args.delete)
    return 0
