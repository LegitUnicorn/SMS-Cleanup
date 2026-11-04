import base64
import io
import os
import random
import re
import shutil

import pytest

from smscleanup import cleanup
from smscleanup.records import record_key
from smscleanup.xmlstream import RefFixer, fix_refs, iter_records

HDR = "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n"


def sms(date, body, addr="+1 (555) 123-4567", typ=2, read=1, contact="Bob"):
    # keep numeric refs (the app writes emoji as &#55357;&#56832;), escape the rest
    body = re.sub(r"&(?!#)", "&amp;", body).replace('"', "&quot;").replace("\n", "&#10;")
    return (
        '  <sms protocol="0" address="%s" date="%d" type="%d" subject="null" body="%s" '
        'toa="null" sc_toa="null" service_center="null" read="%d" status="-1" locked="0" '
        'date_sent="0" sub_id="1" readable_date="whatever" contact_name="%s" />\n'
        % (addr, date, typ, body, read, contact)
    )


def mms(date, text, img_seed, addr="5551234567"):
    img = base64.b64encode(random.Random(img_seed).randbytes(3000)).decode()
    return (
        '  <mms date="%d" msg_box="1" address="%s" m_id="mid%d" ct_t="application/vnd.wap.multipart.related" '
        'read="1" contact_name="Bob">\n'
        "    <parts>\n"
        '      <part seq="-1" ct="application/smil" text="&lt;smil/&gt;" />\n'
        '      <part seq="0" ct="text/plain" text="%s" />\n'
        '      <part seq="1" ct="image/jpeg" name="a.jpg" _data="/data/PART_%d" data="%s" />\n'
        "    </parts>\n"
        '    <addrs>\n      <addr address="%s" type="137" charset="106" />\n    </addrs>\n'
        "  </mms>\n" % (date, addr, date, text, date, img, addr)
    )


def call(date, number="5551234567", duration=30):
    return '  <call number="%s" duration="%d" date="%d" type="1" presentation="1" readable_date="x" contact_name="Bob" />\n' % (
        number,
        duration,
        date,
    )


def smses(*records):
    return HDR + '<smses count="%d" backup_set="x" backup_date="1">\n' % len(records) + "".join(records) + "</smses>\n"


def calls(*records):
    return HDR + '<calls count="%d">\n' % len(records) + "".join(records) + "</calls>\n"


EMOJI = "Hi &#55357;&#56832; there"  # U+1F600 as the app writes it


def write(d, name, text):
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        f.write(text)


@pytest.fixture
def backups(tmp_path):
    d = tmp_path / "SMSBackupRestore"
    d.mkdir()
    A, B, C, D, E, F = (sms(1000 + i, "msg %s" % c) for i, c in enumerate("ABCDEF"))
    EM = sms(2000, EMOJI)
    M1, M2 = mms(3000, "pic1", 1), mms(3001, "pic2", 2)
    # oldest full backup has C (later deleted from phone) and older read/contact flags
    write(d, "sms-20240101000000.xml", smses(A, B, C, EM, M1, sms(1000, "msg A", addr="5551234567", read=0, contact="(Unknown)")))
    write(d, "sms-20240201000000.xml", smses(A, B, D, EM, M1))
    write(d, "sms-20240301000000.xml", smses(A, B, D, E, EM, M1, M2))  # newest full
    write(d, "sms-20240302000000.xml", smses(F))  # incremental
    write(d, "sms-20240115000000.xml", smses(A, B, C)[:-5])  # truncated upload
    write(d, "calls-20240101000000.xml", calls(call(1), call(2), call(3)))
    write(d, "calls-20240201000000.xml", calls(call(2), call(3), call(4)))
    write(d, "notes.txt", "ignore me")
    return d


def test_fix_refs_pairs_and_garbage():
    assert fix_refs(b"&#55357;&#56832;") == b"&#128512;"
    assert fix_refs(b"&#xD83D;&#xDE00;") == b"&#128512;"
    assert fix_refs(b"x&#55357;y") == b"x&#65533;y"
    assert fix_refs(b"&#1;&#10;&amp;") == b"&#65533;&#10;&amp;"
    assert fix_refs(b"a\x01b") == b"a?b"


def test_ref_fixer_any_chunk_boundary():
    data = ('<r><x b="%s" /></r>' % (EMOJI * 3)).encode()
    want = fix_refs(data)
    for size in range(1, 30):
        fx = RefFixer()
        out = b"".join(fx.feed(data[i : i + size]) for i in range(0, len(data), size)) + fx.feed(b"", True)
        assert out == want, size


def test_iter_records_small_chunks_and_emoji():
    text = smses(sms(1, EMOJI), mms(2, "hey &#55357;&#56832;", 5)).encode()
    recs = [(el.tag, el.get("body")) for el in iter_records(io.BytesIO(text), chunk_size=7)]
    assert recs == [("sms", "Hi \U0001F600 there"), ("mms", None)]


def test_key_ignores_volatile_fields():
    a = next(iter_records(io.BytesIO(smses(sms(5, "x", addr="+1 555-123-4567", read=0)).encode())))
    ka = record_key(a)
    b = next(iter_records(io.BytesIO(smses(sms(5, "x", addr="5551234567", contact="Z")).encode())))
    assert ka == record_key(b)


