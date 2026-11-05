"""Independent verification, run after ``apply`` and before anything is deleted.

This deliberately does NOT use anything ``analyze`` produced (state.json,
key files, decisions).  It looks only at what is on disk:

* REMAINING = every sms-/calls- backup still in the backup folder
              + every file in ``<work-dir>/recovered``
* REMOVED   = every backup in ``<work-dir>/superseded``

Every record of every REMOVED file is re-read from disk and classified:

* exact    -- a byte-for-byte identical record (all attributes, all MMS parts
              and attachments) exists in REMAINING
* differs  -- the same message (same date, direction, number, text and
              attachment content) exists, but some details differ; we report
              *which* attributes, e.g. ``read`` or ``contact_name``
* MISSING  -- nothing in REMAINING matches.  These are written to
              ``<work-dir>/verify-missing-*.xml`` so they are never lost, and
              the run ends NOT VERIFIED.
"""

from __future__ import annotations

import hashlib
import os
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter
from typing import Callable, Dict, List, Optional, Tuple

from .cleanup import FNAME_RE, human, log
from .records import XML_DECL, record_key, serialize
from .sources import display_name, open_source, source_size
from .xmlstream import iter_records

AttrFP = Tuple[Tuple[str, int], ...]


def strict_hash(el: ET.Element) -> bytes:
    h = hashlib.blake2b(digest_size=16)
    _feed(h, el)
    return h.digest()


def _enc(s: str) -> bytes:
    return s.encode("utf-8", "surrogatepass")


def _feed(h, el: ET.Element) -> None:
    h.update(b"<" + _enc(el.tag))
    for k in sorted(el.attrib):
        h.update(b"\x1f" + _enc(k) + b"=")
        h.update(_enc(el.attrib[k]))
    text = (el.text or "").strip()
    if text:
        h.update(b"\x1e" + _enc(text))
    for c in el:
        _feed(h, c)
    h.update(b">")


def attr_fp(el: ET.Element) -> AttrFP:
    return tuple(sorted((k, zlib.crc32(_enc(v))) for k, v in el.attrib.items()))


def _diff_names(a: AttrFP, b: AttrFP) -> List[str]:
    da, db = dict(a), dict(b)
    return sorted(k for k in set(da) | set(db) if da.get(k) != db.get(k))


def _with_retries(fn: Callable[[str], object], path: str, attempts: int, wait: float):
    for attempt in range(1, attempts + 1):
        try:
            return fn(path), None
        except (OSError, zipfile.BadZipFile, zlib.error, EOFError) as exc:
            if attempt >= attempts:
                return None, "read error: %s" % exc
            log("     read error (%s); retrying %d of %d" % (exc, attempt + 1, attempts))
            time.sleep(wait)
        except ET.ParseError as exc:
            return None, "XML parse error: %s" % exc
    return None, "unreadable"  # pragma: no cover


def _backups(d: str) -> List[str]:
    if not os.path.isdir(d):
        return []
    return sorted(n for n in os.listdir(d) if FNAME_RE.match(n) and os.path.isfile(os.path.join(d, n)))


def verify(backup_dir: str, work_dir: str, attempts: int = 3, retry_wait: float = 15.0) -> bool:
    rec_dir = os.path.join(work_dir, "recovered")
    sup_dir = os.path.join(work_dir, "superseded")
    remaining = [os.path.join(backup_dir, n) for n in _backups(backup_dir)]
    if os.path.isdir(rec_dir):
        remaining += [
            os.path.join(rec_dir, n) for n in sorted(os.listdir(rec_dir)) if n.lower().endswith(".xml")
        ]
    removed = [os.path.join(sup_dir, n) for n in _backups(sup_dir)]
    others = sorted(
        n for n in os.listdir(backup_dir) if not FNAME_RE.match(n) and os.path.isfile(os.path.join(backup_dir, n))
    )

    if not removed:
        log("Nothing in %s to verify. Run `apply` first (it only moves files; nothing is deleted)." % sup_dir)
        return False
    if others:
        log("Other files in the backup folder (not backups; never touched): %s" % ", ".join(others))
    return compare(
        remaining,
        removed,
        work_dir,
        attempts=attempts,
        retry_wait=retry_wait,
        ok_msg="VERIFIED: every message in the %d removed files is still present in the remaining files.\n"
        "It is safe to delete %s and then empty the Drive trash." % (len(removed), sup_dir),
        fail_msg="NOT VERIFIED -- do NOT delete %s." % sup_dir,
    )


