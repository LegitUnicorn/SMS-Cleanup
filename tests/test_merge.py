import os

import pytest

from smscleanup import cleanup
from smscleanup import merge as M
from smscleanup import verify as V
from smscleanup.cli import main
from smscleanup.xmlstream import iter_records
from tests.test_cleanup import EMOJI, call, calls, mms, sms, smses, write

A, B, C, D, E = (sms(100 + i, "msg %s" % c) for i, c in enumerate("ABCDE"))
M1, M2 = mms(3000, "pic1", 1), mms(3001, "pic2", 2)
EM = sms(2000, EMOJI)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(V.time, "sleep", lambda s: None)


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "SMS"
    d.mkdir()
    # oldest: stale read flag on A, C deleted from phone later
    write(d, "sms-20250101000000.xml", smses(sms(100, "msg A", read=0), B, C, EM, M1))
    write(d, "sms-20250201000000.xml", smses(A, B, EM, M1, M2))
    write(d, "sms-20250301000000.xml", smses(A, B, D, EM, M1, M2, A))  # newest, with an in-file dup
    write(d, "sms-20250302000000.xml", smses(E))  # incremental
    write(d, "calls-20250101000000.xml", calls(call(1), call(2)))
    write(d, "calls-20250201000000.xml", calls(call(2), call(3)))
    write(d, "notes.txt", "x")
    return d


def read(path):
    with open(path, "rb") as fh:
        return list(iter_records(fh))


def test_merge_produces_union_newest_copy_wins(folder, tmp_path, capsys):
    out = str(tmp_path / "merged")
    assert M.merge([str(folder)], out) is True
    files = sorted(os.listdir(out))
    assert files == ["calls-20250201000000-merged.xml", "merge-report.md", "sms-20250302000000-merged.xml"]
    recs = read(os.path.join(out, "sms-20250302000000-merged.xml"))
    bodies = sorted(r.get("body") or r.tag for r in recs)
    assert bodies == ["Hi \U0001F600 there", "mms", "mms", "msg A", "msg B", "msg C", "msg D", "msg E"]
    a = [r for r in recs if r.get("body") == "msg A"]
    assert len(a) == 1 and a[0].get("read") == "1"  # newest copy kept, in-file dup dropped
    assert [r.get("date") for r in read(os.path.join(out, "calls-20250201000000-merged.xml"))] == ["2", "3", "1"]
    raw = open(os.path.join(out, "sms-20250302000000-merged.xml"), encoding="utf-8").read()
    assert 'count="8"' in raw and "&#55357;&#56832;" in raw  # app's own emoji dialect
    log = capsys.readouterr().out
    assert "self-check passed: all 8 records read back identical" in log
    assert "MERGE OK" in log


def test_verify_merged_passes_then_catches_loss(folder, tmp_path, capsys):
    out = str(tmp_path / "merged")
    M.merge([str(folder)], out)
    assert M.verify_merged([str(folder)], out) is True
    log = capsys.readouterr().out
    assert "VERIFIED: every message in all 6 source backups" in log
    assert "read" in log  # the stale read flag shows up as "details differ"

    # drop message C from the merged file -> must be caught and rescued
    p = os.path.join(out, "sms-20250302000000-merged.xml")
    raw = open(p, encoding="utf-8").read()
    lines = [l for l in raw.splitlines(True) if 'body="msg C"' not in l]
    open(p, "w", encoding="utf-8").write("".join(lines))
    assert M.verify_merged([str(folder)], out) is False
    log = capsys.readouterr().out
    assert "NOT VERIFIED" in log
    assert [r.get("body") for r in read(os.path.join(out, "verify-missing-smses.xml"))] == ["msg C"]


def test_merge_includes_superseded_and_recovered_and_skips_own_output(folder, tmp_path):
    # the earlier analyze/apply flow, then merge everything that's left anywhere
    work = str(tmp_path / "SMS-cleanup")
    cleanup.analyze(str(folder), work)
    cleanup.apply(str(folder), work)
    out = os.path.join(str(folder), "merged")  # output inside an input folder
    assert M.merge([str(folder), work], out) is True
    assert M.merge([str(folder), work], out) is True  # re-run must not read its own output
    recs = read(os.path.join(out, "sms-20250302000000-merged.xml"))
    assert len(recs) == 8
    assert M.verify_merged([str(folder), work], out) is True


def test_unreadable_source_makes_merge_incomplete(folder, tmp_path, monkeypatch, capsys):
    real = M.iter_records

    def broken(fh, *a, **k):
        if fh.name.endswith("sms-20250201000000.xml"):
            def gen():
                for i, el in enumerate(real(fh, *a, **k)):
                    if i == 2:
                        raise OSError(5, "Input/output error")
                    yield el
            return gen()
        return real(fh, *a, **k)

    monkeypatch.setattr(M, "iter_records", broken)
    out = str(tmp_path / "merged")
    assert M.merge([str(folder)], out) is False
    log = capsys.readouterr().out
    assert "MERGE INCOMPLETE" in log and "sms-20250201000000.xml could not be read completely" in log
    # nothing is lost: what was readable is still merged, and the other files are complete
    assert len(read(os.path.join(out, "sms-20250302000000-merged.xml"))) == 8


