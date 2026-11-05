"""Run the notebook's Step 3 cell (pure Python) against a fake Drive."""
import json
import os

import pytest

from tests.test_cleanup import call, calls, sms, smses, write

NB = os.path.join(os.path.dirname(__file__), "..", "notebooks", "SMS_Cleanup_Colab.ipynb")


def step3_source():
    nb = json.load(open(NB))
    for c in nb["cells"]:
        s = "".join(c["source"])
        if s.startswith("# @title Step 3"):
            return s
    raise AssertionError("Step 3 cell not found")


def run_step3(drive_root, backup_dir, capsys):
    code = step3_source()
    code = code.replace("'/content/drive/MyDrive/SMSBackupRestore'", repr(str(backup_dir)))
    code = code.replace("'/content/drive/MyDrive'", repr(str(drive_root)))
    exec(compile(code, "step3", "exec"), {})
    return capsys.readouterr().out


def test_step3_lists_subfolders_and_call_logs(tmp_path, capsys):
    root = tmp_path / "MyDrive"
    d = root / "SMS"
    d.mkdir(parents=True)
    write(d, "sms-20250218004201.xml", smses(sms(1, "a")))
    write(d, "sms-20250414013013.xml", smses(sms(1, "a")))
    old = d / "Old phone"
    old.mkdir()
    for day in range(1, 5):
        write(old, "sms-202110%02d000933.xml" % day, smses(sms(day, "x")))
    write(old, "calls-20211001000933.xml", calls(call(1)))
    out = run_step3(root, d, capsys)
    assert "Steps 5 and 6 will read ALL of these (subfolders included):" in out
    assert "7 backup files" in out and "in 2 folder(s):" in out
    assert "/SMS  ->  2 sms" in out
    assert "/SMS/Old phone  ->  4 sms, 1 calls" in out
    assert out.count("sms-20250414013013.xml") == 1  # each file listed once
    assert "skip Step 4" in out


def test_step3_points_to_step4_with_old_cleanup_folder(tmp_path, capsys):
    root = tmp_path / "MyDrive"
    d = root / "SMS"
    d.mkdir(parents=True)
    write(d, "sms-20250414013013.xml", smses(sms(1, "a")))
    (root / "SMS-cleanup" / "superseded").mkdir(parents=True)
    (root / "SMS-cleanup" / "recovered").mkdir()
    write(root / "SMS-cleanup" / "recovered", "sms-recovered.xml", smses(sms(2, "b")))
    out = run_step3(root, d, capsys)
    assert "SMS-cleanup/recovered  ->  1 sms" in out
    assert "do Step 4 next" in out


def test_step3_searches_drive_when_folder_is_wrong(tmp_path, capsys):
    root = tmp_path / "MyDrive"
    real = root / "Phone backups"
    real.mkdir(parents=True)
    write(real, "sms-20250414013013.xml", smses(sms(1, "a")))
    out = run_step3(root, root / "SMSBackupRestore", capsys)
    assert "No sms-/calls- backup files" in out
    assert "1 backup files in %s" % real in out