def compare(
    remaining: List[str],
    removed: List[str],
    work_dir: str,
    ok_msg: str,
    fail_msg: str,
    target: str = "remaining",
    source: str = "removed",
    attempts: int = 3,
    retry_wait: float = 15.0,
    info: Optional[dict] = None,
) -> bool:
    """Prove every record in ``removed`` files exists in ``remaining`` files.

    Reads everything from disk; uses nothing from analyze/apply.  If ``info``
    is given it is filled with ``clean_sources`` (removed files read completely
    with nothing missing), ``missing`` and ``target_ok``.
    """
    clean: List[str] = []
    target_ok = True
    os.makedirs(work_dir, exist_ok=True)
    log("%-10s %d files (%s)" % (target.upper() + ":", len(remaining), human(sum(source_size(p) for p in remaining))))
    log("%-10s %d files (%s)" % (source.upper() + ":", len(removed), human(sum(source_size(p) for p in removed))))

    # ---- 1. index everything that remains, straight from disk ----
    index: Dict[bytes, Dict[bytes, AttrFP]] = {}
    remain_tags: Counter = Counter()
    problems: List[str] = []

    def load(path: str) -> Counter:
        tags: Counter = Counter()
        with open_source(path) as fh:
            for el in iter_records(fh):
                tags[el.tag] += 1
                index.setdefault(record_key(el), {})[strict_hash(el)] = attr_fp(el)
        return tags

    log("\nReading %s files:" % target)
    for p in remaining:
        t0 = time.monotonic()
        tags, err = _with_retries(load, p, attempts, retry_wait)
        if err:
            problems.append("could not fully read %s file %s (%s)" % (target.upper(), display_name(p), err))
            target_ok = False
            log("  %s: %s" % (display_name(p), err))
            continue
        remain_tags.update(tags)
        log("  %s: %s in %.0fs" % (display_name(p), _tags(tags), time.monotonic() - t0))

    # ---- 2. check every removed record ----
    missing_out: Dict[str, object] = {}
    missing_counts: Counter = Counter()
    missing_seen: set = set()
    total: Counter = Counter()
    removed_distinct: Dict[str, set] = {}

    def check(path: str) -> Counter:
        c: Counter = Counter()
        # Missing records are spooled to a temp file and only committed once the
        # whole file was read, so a retried read cannot double-count them.
        spool_path = os.path.join(work_dir, "verify-spool.tmp")
        spooled: List[Tuple[bytes, str, int, int]] = []
        with open(spool_path, "w+b") as spool:
            try:
                with open_source(path) as fh:
                    for el in iter_records(fh):
                        c["records"] += 1
                        key = record_key(el)
                        removed_distinct.setdefault(el.tag, set()).add(key)
                        cands = index.get(key)
                        sh = strict_hash(el)
                        if cands is None:
                            c["missing"] += 1
                            data = serialize(el).encode("utf-8")
                            root = "calls" if el.tag == "call" else "smses"
                            spooled.append((sh, root, spool.tell(), len(data)))
                            spool.write(data)
                        elif sh in cands:
                            c["exact"] += 1
                        else:
                            c["differs"] += 1
                            fp = attr_fp(el)
                            best = min((_diff_names(fp, other) for other in cands.values()), key=len)
                            for name in best or ["(attachment/recipient details)"]:
                                c["diff:" + name] += 1
                for sh, root, offset, length in spooled:
                    if sh in missing_seen:
                        continue
                    missing_seen.add(sh)
                    if root not in missing_out:
                        missing_out[root] = open(
                            os.path.join(work_dir, "verify-missing-%s.xml.part" % root), "wb"
                        )
                    spool.seek(offset)
                    missing_out[root].write(spool.read(length))  # type: ignore[attr-defined]
                    missing_counts[root] += 1
            finally:
                spool.close()
                os.remove(spool_path)
        return c

    log("\nChecking %s files:" % source)
    for p in removed:
        t0 = time.monotonic()
        c, err = _with_retries(check, p, attempts, retry_wait)
        name = display_name(p)
        if err:
            problems.append("could not fully read %s file %s (%s)" % (source.upper(), name, err))
            log("  %s: %s" % (name, err))
            continue
        total.update(c)
        if c["missing"] == 0:
            clean.append(p)
        log(
            "  %s: %d records -> %d exact, %d differ, %d MISSING (%.0fs)"
            % (name, c["records"], c["exact"], c["differs"], c["missing"], time.monotonic() - t0)
        )

    missing_files = _finish_missing(work_dir, missing_out, missing_counts)

    # ---- 3. verdict ----
    log("\n================ VERIFY SUMMARY ================")
    log("%s files hold: %s" % (target, _tags(remain_tags)))
    log(
        "distinct messages across " + source + " files: %s"
        % ", ".join("%s=%d" % (t, len(s)) for t, s in sorted(removed_distinct.items()))
    )
    log("%s records checked: %d" % (source, total["records"]))
    log("  exact copy present:              %d" % total["exact"])
    log("  same message, details differ:    %d" % total["differs"])
    for k, v in sorted(((k[5:], v) for k, v in total.items() if k.startswith("diff:")), key=lambda kv: -kv[1]):
        log("      %-28s %d" % (k, v))
    log(
        "  %-32s %d (%d distinct messages)"
        % ("MISSING from %s files:" % target, total["missing"], sum(missing_counts.values()))
    )
    for p in problems:
        log("  PROBLEM: " + p)

    ok = total["missing"] == 0 and not problems
    if info is not None:
        info.update(clean_sources=clean, missing=total["missing"], target_ok=target_ok)
    if ok:
        log("\n" + ok_msg)
    else:
        log("\n" + fail_msg)
        if missing_files:
            log("Records not found anywhere else were saved to: %s" % ", ".join(missing_files))
        if problems:
            log("Re-run verify; if a file keeps failing to read, keep it.")
    return ok


def _tags(c: Counter) -> str:
    return ", ".join("%s=%d" % kv for kv in sorted(c.items())) or "no records"


def _finish_missing(work_dir: str, outs: Dict[str, object], counts: Counter) -> List[str]:
    paths = []
    for root, fh in outs.items():
        fh.close()  # type: ignore[attr-defined]
        part = os.path.join(work_dir, "verify-missing-%s.xml.part" % root)
        final = os.path.join(work_dir, "verify-missing-%s.xml" % root)
        with open(final, "wb") as out, open(part, "rb") as src:
            out.write(XML_DECL.encode())
            out.write(b'<%s count="%d" backup_date="%d" type="full">\n' % (root.encode(), counts[root], int(time.time() * 1000)))
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
            out.write(b"</%s>\n" % root.encode())
        os.remove(part)
        paths.append(final)
    return paths