def test_transient_error_retried_without_double_counting(folder, tmp_path, monkeypatch, capsys):
    real = M.iter_records
    state = {"n": 0}

    def flaky(fh, *a, **k):
        if fh.name.endswith("sms-20250101000000.xml") and state["n"] == 0:
            state["n"] += 1

            def gen():
                for i, el in enumerate(real(fh, *a, **k)):
                    if i == 3:
                        raise OSError(5, "Input/output error")
                    yield el
            return gen()
        return real(fh, *a, **k)

    monkeypatch.setattr(M, "iter_records", flaky)
    out = str(tmp_path / "merged")
    assert M.merge([str(folder)], out) is True
    assert len(read(os.path.join(out, "sms-20250302000000-merged.xml"))) == 8
    assert "sms-20250101000000.xml: 5 records -> 1 new" in capsys.readouterr().out


def test_cli(folder, tmp_path):
    out = str(tmp_path / "merged")
    assert main(["merge", str(folder), "--out-dir", out]) == 0
    assert main(["verify-merged", str(folder), "--out-dir", out]) == 0
    assert main(["verify-merged", str(folder), "--out-dir", str(tmp_path / "nothing")]) == 1


def test_move_sources_only_after_verified(folder, tmp_path):
    out = str(tmp_path / "merged")
    trash = str(tmp_path / "to-delete")
    M.merge([str(folder)], out)
    # tamper -> NOT VERIFIED -> nothing moves
    p = os.path.join(out, "sms-20250302000000-merged.xml")
    good = open(p, encoding="utf-8").read()
    open(p, "w", encoding="utf-8").write("".join(l for l in good.splitlines(True) if 'body="msg C"' not in l))
    assert M.verify_merged([str(folder)], out, move_sources_to=trash) is False
    assert not os.path.exists(trash)
    assert len(os.listdir(folder)) == 7
    # restore -> VERIFIED -> all 6 backups move, other files stay
    M.merge([str(folder)], out)
    assert M.verify_merged([str(folder)], out, move_sources_to=trash) is True
    assert os.listdir(folder) == ["notes.txt"]
    assert len(os.listdir(trash)) == 6
    assert len(read(os.path.join(out, "sms-20250302000000-merged.xml"))) == 8


def test_move_sources_into_folder_inside_inputs(folder, tmp_path):
    out = str(tmp_path / "merged")
    trash = os.path.join(str(folder), "to-delete")
    M.merge([str(folder)], out)
    assert main(["verify-merged", str(folder), "--out-dir", out, "--move-sources-to", trash]) == 0
    assert sorted(os.listdir(folder)) == ["notes.txt", "to-delete"]


def test_unreadable_source_stays_others_move(folder, tmp_path, monkeypatch, capsys):
    out = str(tmp_path / "merged")
    trash = str(tmp_path / "to-delete")
    M.merge([str(folder)], out)
    real = V.iter_records

    def broken(fh, *a, **k):
        if fh.name.endswith("sms-20250201000000.xml"):
            raise OSError(5, "Input/output error")
        return real(fh, *a, **k)

    monkeypatch.setattr(V, "iter_records", broken)
    assert M.verify_merged([str(folder)], out, move_sources_to=trash) is False
    log = capsys.readouterr().out
    assert "NOT VERIFIED" in log and "stay where they are" in log
    assert sorted(os.listdir(folder)) == ["notes.txt", "sms-20250201000000.xml"]
    assert len(os.listdir(trash)) == 5


def test_missing_record_moves_nothing_even_if_other_files_clean(folder, tmp_path):
    out = str(tmp_path / "merged")
    trash = str(tmp_path / "to-delete")
    M.merge([str(folder)], out)
    p = os.path.join(out, "sms-20250302000000-merged.xml")
    good = open(p, encoding="utf-8").read()
    open(p, "w", encoding="utf-8").write("".join(l for l in good.splitlines(True) if 'body="msg E"' not in l))
    assert M.verify_merged([str(folder)], out, move_sources_to=trash) is False
    assert not os.path.exists(trash)


def test_report_counts_repeats_inside_a_file_separately(folder, tmp_path, capsys):
    out = str(tmp_path / "merged")
    M.merge([str(folder)], out)
    log = capsys.readouterr().out
    # sms-20250301 holds A twice: 7 records = 6 new + 1 repeated inside the file
    assert "sms-20250301000000.xml: 7 records -> 6 new, 0 already merged (0 identical, 0 with different details), 1 repeated inside this file" in log
    report = open(os.path.join(out, "merge-report.md")).read()
    assert "| sms-20250301000000.xml |" in report and "| 7 | 6 | 0 | 1 | ok |" in report
    # "new" summed over all files equals the records in the merged file
    assert len(read(os.path.join(out, "sms-20250302000000-merged.xml"))) == 8


def test_merge_lists_every_source_folder_first(folder, tmp_path, capsys):
    old = folder / "Old phone"
    old.mkdir()
    write(old, "sms-20211001000933.xml", smses(sms(5, "from 2021")))
    write(old, "calls-20211001000933.xml", calls(call(99)))
    out = str(tmp_path / "merged")
    assert M.merge([str(folder)], out) is True
    log = capsys.readouterr().out
    assert "Merging 8 backups" in log and "in 2 folder(s):" in log
    assert "Old phone  ->  1 sms, 1 calls" in log
    assert "from 2021" in open(os.path.join(out, "sms-20250302000000-merged.xml"), encoding="utf-8").read()