def test_end_to_end(backups, tmp_path):
    work = str(tmp_path / "work")
    state = cleanup.analyze(str(backups), work, keep_ratio=0.5)
    f = state["files"]
    assert f["sms-20240302000000.xml"]["decision"] == "keep"  # incremental
    assert f["sms-20240301000000.xml"]["decision"] == "keep"  # newest full
    assert f["sms-20240201000000.xml"]["decision"] == "supersede"
    assert f["sms-20240201000000.xml"]["unique"] == 0
    assert f["sms-20240115000000.xml"]["decision"] == "keep" and f["sms-20240115000000.xml"]["error"]
    # C is in the truncated (kept) file, so the oldest full backup is fully covered
    assert f["sms-20240101000000.xml"]["decision"] == "supersede"
    assert f["sms-20240101000000.xml"]["unique"] == 0
    assert f["calls-20240201000000.xml"]["decision"] == "keep"
    assert f["calls-20240101000000.xml"]["decision"] == "supersede"
    assert f["calls-20240101000000.xml"]["unique"] == 1

    cleanup.apply(str(backups), work)
    left = sorted(os.listdir(backups))
    assert left == [
        "calls-20240201000000.xml",
        "notes.txt",
        "sms-20240115000000.xml",
        "sms-20240301000000.xml",
        "sms-20240302000000.xml",
    ]
    rec = os.path.join(work, "recovered", "calls-recovered.xml")
    with open(rec, "rb") as fh:
        assert [el.get("date") for el in iter_records(fh)] == ["1"]
    assert sorted(os.listdir(os.path.join(work, "superseded"))) == [
        "calls-20240101000000.xml",
        "sms-20240101000000.xml",
        "sms-20240201000000.xml",
    ]
    # idempotent
    cleanup.apply(str(backups), work)


def test_recovers_deleted_messages_and_roundtrips_emoji(backups, tmp_path):
    os.remove(backups / "sms-20240115000000.xml")
    work = str(tmp_path / "work")
    state = cleanup.analyze(str(backups), work)
    old = state["files"]["sms-20240101000000.xml"]
    assert old["decision"] == "supersede" and old["unique"] == 1  # message C
    cleanup.apply(str(backups), work)
    rec = os.path.join(work, "recovered", "sms-recovered.xml")
    with open(rec, "rb") as fh:
        bodies = [el.get("body") for el in iter_records(fh)]
    assert bodies == ["msg C"]


def test_resume_matches_single_run(backups, tmp_path):
    w1, w2 = str(tmp_path / "w1"), str(tmp_path / "w2")
    full = cleanup.analyze(str(backups), w1)
    for _ in range(10):
        st = cleanup.analyze(str(backups), w2, max_files=1)
    strip = lambda s: {n: (e["decision"], e["unique"]) for n, e in s["files"].items()}
    assert strip(full) == strip(st)


def test_spool_cap_second_pass(backups, tmp_path):
    os.remove(backups / "sms-20240115000000.xml")
    work = str(tmp_path / "work")
    cleanup.analyze(str(backups), work, extras_cap_mb=0)
    cleanup.apply(str(backups), work)
    with open(os.path.join(work, "recovered", "sms-recovered.xml"), "rb") as fh:
        assert [el.get("body") for el in iter_records(fh)] == ["msg C"]


def test_apply_refuses_if_kept_file_changed(backups, tmp_path):
    work = str(tmp_path / "work")
    cleanup.analyze(str(backups), work)
    with open(backups / "sms-20240301000000.xml", "a") as fh:
        fh.write(" ")
    with pytest.raises(SystemExit):
        cleanup.apply(str(backups), work)
    assert os.path.exists(backups / "sms-20240201000000.xml")


def test_big_mms_and_incremental_mixture(tmp_path):
    d = tmp_path / "b"
    d.mkdir()
    recs = [mms(10_000 + i, "p%d" % i, i) for i in range(50)]
    write(d, "sms-20250101000000.xml", smses(*recs[:40]))
    write(d, "sms-20250201000000.xml", smses(*recs[5:45]))  # phone deleted first 5
    write(d, "sms-20250301000000.xml", smses(*recs[45:]))  # incremental
    st = cleanup.analyze(str(d), str(tmp_path / "w"))
    f = st["files"]
    assert [f[n]["decision"] for n in sorted(f)] == ["supersede", "keep", "keep"]
    assert f["sms-20250101000000.xml"]["unique"] == 5
    cleanup.apply(str(d), str(tmp_path / "w"))


def test_recovered_file_uses_app_emoji_dialect(tmp_path):
    d = tmp_path / "b"
    d.mkdir()
    write(d, "sms-20250101000000.xml", smses(sms(1, EMOJI), sms(2, "x"), sms(3, "y")))
    write(d, "sms-20250201000000.xml", smses(sms(2, "x"), sms(3, "y"), sms(4, "z")))
    w = str(tmp_path / "w")
    cleanup.analyze(str(d), w)
    cleanup.apply(str(d), w)
    raw = open(os.path.join(w, "recovered", "sms-recovered.xml"), encoding="utf-8").read()
    assert 'body="Hi &#55357;&#56832; there"' in raw
    assert 'count="1"' in raw
