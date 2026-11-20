import os
import zipfile

import pytest

from smscleanup import verify as V
from smscleanup.wizard import check_main, check_other, clean_path, wizard
from smscleanup.xmlstream import iter_records
from tests.test_cleanup import call, calls, sms, smses, write

A, B, C, D = (sms(100 + i, "msg %s" % c) for i, c in enumerate("ABCD"))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(V.time, "sleep", lambda s: None)


def answers(*items):
    it = iter(items)
    return lambda prompt="": next(it)


def read(path):
    with open(path, "rb") as fh:
        return list(iter_records(fh))


@pytest.fixture
def drive(tmp_path):
    """Mimics X:\\My Drive with SMS-RecentMerge, loose files, a zip folder, an unrelated folder."""
    root = tmp_path / "My Drive"
    recent = root / "SMS-RecentMerge"
    zips = root / "SMSProBackup"
    for d in (recent, zips, root / "Work"):
        d.mkdir(parents=True)
    write(recent, "sms-20250414013013-merged.xml", smses(A, B))
    write(recent, "calls-20220104000949-merged.xml", calls(call(1)))
    write(root, "sms-20250824005235.xml", smses(B, C))
    write(root, "calls-20221023000904.xml", calls(call(1), call(2)))
    with zipfile.ZipFile(zips / "sms-20260221013057.xml.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("sms-20260221013057.xml", smses(D))
    write(root / "Work", "budget.txt", "x")
    return root


def test_clean_path_accepts_pasted_windows_style_paths(tmp_path):
    assert clean_path('  "%s"  ' % tmp_path) == str(tmp_path)
    assert clean_path(str(tmp_path) + "/") == str(tmp_path)
    assert clean_path('"%s/*"' % tmp_path) == str(tmp_path) + os.sep + "*"


def test_checks_refuse_my_drive_itself(drive):
    assert "can't be the whole drive or My Drive" in check_main(str(drive))
    assert "doesn't exist" in check_main(str(drive / "Nope"))
    assert check_main(str(drive / "SMS-RecentMerge")) is None
    assert "WHOLE drive" in check_other(str(drive))
    assert check_other(str(drive) + os.sep + "*") is None
    assert check_other(str(drive / "SMSProBackup")) is None


def test_full_guided_run(drive, capsys):
    recent = drive / "SMS-RecentMerge"
    rc = wizard(
        answers(
            str(drive),  # refused: My Drive itself
            '"%s"' % recent,  # pasted with quotes
            str(drive),  # refused as OTHER (whole drive)
            "?",  # list folders
            str(drive) + "/*",  # loose files only
            str(drive / "SMSProBackup"),
            "",  # done
            "y",  # start merge
        )
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "can't be the whole drive or My Drive" in out
    assert "would search your WHOLE drive" in out
    assert "NOT included  %s  (2 .xml)  <- loose files: add  %s%s*" % (drive, drive, os.sep) in out
    assert "NOT included  %s  (1 .zip)" % (drive / "SMSProBackup") in out
    assert "MERGE OK" in out and "VERIFIED" in out and "DONE -- VERIFIED." in out
    assert "finished UPLOADING" in out

    merged = drive / "SMS-RecentMerge-merged"
    sms_out = [f for f in os.listdir(merged) if f.startswith("sms-")]
    assert sms_out == ["sms-20260221013057-merged.xml"]
    assert sorted(r.get("body") for r in read(merged / sms_out[0])) == ["msg A", "msg B", "msg C", "msg D"]
    assert len(read(merged / "calls-20221023000904-merged.xml")) == 2
    # originals gathered; unrelated things untouched
    assert sorted(os.listdir(drive / "SMS-RecentMerge-to-delete")) == [
        "calls-20220104000949-merged.xml",
        "calls-20221023000904.xml",
        "sms-20250414013013-merged.xml",
        "sms-20250824005235.xml",
        "sms-20260221013057.xml.zip",
    ]
    assert os.listdir(drive / "Work") == ["budget.txt"]


def test_answering_no_changes_nothing(drive, capsys):
    recent = drive / "SMS-RecentMerge"
    before = sorted(os.listdir(recent))
    assert wizard(answers(str(recent), "", "n")) == 1
    assert "Stopped. Nothing was changed." in capsys.readouterr().out
    assert sorted(os.listdir(recent)) == before
    assert not os.path.exists(drive / "SMS-RecentMerge-merged")


def test_no_backups_found(tmp_path, capsys):
    empty = tmp_path / "My Drive" / "Empty"
    empty.mkdir(parents=True)
    assert wizard(answers(str(empty), "")) == 1
    assert "No sms-/calls- backups found" in capsys.readouterr().out


def test_cli_wizard_handles_ctrl_c(monkeypatch, capsys):
    from smscleanup.cli import main

    def boom(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", boom)
    assert main(["wizard"]) == 1
    assert "Stopped." in capsys.readouterr().out
