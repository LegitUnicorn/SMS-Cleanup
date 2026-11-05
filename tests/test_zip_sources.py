import os
import zipfile

import pytest

from smscleanup import merge as M
from smscleanup import verify as V
from smscleanup.sources import collect_inputs, describe_sources, find_backup_folders, open_source
from smscleanup.xmlstream import iter_records
from tests.test_cleanup import EMOJI, call, calls, mms, sms, smses, write

A, B, C, D = (sms(100 + i, "msg %s" % c) for i, c in enumerate("ABCD"))
M1 = mms(3000, "pic1", 1)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(V.time, "sleep", lambda s: None)


def zip_one(path, member, text, method=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(path, "w", method) as zf:
        zf.writestr(member, text)


def read(path):
    with open(path, "rb") as fh:
        return list(iter_records(fh))


@pytest.fixture
def drive(tmp_path):
    """SMS (plain xml) + 'SMS zips' (zipped copies + one zip-only backup) + 'Old phone'."""
    root = tmp_path / "MyDrive"
    sms_dir = root / "SMS"
    zips = root / "SMS zips"
    old = root / "Old phone"
    for d in (sms_dir, zips, old):
        d.mkdir(parents=True)
    write(sms_dir, "sms-20250301000000.xml", smses(A, B, M1))
    # zipped copy of the same backup, plus an older zip-only backup holding C
    zip_one(zips / "sms-20250301000000.zip", "sms-20250301000000.xml", smses(A, B, M1))
    zip_one(zips / "sms-20250101000000.zip", "sms-20250101000000.xml", smses(sms(100, "msg A", read=0), C, EM := sms(2000, EMOJI)))
    zip_one(zips / "calls-20250101000000.zip", "calls-20250101000000.xml", calls(call(1), call(2)))
    write(zips, "notes.zip", "not a zip at all")  # unrelated, silently ignored
    write(old, "sms-20210101000000.xml", smses(D))
    return root


def test_collect_reads_inside_zips(drive):
    problems = []
    found = collect_inputs([str(drive)], problems=problems)
    names = sorted(os.path.basename(s.replace("\0", "/")) for _, _, s in found)
    assert names == [
        "calls-20250101000000.xml",
        "sms-20210101000000.xml",
        "sms-20250101000000.xml",
        "sms-20250301000000.xml",
        "sms-20250301000000.xml",
    ]
    assert problems == []  # notes.zip isn't named like a backup -> not reported
    text = "\n".join(describe_sources(found))
    assert "SMS zips  ->  2 sms, 1 calls, 3 of them in .zip files" in text
    assert "sms-20250101000000.zip/sms-20250101000000.xml" in text
    zipped = [s for _, _, s in found if "\0" in s][0]
    with open_source(zipped) as fh:
        assert len(list(iter_records(fh))) >= 2


def test_merge_and_verify_across_folders_and_zips(drive, tmp_path, capsys):
    out = str(tmp_path / "merged")
    trash = str(tmp_path / "to-delete")
    inputs = [str(drive / "SMS"), str(drive / "SMS zips"), str(drive / "Old phone")]
    assert M.merge(inputs, out) is True
    sms_out = read(os.path.join(out, "sms-20250301000000-merged.xml"))
    assert sorted(r.get("body") or r.tag for r in sms_out) == ["Hi \U0001F600 there", "mms", "msg A", "msg B", "msg C", "msg D"]
    assert len(read(os.path.join(out, "calls-20250101000000-merged.xml"))) == 2
    log = capsys.readouterr().out
    # the zipped copy of the newest backup adds nothing
    assert "sms-20250301000000.zip/sms-20250301000000.xml: 3 records -> 0 new, 3 already merged" in log

    assert M.verify_merged(inputs, out, move_sources_to=trash) is True
    assert sorted(os.listdir(trash)) == [
        "calls-20250101000000.zip",
        "sms-20210101000000.xml",
        "sms-20250101000000.zip",
        "sms-20250301000000.xml",
        "sms-20250301000000.zip",
    ]
    assert os.listdir(drive / "SMS zips") == ["notes.zip"]  # unrelated file untouched


def test_damaged_zip_stays_others_move(drive, tmp_path, capsys):
    bad = drive / "SMS zips" / "sms-20250201000000.zip"
    zip_one(bad, "sms-20250201000000.xml", smses(A, B), method=zipfile.ZIP_STORED)
    data = bytearray(bad.read_bytes())
    i = data.index(b"msg B")
    data[i : i + 5] = b"msg X"  # body changed -> CRC check fails at the end of the read
    bad.write_bytes(bytes(data))
    out = str(tmp_path / "merged")
    trash = str(tmp_path / "to-delete")
    inputs = [str(drive)]
    assert M.merge(inputs, out) is False  # INCOMPLETE: one source unreadable
    assert M.verify_merged(inputs, out, move_sources_to=trash) is False
    log = capsys.readouterr().out
    assert "could not fully read SOURCE file sms-20250201000000.zip/sms-20250201000000.xml" in log
    assert "stay where they are" in log
    assert os.path.exists(bad)
    assert "sms-20250201000000.zip" not in os.listdir(trash)
    assert len(os.listdir(trash)) == 5  # everything else verified and moved


def test_unopenable_and_password_zips_are_reported_and_left(drive, tmp_path, capsys):
    write(drive / "SMS zips", "sms-20240101000000.zip", "garbage, not a zip")
    locked = drive / "SMS zips" / "sms-20240201000000.zip"
    zip_one(locked, "sms-20240201000000.xml", smses(A))
    # set the "encrypted" flag (bit 0 of the general-purpose flags) in both headers,
    # as a password-protected archive has; zipfile itself can't write those
    data = bytearray(locked.read_bytes())
    data[data.index(b"PK\x03\x04") + 6] |= 0x1
    data[data.index(b"PK\x01\x02") + 8] |= 0x1
    locked.write_bytes(bytes(data))
    problems = []
    collect_inputs([str(drive)], problems=problems)
    assert any("sms-20240101000000.zip: can't be opened as a zip" in p for p in problems)
    assert any("sms-20240201000000.xml is password-protected" in p for p in problems)
    out = str(tmp_path / "merged")
    trash = str(tmp_path / "to-delete")
    assert M.merge([str(drive)], out) is True
    assert M.verify_merged([str(drive)], out, move_sources_to=trash) is True
    log = capsys.readouterr().out
    assert "SKIPPED (stays where it is)" in log
    assert sorted(f for f in os.listdir(drive / "SMS zips")) == [
        "notes.zip",
        "sms-20240101000000.zip",
        "sms-20240201000000.zip",
    ]


def test_find_backup_folders(drive):
    (drive / "SMS-merged").mkdir()
    write(drive / "SMS-merged", "sms-20250301000000-merged.xml", smses(A))
    hits = find_backup_folders(str(drive))
    got = {os.path.relpath(k, drive): dict(v) for k, v in hits.items()}
    assert got == {"SMS": {"xml": 1}, "SMS zips": {"zip": 3}, "Old phone": {"xml": 1}}
