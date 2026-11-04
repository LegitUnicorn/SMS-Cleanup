"""Record identity (dedupe keys) and serialization back to backup XML."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from typing import List

SEP = "\x1f"
_NON_DIGIT = re.compile(r"\D")
_HAS_ALPHA = re.compile(r"[A-Za-z]")


def norm_addr(addr: str) -> str:
    """Normalise a phone number so '+1 (555) 123-4567' == '5551234567'.

    Short codes and alphanumeric sender IDs are just lower-cased.
    """
    addr = (addr or "").strip()
    if not addr or addr.lower() in ("null", "insert-address-token"):
        return ""
    if _HAS_ALPHA.search(addr):
        return addr.lower()
    digits = _NON_DIGIT.sub("", addr)
    if len(digits) >= 7:
        return digits[-10:]
    return digits or addr.lower()


def _digest(fields: List[str]) -> bytes:
    h = hashlib.blake2b(digest_size=16)
    h.update(SEP.join(fields).encode("utf-8", "surrogatepass"))
    return h.digest()


def _mms_addrs(el: ET.Element) -> str:
    addrs = {norm_addr(a.get("address", "")) for a in el.iter("addr")}
    addrs.discard("")
    if not addrs:
        addrs = {norm_addr(x) for x in el.get("address", "").split("~")}
        addrs.discard("")
    return "|".join(sorted(addrs))


def _mms_content(el: ET.Element) -> str:
    parts = []
    for p in el.iter("part"):
        data = p.get("data")
        data_hash = (
            hashlib.blake2b(data.encode("ascii", "replace"), digest_size=16).hexdigest()
            if data
            else ""
        )
        parts.append(SEP.join((p.get("seq", ""), p.get("ct", ""), p.get("text", ""), data_hash)))
    parts.sort()
    return hashlib.blake2b("\x1e".join(parts).encode("utf-8", "surrogatepass"), digest_size=16).hexdigest()


def record_key(el: ET.Element) -> bytes:
    """Stable 16-byte identity for a record.

    Only fields that never change between backups of the same message are
    used: things like ``read``, ``contact_name``, ``readable_date``,
    ``sub_id`` or the on-phone ``_data`` path are deliberately ignored.
    Message *content* (body / attachments) IS part of the identity, so two
    copies that differ in content are both kept -- we never discard data on a
    guess.
    """
    a = el.attrib
    tag = el.tag
    if tag == "sms":
        return _digest(
            ["sms", a.get("date", ""), a.get("type", ""), norm_addr(a.get("address", "")), a.get("body", "")]
        )
    if tag == "mms":
        return _digest(
            [
                "mms",
                a.get("date", ""),
                a.get("msg_box", ""),
                a.get("m_id", ""),
                _mms_addrs(el),
                _mms_content(el),
            ]
        )
    if tag == "call":
        return _digest(
            [
                "call",
                norm_addr(a.get("number", "")),
                a.get("date", ""),
                a.get("duration", ""),
                a.get("type", ""),
            ]
        )
    # Unknown record type: fall back to every attribute + children.
    blob = ET.tostring(el, encoding="unicode")
    return _digest(["other", tag, blob])


# --------------------------------------------------------------------------
# Serialization (mirrors the backup app's own output format)
# --------------------------------------------------------------------------

_NON_BMP = re.compile("[\U00010000-\U0010FFFF]")


def _surrogate_ref(m: "re.Match[str]") -> str:
    cp = ord(m.group(0)) - 0x10000
    return "&#%d;&#%d;" % (0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF))


def escape_attr(s: str) -> str:
    if not s:
        return s
    s = (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\n", "&#10;")
        .replace("\r", "&#13;")
        .replace("\t", "&#9;")
    )
    # The app itself encodes emoji as surrogate-pair references; do the same
    # so the output is byte-for-byte the dialect the app restores from.
    return _NON_BMP.sub(_surrogate_ref, s)


def escape_text(s: str) -> str:
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _NON_BMP.sub(_surrogate_ref, s)


def serialize(el: ET.Element, indent: str = "  ") -> str:
    out: List[str] = []
    _ser(el, out, indent)
    return "".join(out)


def _ser(el: ET.Element, out: List[str], indent: str) -> None:
    attrs = "".join(' %s="%s"' % (k, escape_attr(v)) for k, v in el.attrib.items())
    children = list(el)
    text = el.text if el.text and el.text.strip() else ""
    if not children and not text:
        out.append("%s<%s%s />\n" % (indent, el.tag, attrs))
        return
    out.append("%s<%s%s>" % (indent, el.tag, attrs))
    if text:
        out.append(escape_text(text))
    if children:
        out.append("\n")
        for c in children:
            _ser(c, out, indent + "  ")
        out.append(indent)
    out.append("</%s>\n" % el.tag)


XML_DECL = "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n"
ROOT_FOR_GROUP = {"sms": "smses", "calls": "calls"}
