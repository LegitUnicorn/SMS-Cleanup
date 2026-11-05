"""Run the notebook's Step 3 / 3b cells (pure Python) against a fake Drive."""
import json
import os
import zipfile

from tests.test_cleanup import call, calls, sms, smses, write

NB = os.path.join(os.path.dirname(__file__), "..", "notebooks", "SMS_Cleanup_Colab.ipynb")


def cell(prefix):
    nb = json.load(open(NB))
    for c in nb["cells"]:
        s = "".join(c["source"])
        if s.startswith(prefix):
            return s
    raise AssertionError("cell not found: " + prefix)


def run(drive_root, backup_dir, capsys, other="", with_3b=False):
    ns = {}
    code = cell("# @title Step 3 ")
    code = code.replace("'/content/drive/MyDrive/SMSBackupRestore'", repr(str(backup_dir)))
    code = code.replace("OTHER_FOLDERS = ''", "OTHER_FOLDERS = %r" % other)
    code = code.replace("'/content/drive/MyDrive'", repr(str(drive_root)))
    exec(compile(code, "step3", "exec"), ns)
    if with_3b:
        capsys.readouterr()
        code = cell("# @title Step 3b").replace("'/content/drive/MyDrive'", repr(str(drive_root)))
        exec(compile(code, "step3b", "exec"), ns)
    return capsys.readouterr().out, ns


def make_drive(tmp_path):
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
    zips = root / "SMS zips"
    zips.mkdir()
    for ts in ("20250101000000", "20250201000000"):
        with zipfile.ZipFile(zips / ("sms-%s.zip" % ts), "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("sms-%s.xml" % ts, smses(sms(5, "z")))
    write(zips, "sms-20240101000000.zip", "broken")
    (root / "Elsewhere").mkdir()
    write(root / "Elsewhere", "sms-20200101000000.xml", smses(sms(9, "e")))
    return root, d, zips


def test_step3_lists_subfolders_and_call_logs(tmp_path, capsys):
    root, d, _ = make_drive(tmp_path)
    out, ns = run(root, d, capsys)
    assert "Steps 5 and 6 will read ALL of these (subfolders and .zip files included):" in out
    assert "7 backups" in out and "in 2 folder(s):" in out
    assert "/SMS  ->  2 sms" in out
    assert "/SMS/Old phone  ->  4 sms, 1 calls" in out
    assert out.count("sms-20250414013013.xml") == 1  # each file listed once
    assert "skip Step 4" in out and "Run Step 3b first" in out
    assert ns["INPUTS"] == [str(d)]


def test_step3_other_folders_with_zips(tmp_path, capsys):
    root, d, zips = make_drive(tmp_path)
    out, ns = run(root, d, capsys, other=" %s ; %s/ " % (zips, root / "Elsewhere"))
    assert ns["INPUTS"] == [str(d), str(zips), str(root / "Elsewhere")]
    assert "10 backups" in out and "in 4 folder(s):" in out
    assert "/SMS zips  ->  2 sms, 2 of them in .zip files" in out
    assert "sms-20250101000000.zip/sms-20250101000000.xml" in out
    assert "These will be SKIPPED and left where they are:" in out
    assert "sms-20240101000000.zip: can't be opened as a zip" in out


def test_step3_bad_other_folder_is_flagged(tmp_path, capsys):
    root, d, _ = make_drive(tmp_path)
    out, ns = run(root, d, capsys, other=str(root / "Nope"))
    assert 'OTHER_FOLDERS: "%s" is not a folder' % (root / "Nope") in out
    assert ns["INPUTS"] == [str(d)]


def test_step3b_marks_included_and_missing_folders(tmp_path, capsys):
    root, d, zips = make_drive(tmp_path)
    out, _ = run(root, d, capsys, other=str(zips), with_3b=True)
    lines = out.strip().splitlines()
    assert any(l.startswith("included") and l.rstrip().endswith("/SMS  (2 .xml)") for l in lines)
    assert any(l.startswith("included") and "/SMS/Old phone  (5 .xml)" in l for l in lines)
    assert any(l.startswith("included") and "/SMS zips  (3 .zip)" in l for l in lines)
    assert any(l.startswith("NOT included") and "/Elsewhere  (1 .xml)" in l for l in lines)


def test_step3_points_to_step4_with_old_cleanup_folder(tmp_path, capsys):
    root = tmp_path / "MyDrive"
    d = root / "SMS"
    d.mkdir(parents=True)
    write(d, "sms-20250414013013.xml", smses(sms(1, "a")))
    (root / "SMS-cleanup" / "superseded").mkdir(parents=True)
    (root / "SMS-cleanup" / "recovered").mkdir()
    write(root / "SMS-cleanup" / "recovered", "sms-recovered.xml", smses(sms(2, "b")))
    out, _ = run(root, d, capsys)
    assert "SMS-cleanup/recovered  ->  1 sms" in out
    assert "do Step 4 next" in out


def test_step3_searches_drive_when_folder_is_wrong(tmp_path, capsys):
    root = tmp_path / "MyDrive"
    real = root / "Phone backups"
    real.mkdir(parents=True)
    write(real, "sms-20250414013013.xml", smses(sms(1, "a")))
    out, _ = run(root, root / "SMSBackupRestore", capsys)
    assert "No sms-/calls- backups found" in out
    assert "%s  (1 .xml)" % real in out
