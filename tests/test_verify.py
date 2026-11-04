import os
import shutil

import pytest

from smscleanup import cleanup, verify as V
from smscleanup.xmlstream import iter_records
from tests.test_cleanup import EMOJI, call, calls, mms, sms, smses, write

A, B, C, D = (sms(100 + i, "msg %s" % c) for i, c in enumerate("ABCD"))
M1, M2 = mms(3000, "pic1", 1), mms(3001, "pic2", 2)
EM = sms(2000, EMOJI)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(V.time, "sleep", lambda s: None)
    monkeypatch.setattr(cleanup.time, "sleep", lambda s: None)


@pytest.fixture
def cleaned(tmp_path):
    d = tmp_path / "SMS"
    d.mkdir()
    # older backups have a stale read flag / contact name, and C was later deleted
    write(d, "sms-20250101000000.xml", smses(sms(100, "msg A", read=0, contact="(Unknown)"), B, C, EM, M1))
    write(d, "sms-20250201000000.xml", smses(A, B, EM, M1, M2))
    write(d, "sms-20250301000000.xml", smses(A, B, D, EM, M1, M2))
    write(d, "calls-20250101000000.xml", calls(call(1), call(2)))
    write(d, "calls-20250201000000.xml", calls(call(1), call(2), call(3)))
    write(d, "notes.txt", "x")
    work = str(tmp_path / "w")
    cleanup.analyze(str(d), work)
    cleanup.apply(str(d), work)
    return d, work


def test_verified_after_real_cleanup(cleaned, capsys):
    d, work = cleaned
    assert V.verify(str(d), work) is True
    out = capsys.readouterr().out
    assert "VERIFIED: every message in the 3 removed files" in out
    assert "MISSING from remaining files:    0 (0 distinct messages)" in out
    # the stale read flag / contact name show up as "details differ", by attribute
    assert "same message, details differ:    1" in out
    assert "contact_name" in out and "read" in out
    assert "notes.txt" in out  # other files listed as untouched


def test_detects_missing_and_saves_them(cleaned, capsys):
    d, work = cleaned
    os.remove(os.path.join(work, "recovered", "sms-recovered.xml"))  # loses message C
    assert V.verify(str(d), work) is False
    out = capsys.readouterr().out
    assert "MISSING from remaining files:    1 (1 distinct messages)" in out
    assert "NOT VERIFIED" in out
    rescue = os.path.join(work, "verify-missing-smses.xml")
    with open(rescue, "rb") as fh:
        assert [el.get("body") for el in iter_records(fh)] == ["msg C"]
    assert 'count="1"' in open(rescue).read()


def test_detects_tampered_kept_file(cleaned, capsys):
    d, work = cleaned
    # someone edits the newest backup and drops an MMS that older files have
    write(d, "sms-20250301000000.xml", smses(A, B, D, EM, M2))
    assert V.verify(str(d), work) is False
    out = capsys.readouterr().out
    # M1 is in both removed files: 2 records, 1 distinct message, saved once
    assert "MISSING from remaining files:    2 (1 distinct messages)" in out
    with open(os.path.join(work, "verify-missing-smses.xml"), "rb") as fh:
        recs = list(iter_records(fh))
    assert [r.tag for r in recs] == ["mms"] and recs[0].get("date") == "3000"


def test_mms_with_different_attachment_is_missing_not_differs(cleaned, capsys):
    d, work = cleaned
    # same MMS date/addr but the attachment bytes differ -> must not count as present
    write(d, "sms-20250301000000.xml", smses(A, B, D, EM, mms(3000, "pic1", 999), M2))
    assert V.verify(str(d), work) is False


def test_unreadable_removed_file_is_not_verified(cleaned, monkeypatch, capsys):
    d, work = cleaned
    real = V.iter_records

    def broken(fh, *a, **k):
        if "superseded" in fh.name and fh.name.endswith("sms-20250101000000.xml"):
            raise OSError(5, "Input/output error")
        return real(fh, *a, **k)

    monkeypatch.setattr(V, "iter_records", broken)
    assert V.verify(str(d), work) is False
    out = capsys.readouterr().out
    assert "could not fully read REMOVED file sms-20250101000000.xml" in out


def test_transient_error_is_retried(cleaned, monkeypatch, capsys):
    d, work = cleaned
    real = V.iter_records
    state = {"n": 0}

    def flaky(fh, *a, **k):
        if fh.name.endswith("sms-20250201000000.xml") and state["n"] == 0:
            state["n"] += 1

            def gen():
                yield from []
                raise OSError(5, "Input/output error")

            return gen()
        return real(fh, *a, **k)

    monkeypatch.setattr(V, "iter_records", flaky)
    assert V.verify(str(d), work) is True
    assert state["n"] == 1


def test_nothing_to_verify_before_apply(tmp_path):
    d = tmp_path / "SMS"
    d.mkdir()
    write(d, "sms-20250101000000.xml", smses(A))
    assert V.verify(str(d), str(tmp_path / "w")) is False


def test_cli_exit_codes(cleaned):
    from smscleanup.cli import main

    d, work = cleaned
    assert main(["verify", str(d), "--work-dir", work]) == 0
    os.remove(os.path.join(work, "recovered", "sms-recovered.xml"))
    assert main(["verify", str(d), "--work-dir", work]) == 1
