"""Merge any number of backups into ONE deduplicated backup file per type.

    smscleanup merge  <inputs...> --out-dir DIR
    smscleanup verify-merged <inputs...> --out-dir DIR

``merge`` reads every ``sms-*.xml`` / ``calls-*.xml`` under the inputs (files
or folders, searched recursively, including backups inside .zip files), newest
first, and writes each message once:

    DIR/sms-<newest timestamp>-merged.xml     (SMS + MMS)
    DIR/calls-<newest timestamp>-merged.xml   (call log, if there are any)

When the same message appears in several backups the copy from the newest
backup is kept (it has the latest read status / contact name).  The output is
in the backup app's own XML format, so it can be restored like any backup.

After writing, the output is re-read from disk and checked record for record
against what was written.  ``verify-merged`` is the independent second step:
it re-reads every input and proves each record exists in the merged file.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
import uuid
from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

from .cleanup import human, log
from .records import ROOT_FOR_GROUP, XML_DECL, record_key, serialize
from .sources import (  # noqa: F401  (collect_inputs/describe_sources re-exported for the notebook)
    collect_inputs,
    container,
    describe_sources,
    display_name,
    open_source,
    source_size,
)
from .verify import _diff_names, _tags, _with_retries, attr_fp, compare, strict_hash
from .xmlstream import iter_records

MERGED_RE = re.compile(r"^(sms|calls)-\d{14}-merged\.xml$", re.IGNORECASE)


def merge(
    inputs: Sequence[str],
    out_dir: str,
    tmp_dir: Optional[str] = None,
    attempts: int = 3,
    retry_wait: float = 15.0,
) -> bool:
    os.makedirs(out_dir, exist_ok=True)
    if tmp_dir:
        os.makedirs(tmp_dir, exist_ok=True)
    skipped: List[str] = []
    files = collect_inputs(inputs, exclude=[out_dir], problems=skipped)
    for p in skipped:
        log("SKIPPED: " + p)
    if not files:
        log("No sms-*.xml / calls-*.xml backups found in: %s" % ", ".join(inputs))
        return False
    log("Merging " + "\n".join(describe_sources(files)))
    ok = True
    report = ["# Merge report", ""]
    for group in sorted({g for g, _, _ in files}):
        gfiles = [(ts, p) for g, ts, p in files if g == group]
        good, lines = _merge_group(group, gfiles, out_dir, tmp_dir, attempts, retry_wait)
        ok = ok and good
        report += lines
    with open(os.path.join(out_dir, "merge-report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    log("\nReport written to %s" % os.path.join(out_dir, "merge-report.md"))
    if ok:
        log(
            "\nMERGE OK. Next run `verify-merged` with the same inputs: it independently re-reads every "
            "source file and proves each message is in the merged file. Delete nothing before it says VERIFIED."
        )
    else:
        log("\nMERGE INCOMPLETE -- see problems above. Delete nothing.")
    return ok


def _merge_group(
    group: str,
    gfiles: List[Tuple[str, str]],
    out_dir: str,
    tmp_dir: Optional[str],
    attempts: int,
    retry_wait: float,
) -> Tuple[bool, List[str]]:
    newest = max((ts for ts, _ in gfiles if ts), default=time.strftime("%Y%m%d%H%M%S"))
    final = os.path.join(out_dir, "%s-%s-merged.xml" % (group, newest))
    log(
        "\n[%s] merging %d files (%s) into %s"
        % (group, len(gfiles), human(sum(source_size(p) for _, p in gfiles)), final)
    )

    # key -> (strict hash, attribute fingerprint) of the copy we wrote
    index: Dict[bytes, Tuple[bytes, tuple]] = {}
    tags: Counter = Counter()
    problems: List[str] = []
    rows = []
    totals: Counter = Counter()

    body = tempfile.NamedTemporaryFile(mode="w+b", suffix=".merge", dir=tmp_dir, delete=False)
    try:
        for ts, path in gfiles:
            name = display_name(path)
            added_here: set = set()  # survives retries: records we already wrote from this file
            t0 = time.monotonic()

            def run(p: str) -> Counter:
                c: Counter = Counter()
                seen_now: set = set()  # keys seen in this read of this file
                with open_source(p) as fh:
                    for el in iter_records(fh):
                        c["records"] += 1
                        k = record_key(el)
                        if k in seen_now:  # the same message twice inside this one backup
                            c["dup_in_file"] += 1
                            continue
                        seen_now.add(k)
                        if k in added_here:  # written by an earlier, interrupted read of this file
                            c["added"] += 1
                            continue
                        prev = index.get(k)
                        if prev is not None:
                            sh = strict_hash(el)
                            if sh == prev[0]:
                                c["dup_exact"] += 1
                            else:
                                c["dup_differs"] += 1
                                for n in _diff_names(attr_fp(el), prev[1]) or ["(attachment/recipient details)"]:
                                    c["diff:" + n] += 1
                            continue
                        body.write(serialize(el).encode("utf-8"))
                        index[k] = (strict_hash(el), attr_fp(el))
                        added_here.add(k)
                        tags[el.tag] += 1
                        c["added"] += 1
                return c

            c, err = _with_retries(run, path, attempts, retry_wait)
            if err:
                problems.append(
                    "%s could not be read completely (%s); %d messages from the readable part were included"
                    % (name, err, len(added_here))
                )
                log("  %s: %s" % (name, err))
                rows.append("| %s | %s | ? | %d | ? | ? | INCOMPLETE |" % (name, human(source_size(path)), len(added_here)))
                continue
            totals.update(c)
            log(
                "  %s: %d records -> %d new, %d already merged (%d identical, %d with different details)%s in %.0fs"
                % (
                    name,
                    c["records"],
                    c["added"],
                    c["dup_exact"] + c["dup_differs"],
                    c["dup_exact"],
                    c["dup_differs"],
                    ", %d repeated inside this file" % c["dup_in_file"] if c["dup_in_file"] else "",
                    time.monotonic() - t0,
                )
            )
            rows.append(
                "| %s | %s | %d | %d | %d | %d | ok |"
                % (
                    name,
                    human(source_size(path)),
                    c["records"],
                    c["added"],
                    c["dup_exact"] + c["dup_differs"],
                    c["dup_in_file"],
                )
            )
        body.flush()

        # ---- write the final file: header with the real count, then the body ----
        count = len(index)
        root = ROOT_FOR_GROUP.get(group, group)
        part = final + ".part"
        with open(part, "wb") as out:
            out.write(XML_DECL.encode())
            out.write(b"<!--Merged by smscleanup from %d backups-->\n" % len(gfiles))
            out.write(
                b'<%s count="%d" backup_set="%s" backup_date="%d" type="full">\n'
                % (root.encode(), count, str(uuid.uuid4()).encode(), int(time.time() * 1000))
            )
            body.seek(0)
            shutil.copyfileobj(body, out, 4 << 20)
            out.write(b"</%s>\n" % root.encode())
        os.replace(part, final)
    finally:
        body.close()
        os.unlink(body.name)

    # ---- self-check: re-read what landed on disk, record for record ----
    log("  re-reading %s (%s) to check it..." % (os.path.basename(final), human(os.path.getsize(final))))
    want = {sh for sh, _ in index.values()}
    got: set = set()
    n = 0
    try:
        with open(final, "rb") as fh:
            for el in iter_records(fh):
                n += 1
                got.add(strict_hash(el))
        if n != count or got != want:
            problems.append(
                "self-check failed: wrote %d records, read back %d (%d not matching)" % (count, n, len(want ^ got))
            )
    except Exception as exc:  # noqa: BLE001 -- report any failure to read our own output
        problems.append("self-check failed: could not re-read output (%s)" % exc)

    log("  merged file holds: %s" % _tags(tags))
    if totals["dup_differs"]:
        log("  copies with different details (newest copy kept), by attribute:")
        for k, v in sorted(((k[5:], v) for k, v in totals.items() if k.startswith("diff:")), key=lambda kv: -kv[1]):
            log("      %-28s %d" % (k, v))
    for p in problems:
        log("  PROBLEM: " + p)
    if not problems:
        log("  self-check passed: all %d records read back identical" % count)

    lines = [
        "## %s" % group,
        "",
        "Output: `%s` — %d records (%s), %s" % (os.path.basename(final), count, _tags(tags), human(os.path.getsize(final))),
        "",
        "| source file | size | records | new | already merged | repeated in file | status |",
        "|---|---:|---:|---:|---:|---:|---|",
    ] + rows + [""]
    if problems:
        lines += ["**Problems:**", ""] + ["- " + p for p in problems] + [""]
    return not problems, lines


def merged_outputs(out_dir: str) -> List[str]:
    if not os.path.isdir(out_dir):
        return []
    return sorted(os.path.join(out_dir, n) for n in os.listdir(out_dir) if MERGED_RE.match(n))


def verify_merged(
    inputs: Sequence[str],
    out_dir: str,
    attempts: int = 3,
    retry_wait: float = 15.0,
    move_sources_to: Optional[str] = None,
) -> bool:
    merged = merged_outputs(out_dir)
    if not merged:
        log("No *-merged.xml files in %s. Run `merge` first." % out_dir)
        return False
    exclude = [out_dir] + ([move_sources_to] if move_sources_to else [])
    skipped: List[str] = []
    sources = [p for _, _, p in collect_inputs(inputs, exclude=exclude, problems=skipped)]
    for p in skipped:
        log("SKIPPED (stays where it is): " + p)
    if not sources:
        log("No source backups found in: %s" % ", ".join(inputs))
        return False
    files = sorted({container(p) for p in sources}) + merged
    sizes = {f: os.path.getsize(f) for f in files}
    info: dict = {}
    ok = compare(
        merged,
        sources,
        out_dir,
        target="merged",
        source="source",
        attempts=attempts,
        retry_wait=retry_wait,
        ok_msg="VERIFIED: every message in all %d source backups is present in the merged file(s):\n  %s\n"
        "Keep the merged file(s). The source backups can be deleted (then empty the Drive trash)."
        % (len(sources), "\n  ".join(merged)),
        fail_msg="NOT VERIFIED -- do NOT delete any source files.",
        info=info,
    )
    if move_sources_to:
        if ok:
            _move_sources(sources, merged, sizes, move_sources_to)
        elif info.get("target_ok") and info.get("missing") == 0 and info.get("clean_sources"):
            # Only problem: some sources could not be read to the end. Every other
            # source was checked completely, so the files holding only those are
            # safe to move.  (A .zip moves only if every backup inside it checked out.)
            clean = set(info["clean_sources"])
            stuck = sorted({container(p) for p in sources if p not in clean})
            movable = [p for p in sources if container(p) not in stuck]
            log(
                "\nNothing is missing, but %d file(s) could not be read to the end, so they stay where "
                "they are:\n  %s\nEverything else was verified completely."
                % (len(stuck), "\n  ".join(stuck))
            )
            if movable:
                _move_sources(movable, merged, sizes, move_sources_to)
        else:
            log("Nothing was moved.")
    return ok


def _move_sources(sources: List[str], merged: List[str], sizes: Dict[str, int], dest: str) -> None:
    """Only after VERIFIED: gather the source files (.xml or .zip) in one folder to delete."""
    files = []
    for p in sources:
        c = container(p)
        if c not in files:
            files.append(c)
    for f in files + merged:
        if not os.path.exists(f) or os.path.getsize(f) != sizes[f]:
            log("A file changed while verifying (%s); not moving anything." % f)
            return
    os.makedirs(dest, exist_ok=True)
    total = 0
    for f in files:
        name = os.path.basename(f)
        target = os.path.join(dest, name)
        n = 1
        while os.path.exists(target):
            stem, ext = os.path.splitext(name)
            target = os.path.join(dest, "%s.%d%s" % (stem, n, ext))
            n += 1
        shutil.move(f, target)
        total += sizes[f]
    log(
        "\nMoved the %d verified source files (%s) to %s.\n"
        "Delete that folder in Google Drive and then EMPTY THE TRASH to free the space."
        % (len(files), human(total), dest)
    )
