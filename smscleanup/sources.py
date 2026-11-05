"""Finding and opening backup sources: plain .xml files and .xml files inside .zip archives.

A *source* is a string.  For a plain file it is the file's path.  For a backup
stored inside a zip archive it is ``"<zip path>\\0<member name>"`` (a NUL byte
can never appear in a real path, so the two can't be confused).  The archive
is read in place, streaming; nothing is extracted to disk.
"""

from __future__ import annotations

import os
import re
import zipfile
from collections import Counter
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from .cleanup import human

SEP = "\0"
NAME_RE = re.compile(r"^(sms|calls)-.*\.xml$", re.IGNORECASE)
ZIP_NAME_RE = re.compile(r"^(sms|calls)-.*\.zip$", re.IGNORECASE)
TS_RE = re.compile(r"(\d{14})")
SKIP_DIR_SUFFIXES = ("-cleanup", "-merged", "-to-delete")

Source = Tuple[str, str, str]  # (group, timestamp or "", source)


def container(src: str) -> str:
    """The file on disk that holds this source (the .xml itself, or its .zip)."""
    return src.split(SEP, 1)[0]


def is_zipped(src: str) -> bool:
    return SEP in src


def display_name(src: str) -> str:
    if SEP in src:
        z, member = src.split(SEP, 1)
        return "%s/%s" % (os.path.basename(z), os.path.basename(member))
    return os.path.basename(src)


def source_size(src: str) -> int:
    """Size of the backup XML itself (uncompressed size for a zipped one)."""
    if SEP in src:
        z, member = src.split(SEP, 1)
        with zipfile.ZipFile(z) as zf:
            return zf.getinfo(member).file_size
    return os.path.getsize(src)


@contextmanager
def open_source(src: str) -> Iterator:
    """Open a source for binary reading (zip members are decompressed on the fly)."""
    if SEP in src:
        z, member = src.split(SEP, 1)
        with zipfile.ZipFile(z) as zf, zf.open(member) as fh:
            yield fh
    else:
        with open(src, "rb") as fh:
            yield fh


def collect_inputs(
    inputs: Sequence[str],
    exclude: Sequence[str] = (),
    problems: Optional[List[str]] = None,
) -> List[Source]:
    """Every sms-*/calls-*.xml under ``inputs`` (folders searched recursively),
    including ones inside .zip archives.  Newest first within each group.

    Zips that can't be read, or whose backups are password-protected, are
    skipped and described in ``problems`` (if given).
    """
    if problems is None:
        problems = []
    excl = [os.path.realpath(e) for e in exclude]
    seen = set()
    out: List[Source] = []

    def excluded(real: str) -> bool:
        return any(real == e or real.startswith(e + os.sep) for e in excl)

    def add_xml(path: str, m: "re.Match[str]") -> None:
        real = os.path.realpath(path)
        if real in seen or excluded(real):
            return
        seen.add(real)
        ts = TS_RE.search(os.path.basename(path))
        out.append((m.group(1).lower(), ts.group(1) if ts else "", path))

    def add_zip(path: str) -> None:
        real = os.path.realpath(path)
        if excluded(real):
            return
        named_like_backup = bool(ZIP_NAME_RE.match(os.path.basename(path)))
        try:
            with zipfile.ZipFile(path) as zf:
                infos = zf.infolist()
        except (zipfile.BadZipFile, OSError) as exc:
            if named_like_backup:
                problems.append("%s: can't be opened as a zip (%s); skipped" % (path, exc))
            return
        found = 0
        for info in infos:
            if info.is_dir():
                continue
            base = os.path.basename(info.filename)
            m = NAME_RE.match(base)
            if not m:
                continue
            if info.flag_bits & 0x1:
                problems.append("%s: %s is password-protected; skipped" % (path, base))
                continue
            key = real + SEP + info.filename
            if key in seen:
                continue
            seen.add(key)
            found += 1
            ts = TS_RE.search(base) or TS_RE.search(os.path.basename(path))
            out.append((m.group(1).lower(), ts.group(1) if ts else "", path + SEP + info.filename))
        if not found and named_like_backup and not any(p.startswith(path + ":") for p in problems):
            problems.append("%s: no sms-/calls- .xml backup inside; skipped" % path)

    def add(path: str) -> None:
        name = os.path.basename(path)
        m = NAME_RE.match(name)
        if m:
            add_xml(path, m)
        elif name.lower().endswith(".zip"):
            add_zip(path)

    for p in inputs:
        if os.path.isfile(p):
            add(p)
        elif os.path.isdir(p):
            for d, subdirs, files in os.walk(p):
                if any(os.path.realpath(d) == e for e in excl):
                    subdirs[:] = []
                    continue
                subdirs.sort()
                for f in sorted(files):
                    add(os.path.join(d, f))
        else:
            raise SystemExit("input not found: %s" % p)
    # newest first; files without a timestamp (e.g. sms-recovered.xml) last
    out.sort(key=lambda t: (t[0], t[1] != "", t[1], t[2]), reverse=True)
    return out


def describe_sources(files: List[Source], show: int = 3) -> List[str]:
    """Human summary of collect_inputs() output, grouped by folder."""
    by_dir: Dict[str, dict] = {}
    containers = set()
    for group, _, src in files:
        c = container(src)
        e = by_dir.setdefault(os.path.dirname(c), {"sms": 0, "calls": 0, "zipped": 0, "bytes": 0, "names": []})
        e[group] += 1
        if is_zipped(src):
            e["zipped"] += 1
        if c not in containers:
            containers.add(c)
            e["bytes"] += os.path.getsize(c)
        e["names"].append(display_name(src))
    total = sum(e["bytes"] for e in by_dir.values())
    lines = ["%d backups (%s on Drive) in %d folder(s):" % (len(files), human(total), len(by_dir))]
    for d in sorted(by_dir):
        e = by_dir[d]
        kinds = [
            ("%d sms" % e["sms"]) if e["sms"] else "",
            ("%d calls" % e["calls"]) if e["calls"] else "",
            ("%d of them in .zip files" % e["zipped"]) if e["zipped"] else "",
        ]
        lines.append("  %s  ->  %s (%s)" % (d, ", ".join(k for k in kinds if k), human(e["bytes"])))
        names = sorted(e["names"])
        shown = names if len(names) <= 2 * show else names[:show] + ["..."] + names[-show:]
        lines += ["      " + n for n in shown]
    return lines


def find_backup_folders(root: str, skip_suffixes: Sequence[str] = SKIP_DIR_SUFFIXES) -> Dict[str, Counter]:
    """Every folder under ``root`` holding sms-/calls- backups, by file name only
    (fast: zip contents are not opened).  {folder: Counter(xml=n, zip=n)}"""
    hits: Dict[str, Counter] = {}
    for d, subdirs, files in os.walk(root):
        subdirs[:] = sorted(s for s in subdirs if not s.endswith(tuple(skip_suffixes)))
        c: Counter = Counter()
        for f in files:
            if NAME_RE.match(f):
                c["xml"] += 1
            elif ZIP_NAME_RE.match(f):
                c["zip"] += 1
        if c:
            hits[d] = c
    return hits
