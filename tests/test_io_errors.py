import os

import pytest

from smscleanup import cleanup
from smscleanup.xmlstream import iter_records
from tests.test_cleanup import sms, smses, write

A, B, C, D, E, F, G = (sms(100 + i, "msg %s" % c) for i, c in enumerate("ABCDEFG"))
X = "sms-20250218004201.xml"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(cleanup.time, "sleep", lambda s: None)


def make_flaky(monkeypatch, name, fail_after, times):
    real = cleanup.iter_records
    state = {"fails": 0}

    def flaky(fh, *a, **k):
        gen = real(fh, *a, **k)
        if os.path.basename(getattr(fh, "name", "")) == name and state["fails"] < times:
            state["fails"] += 1
            for i, el in enumerate(gen):
                if i == fail_after:
                    raise OSError(5, "Input/output error")
                yield el
        else:
            yield from gen

    monkeypatch.setattr(cleanup, "iter_records", flaky)
    return state


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "SMS"
    d.mkdir()
    write(d, "sms-20250414013013.xml", smses(A, B, E, F, G))  # newest
    write(d, X, smses(A, C, B, E, F, G, D))  # C, D only here
    write(d, "sms-20250101000000.xml", smses(A, C))  # older, relies on X for C
    return d


def decisions(state):
    return {n: (e["decision"], e["unique"], bool(e["error"])) for n, e in state["files"].items()}


def test_transient_io_error_is_retried_automatically(folder, tmp_path, monkeypatch):
    clean = cleanup.analyze(str(folder), str(tmp_path / "clean"))
    flaky = make_flaky(monkeypatch, X, fail_after=2, times=1)
    st = cleanup.analyze(str(folder), str(tmp_path / "w"))
    assert flaky["fails"] == 1
    assert decisions(st) == decisions(clean)
    assert decisions(st)[X] == ("supersede", 2, False)


def test_persistent_error_kept_then_retry_errors_fixes_it(folder, tmp_path, monkeypatch):
    work = str(tmp_path / "w")
    flaky = make_flaky(monkeypatch, X, fail_after=2, times=99)
    st = cleanup.analyze(str(folder), work, read_attempts=3)
    assert flaky["fails"] == 3
    assert decisions(st)[X] == ("keep", 1, True)  # partial read: A, C
    # the older file was judged against X's partial keys and relies on C
    assert decisions(st)["sms-20250101000000.xml"] == ("supersede", 0, False)
    assert "--retry-errors" in open(os.path.join(work, "report.md")).read()

    monkeypatch.undo()
    monkeypatch.setattr(cleanup.time, "sleep", lambda s: None)
    st = cleanup.analyze(str(folder), work, retry_errors=True)
    assert decisions(st)[X] == ("supersede", 2, False)
    order = {n: e["order"] for n, e in st["files"].items()}
    assert order[X] == 1  # stays in its original place

    cleanup.apply(str(folder), work)  # verification must pass (C comes from recovered)
    assert sorted(os.listdir(folder)) == ["sms-20250414013013.xml"]
    with open(os.path.join(work, "recovered", "sms-recovered.xml"), "rb") as fh:
        assert sorted(el.get("body") for el in iter_records(fh)) == ["msg C", "msg D"]


def test_retry_errors_after_apply_already_ran(folder, tmp_path, monkeypatch):
    work = str(tmp_path / "w")
    make_flaky(monkeypatch, X, fail_after=2, times=99)
    cleanup.analyze(str(folder), work)
    monkeypatch.undo()
    monkeypatch.setattr(cleanup.time, "sleep", lambda s: None)
    cleanup.apply(str(folder), work)  # moves only the older file; X kept
    assert sorted(os.listdir(folder)) == [X, "sms-20250414013013.xml"]
    cleanup.analyze(str(folder), work, retry_errors=True)
    cleanup.apply(str(folder), work)
    assert sorted(os.listdir(folder)) == ["sms-20250414013013.xml"]
    with open(os.path.join(work, "recovered", "sms-recovered.xml"), "rb") as fh:
        assert sorted(el.get("body") for el in iter_records(fh)) == ["msg C", "msg D"]


def test_parse_errors_are_not_retried(tmp_path):
    d = tmp_path / "SMS"
    d.mkdir()
    write(d, "sms-20250414013013.xml", smses(A, B))
    write(d, X, smses(A, B, C)[:-5])  # truncated upload
    st = cleanup.analyze(str(d), str(tmp_path / "w"))
    e = st["files"][X]
    assert e["decision"] == "keep" and e["error"] and not e["io_error"]
