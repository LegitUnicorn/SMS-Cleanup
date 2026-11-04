"""Analyze / apply logic.

Algorithm (per group: ``sms-*.xml`` and ``calls-*.xml`` are independent)
------------------------------------------------------------------------
Files are processed newest -> oldest while a set of every message key seen so
far is maintained.  For each file we count how many of its records are *new*
(not in any newer file):

* mostly new  (unique / total >= keep_ratio)  -> **keep** the file untouched
  (the newest full backup, or a genuine incremental backup)
* mostly old                                   -> **supersede** it.  The few
  records that are only in this file (messages that were later deleted from
  the phone) are copied into a small ``<group>-recovered.xml``.

Invariant: every record of a superseded file is in a kept file or in the
recovered file.  ``apply`` re-checks that invariant from disk (it re-parses
the recovered file it just wrote) before it moves or deletes anything.

Everything is resumable: per-file results are committed to ``state.json`` in
the work directory, so a Colab disconnect costs at most one file of progress.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set

from .records import ROOT_FOR_GROUP, XML_DECL, record_key, serialize
from .xmlstream import ParseResult, iter_records

STATE_VERSION = 1
FNAME_RE = re.compile(r"^(sms|calls)-(\d{14})[^/\\]*\.xml$", re.IGNORECASE)
KEY_LEN = 16


def log(msg: str) -> None:
    print(msg, flush=True)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024
    return str(n)


@dataclass
class BackupFile:
    group: str
    ts: str
    name: str
    path: str
    size: int


def discover(backup_dir: str) -> List[BackupFile]:
    files = []
    for name in os.listdir(backup_dir):
        m = FNAME_RE.match(name)
        if not m:
            continue
        path = os.path.join(backup_dir, name)
        if not os.path.isfile(path):
            continue
        files.append(BackupFile(m.group(1).lower(), m.group(2), name, path, os.path.getsize(path)))
    # newest first; name as tie-breaker for determinism
    files.sort(key=lambda f: (f.group, f.ts, f.name), reverse=True)
    return files


# --------------------------------------------------------------------------
# Work directory / state
# --------------------------------------------------------------------------


class WorkDir:
    def __init__(self, path: str) -> None:
        self.path = path
        self.keys = os.path.join(path, "keys")
        self.extras = os.path.join(path, "extras")
        self.recovered = os.path.join(path, "recovered")
        self.superseded = os.path.join(path, "superseded")
        self.state_path = os.path.join(path, "state.json")
        self.report_path = os.path.join(path, "report.md")
        for d in (path, self.keys, self.extras, self.recovered):
            os.makedirs(d, exist_ok=True)

    def load_state(self) -> dict:
        if os.path.exists(self.state_path):
            with open(self.state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            if state.get("version") != STATE_VERSION:
                raise SystemExit("state.json version mismatch; use a fresh --work-dir")
            return state
        return {"version": STATE_VERSION, "files": {}, "next_order": 0}

    def save_state(self, state: dict) -> None:
        _atomic_write_text(self.state_path, json.dumps(state, indent=1, sort_keys=True))

    def keys_path(self, name: str) -> str:
        return os.path.join(self.keys, name + ".keys")

    def extras_path(self, name: str) -> str:
        return os.path.join(self.extras, name + ".frag")


def _atomic_write_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _write_keys(path: str, keys: Iterable[bytes]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(b"".join(sorted(keys)))
    os.replace(tmp, path)


def read_keys(path: str) -> Set[bytes]:
    with open(path, "rb") as f:
        data = f.read()
    if len(data) % KEY_LEN:
        raise SystemExit("corrupt keys file: %s" % path)
    return {data[i : i + KEY_LEN] for i in range(0, len(data), KEY_LEN)}


def _copy_file(src: str, dst: str) -> None:
    tmp = dst + ".tmp"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


# --------------------------------------------------------------------------
# analyze
# --------------------------------------------------------------------------


def analyze(
    backup_dir: str,
    work_dir: str,
    keep_ratio: float = 0.5,
    max_files: Optional[int] = None,
    max_hours: Optional[float] = None,
    extras_cap_mb: int = 1024,
    tmp_dir: Optional[str] = None,
) -> dict:
    wd = WorkDir(work_dir)
    if tmp_dir:
        os.makedirs(tmp_dir, exist_ok=True)
    state = wd.load_state()
    state.setdefault("backup_dir", os.path.abspath(backup_dir))
    files = discover(backup_dir)
    if not files:
        log("No sms-*.xml / calls-*.xml files found in %s" % backup_dir)
        return state

    started = time.monotonic()
    processed_now = 0
    by_group: Dict[str, List[BackupFile]] = {}
    for f in files:
        by_group.setdefault(f.group, []).append(f)

    for group, gfiles in sorted(by_group.items()):
        done = {n: e for n, e in state["files"].items() if e["group"] == group}
        for f in gfiles:
            e = done.get(f.name)
            if e and not e.get("moved") and e["size"] != f.size:
                raise SystemExit(
                    "%s changed size since it was analyzed (%d -> %d). "
                    "Start over with an empty --work-dir." % (f.name, e["size"], f.size)
                )
        pending = [f for f in gfiles if f.name not in done]
        log(
            "[%s] %d files total, %d already analyzed, %d to go (%s)"
            % (group, len(gfiles), len(done), len(pending), human(sum(f.size for f in pending)))
        )
        if not pending:
            continue
        if done:
            oldest_done = min(e["ts"] for e in done.values())
            if any(f.ts > oldest_done for f in pending):
                log(
                    "  note: some new files are newer than files already analyzed; "
                    "that is safe, they are simply compared against everything seen so far."
                )
        seen: Set[bytes] = set()
        for name, e in sorted(done.items(), key=lambda kv: kv[1]["order"]):
            seen |= read_keys(wd.keys_path(name))
        if done:
            log("  loaded %d known message keys from previous runs" % len(seen))

        for f in pending:
            if max_files is not None and processed_now >= max_files:
                log("Reached --max-files; stopping. Re-run to continue.")
                return _finish(wd, state)
            if max_hours is not None and time.monotonic() - started > max_hours * 3600:
                log("Reached --max-hours; stopping. Re-run to continue.")
                return _finish(wd, state)
            entry = _analyze_file(f, wd, seen, keep_ratio, extras_cap_mb * (1 << 20), tmp_dir)
            entry["order"] = state["next_order"]
            state["next_order"] += 1
            state["files"][f.name] = entry
            wd.save_state(state)
            processed_now += 1
    return _finish(wd, state)


def _finish(wd: WorkDir, state: dict) -> dict:
    wd.save_state(state)
    report = build_report(state)
    _atomic_write_text(wd.report_path, report)
    log("\n" + report)
    log("Report written to %s" % wd.report_path)
    return state


def _analyze_file(
    f: BackupFile,
    wd: WorkDir,
    seen: Set[bytes],
    keep_ratio: float,
    extras_cap: int,
    tmp_dir: Optional[str],
) -> dict:
    log("  -> %s (%s)" % (f.name, human(f.size)))
    t0 = time.monotonic()
    keys: Set[bytes] = set()
    unique_keys: Set[bytes] = set()
    tags: Counter = Counter()
    unique_tags: Counter = Counter()
    total = 0
    error = None
    result = ParseResult()

    # Unique records are spooled to a *local* temp file while we stream, so
    # the common case (old full backup with a handful of unique messages)
    # needs only one read.  If the spool gets large the file is almost
    # certainly a keeper and we stop spooling.
    spool = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".frag", dir=tmp_dir, delete=False
    )
    spool_bytes = 0
    spool_ok = True

    def progress(nbytes: int, nrec: int) -> None:
        dt = time.monotonic() - t0
        rate = nbytes / dt if dt else 0
        eta = (f.size - nbytes) / rate if rate else 0
        log(
            "     %5.1f%%  %s/s  %d records  ~%dm left"
            % (100.0 * nbytes / max(f.size, 1), human(rate), nrec, eta // 60)
        )

    try:
        with open(f.path, "rb") as fh:
            for el in iter_records(fh, result, progress=progress):
                total += 1
                tags[el.tag] += 1
                k = record_key(el)
                keys.add(k)
                if k in seen:
                    continue
                seen.add(k)
                unique_keys.add(k)
                unique_tags[el.tag] += 1
                if spool_ok:
                    s = serialize(el)
                    spool_bytes += len(s)
                    if spool_bytes > extras_cap:
                        spool_ok = False
                    else:
                        spool.write(s)
    except ET.ParseError as exc:
        error = "XML parse error after %d records: %s" % (total, exc)
    except OSError as exc:
        error = "read error after %d records: %s" % (total, exc)
    finally:
        spool.close()

    unique = len(unique_keys)
    if error:
        decision = "keep"
        reason = "unreadable/truncated -> left alone"
    elif total == 0:
        decision = "supersede"
        reason = "no records"
    elif unique / total >= keep_ratio:
        decision = "keep"
        reason = "%.1f%% of records not in any newer file" % (100.0 * unique / total)
    elif unique == 0:
        decision = "supersede"
        reason = "fully contained in newer files"
    else:
        decision = "supersede"
        reason = "%.2f%% unique (%d records) -> copied to recovered file" % (
            100.0 * unique / total,
            unique,
        )

    try:
        if decision == "supersede" and unique:
            if not spool_ok:
                log("     large unique set; second pass to extract %d records" % unique)
                _extract(f.path, unique_keys, spool.name)
            _copy_file(spool.name, wd.extras_path(f.name))
        _write_keys(wd.keys_path(f.name), keys)
    finally:
        try:
            os.unlink(spool.name)
        except OSError:
            pass

    dt = time.monotonic() - t0
    log(
        "     %d records (%s), %d unique -> %s [%s] in %.0fs"
        % (total, ", ".join("%s=%d" % kv for kv in sorted(tags.items())), unique, decision.upper(), reason, dt)
    )
    if error:
        log("     WARNING: " + error)
    return {
        "group": f.group,
        "ts": f.ts,
        "size": f.size,
        "records": total,
        "distinct": len(keys),
        "unique": unique,
        "tags": dict(tags),
        "unique_tags": dict(unique_tags),
        "decision": decision,
        "reason": reason,
        "error": error,
        "root_tag": result.root_tag,
        "seconds": round(dt, 1),
        "moved": False,
    }


def _extract(path: str, wanted: Set[bytes], out_path: str) -> None:
    remaining = set(wanted)
    with open(path, "rb") as fh, open(out_path, "w", encoding="utf-8") as out:
        for el in iter_records(fh):
            k = record_key(el)
            if k in remaining:
                remaining.discard(k)
                out.write(serialize(el))
    if remaining:
        raise RuntimeError("could not re-extract %d records from %s" % (len(remaining), path))


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def build_report(state: dict) -> str:
    lines = ["# SMS backup cleanup report", ""]
    groups = sorted({e["group"] for e in state["files"].values()})
    for g in groups:
        entries = sorted(
            ((n, e) for n, e in state["files"].items() if e["group"] == g),
            key=lambda kv: kv[1]["ts"],
            reverse=True,
        )
        keep = [e for _, e in entries if e["decision"] == "keep"]
        sup = [e for _, e in entries if e["decision"] == "supersede"]
        lines += [
            "## %s backups" % g,
            "",
            "| | |",
            "|---|---|",
            "| files analyzed | %d (%s) |" % (len(entries), human(sum(e["size"] for _, e in entries))),
            "| keep | %d (%s) |" % (len(keep), human(sum(e["size"] for e in keep))),
            "| supersede (reclaimable) | %d (**%s**) |" % (len(sup), human(sum(e["size"] for e in sup))),
            "| records rescued into recovered file | %d |" % sum(e["unique"] for e in sup),
            "| files with read errors (kept) | %d |" % sum(1 for _, e in entries if e["error"]),
            "",
            "| file | size | records | unique | decision | moved |",
            "|---|---:|---:|---:|---|---|",
        ]
        for n, e in entries:
            lines.append(
                "| %s | %s | %d | %d | %s%s | %s |"
                % (
                    n,
                    human(e["size"]),
                    e["records"],
                    e["unique"],
                    e["decision"],
                    " (error)" if e["error"] else "",
                    "yes" if e["moved"] else "",
                )
            )
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------


def apply(backup_dir: str, work_dir: str, delete: bool = False) -> None:
    wd = WorkDir(work_dir)
    state = wd.load_state()
    if not state["files"]:
        raise SystemExit("Nothing analyzed yet; run `analyze` first.")
    groups = sorted({e["group"] for e in state["files"].values()})
    for g in groups:
        _apply_group(g, backup_dir, wd, state, delete)
    _finish(wd, state)
    log(
        "\nDone. Superseded files are in %s.\n"
        "When you are happy, delete that folder in Google Drive AND empty the Drive trash -- "
        "space is only freed once the trash is emptied.\n"
        "Keep the files in %s: they contain messages that exist in no other backup."
        % ("the Drive trash" if delete else wd.superseded, wd.recovered)
    )


def _apply_group(group: str, backup_dir: str, wd: WorkDir, state: dict, delete: bool) -> None:
    entries = {n: e for n, e in state["files"].items() if e["group"] == group}
    kept = {n: e for n, e in entries.items() if e["decision"] == "keep"}
    sup = {n: e for n, e in entries.items() if e["decision"] == "supersede"}
    log("[%s] keep %d, supersede %d" % (group, len(kept), len(sup)))

    # 1. kept files must still be there, unchanged
    for n, e in kept.items():
        p = os.path.join(backup_dir, n)
        if not os.path.isfile(p) or os.path.getsize(p) != e["size"]:
            raise SystemExit("ABORT: kept file %s is missing or changed size" % n)

    # 2. build the recovered file from all extras fragments
    rec_path = os.path.join(wd.recovered, "%s-recovered.xml" % group)
    with_extras = sorted((n for n, e in sup.items() if e["unique"]), key=lambda n: sup[n]["ts"], reverse=True)
    expected = sum(sup[n]["unique"] for n in with_extras)
    if with_extras:
        root = ROOT_FOR_GROUP.get(group, group)
        tmp = rec_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as out:
            out.write(XML_DECL)
            out.write("<!--Records found only in superseded backups; written by smscleanup-->\n")
            out.write(
                '<%s count="%d" backup_date="%d" type="full">\n'
                % (root, expected, int(time.time() * 1000))
            )
            for n in with_extras:
                with open(wd.extras_path(n), "r", encoding="utf-8") as frag:
                    shutil.copyfileobj(frag, out, 1 << 20)
            out.write("</%s>\n" % root)
        os.replace(tmp, rec_path)

    # 3. verify from disk: re-parse recovered file, union with kept keys
    covered: Set[bytes] = set()
    if with_extras:
        n_rec = 0
        with open(rec_path, "rb") as fh:
            for el in iter_records(fh):
                covered.add(record_key(el))
                n_rec += 1
        if n_rec != expected:
            raise SystemExit("ABORT: recovered file has %d records, expected %d" % (n_rec, expected))
        log("  recovered file: %s (%d records, %s)" % (rec_path, n_rec, human(os.path.getsize(rec_path))))
    for n in kept:
        covered |= read_keys(wd.keys_path(n))
    for n in sup:
        missing = read_keys(wd.keys_path(n)) - covered
        if missing:
            raise SystemExit(
                "ABORT: %d records of %s would be lost; nothing was moved for this group" % (len(missing), n)
            )
    log("  verified: every record of all %d superseded files is in a kept file or the recovered file" % len(sup))

    # 4. move (or delete) superseded files
    if not delete:
        os.makedirs(wd.superseded, exist_ok=True)
    freed = 0
    for n, e in sorted(sup.items()):
        if e["moved"]:
            continue
        src = os.path.join(backup_dir, n)
        if not os.path.exists(src):
            log("  %s already gone; skipping" % n)
            e["moved"] = True
            continue
        if os.path.getsize(src) != e["size"]:
            raise SystemExit("ABORT: %s changed size since analysis" % n)
        if delete:
            os.remove(src)
        else:
            shutil.move(src, os.path.join(wd.superseded, n))
        e["moved"] = True
        freed += e["size"]
        wd.save_state(state)
        log("  %s %s (%s)" % ("deleted" if delete else "moved", n, human(e["size"])))
    log("  %s %s" % ("deleted" if delete else "moved out of the backup folder:", human(freed)))


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------


def inspect(path: str) -> None:
    res = ParseResult()
    tags: Counter = Counter()
    keys: Set[bytes] = set()
    dates: List[int] = []
    t0 = time.monotonic()
    try:
        with open(path, "rb") as fh:
            for el in iter_records(fh, res, progress=lambda b, r: log("  %s read, %d records" % (human(b), r))):
                tags[el.tag] += 1
                keys.add(record_key(el))
                d = el.get("date")
                if d and d.isdigit():
                    v = int(d)
                    dates.append(v if v > 10**11 else v * 1000)
    except ET.ParseError as exc:
        log("PARSE ERROR: %s" % exc)
    log("root <%s %s>" % (res.root_tag, " ".join('%s="%s"' % kv for kv in res.root_attrib.items())))
    log("records: %s, distinct: %d" % (dict(tags), len(keys)))
    if dates:
        log(
            "date range: %s .. %s"
            % (
                time.strftime("%Y-%m-%d", time.gmtime(min(dates) / 1000)),
                time.strftime("%Y-%m-%d", time.gmtime(max(dates) / 1000)),
            )
        )
    log("%s in %.1fs" % (human(res.bytes_read), time.monotonic() - t0))


if __name__ == "__main__":  # pragma: no cover
    sys.exit("use: python -m smscleanup")
