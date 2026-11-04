"""Streaming reader for SMS Backup & Restore style XML files.

Backup files can be tens of GB each (MMS attachments are inlined as base64),
so everything here works on a bounded-size buffer and never builds the full
tree in memory.

The backup app writes characters outside the Basic Multilingual Plane (most
emoji) as *pairs of UTF-16 surrogate character references*, e.g.
``&#55357;&#56832;`` for U+1F600.  That is not well-formed XML and makes every
standard parser (expat / ElementTree / lxml) abort.  ``RefFixer`` rewrites
those pairs into a single valid reference (``&#128512;``) on the fly, and
replaces lone surrogates / forbidden control characters with U+FFFD so a single
odd message cannot stop a multi-GB scan.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from typing import BinaryIO, Callable, Iterator, Optional

_REF = re.compile(rb"&#(?:[xX]([0-9A-Fa-f]{1,8})|([0-9]{1,10}));")
_TRAILING_REF = re.compile(rb"&#(?:[xX][0-9A-Fa-f]{1,8}|[0-9]{1,10});\Z")
_REPLACEMENT = b"&#65533;"
# Raw C0 control bytes that are illegal in XML 1.0. They never occur inside a
# multi-byte UTF-8 sequence (continuation bytes are >= 0x80), so a byte-level
# translate is safe.
_CTRL_TABLE = bytes(
    (0x3F if (b < 0x20 and b not in (0x09, 0x0A, 0x0D)) else b) for b in range(256)
)
_HOLDBACK = 64


def _ref_value(m: "re.Match[bytes]") -> int:
    hex_part, dec_part = m.group(1), m.group(2)
    return int(hex_part, 16) if hex_part is not None else int(dec_part)


def _is_xml_char(cp: int) -> bool:
    return (
        cp in (0x9, 0xA, 0xD)
        or 0x20 <= cp <= 0xD7FF
        or 0xE000 <= cp <= 0xFFFD
        or 0x10000 <= cp <= 0x10FFFF
    )


def fix_refs(data: bytes) -> bytes:
    """Repair character references in a complete buffer (see module doc)."""
    data = data.translate(_CTRL_TABLE)
    if b"&#" not in data:
        return data
    out = []
    pos = 0
    search = _REF.search
    match = _REF.match
    while True:
        m = search(data, pos)
        if m is None:
            break
        cp = _ref_value(m)
        if 0xD800 <= cp <= 0xDBFF:
            m2 = match(data, m.end())
            if m2 is not None:
                cp2 = _ref_value(m2)
                if 0xDC00 <= cp2 <= 0xDFFF:
                    combined = 0x10000 + ((cp - 0xD800) << 10) + (cp2 - 0xDC00)
                    out.append(data[pos : m.start()])
                    out.append(b"&#%d;" % combined)
                    pos = m2.end()
                    continue
            out.append(data[pos : m.start()])
            out.append(_REPLACEMENT)
            pos = m.end()
        elif not _is_xml_char(cp):
            out.append(data[pos : m.start()])
            out.append(_REPLACEMENT)
            pos = m.end()
        else:
            out.append(data[pos : m.end()])
            pos = m.end()
    if pos == 0:
        return data
    out.append(data[pos:])
    return b"".join(out)


class RefFixer:
    """Chunk-boundary-safe wrapper around :func:`fix_refs`.

    Holds back any trailing bytes that could be the start of a reference (or
    a complete high-surrogate reference whose partner is in the next chunk).
    """

    def __init__(self) -> None:
        self._tail = b""

    def feed(self, chunk: bytes, final: bool = False) -> bytes:
        buf = self._tail + chunk if self._tail else chunk
        if final:
            self._tail = b""
            return fix_refs(buf)
        cut = len(buf)
        window = max(0, cut - _HOLDBACK)
        amp = buf.rfind(b"&", window)
        if amp != -1:
            cut = amp
        m = _TRAILING_REF.search(buf, max(0, cut - _HOLDBACK), cut)
        if m is not None:
            cut = m.start()
        self._tail = buf[cut:]
        return fix_refs(buf[:cut])


class ParseResult:
    """Filled in by :func:`iter_records` while/after iterating."""

    def __init__(self) -> None:
        self.root_tag: Optional[str] = None
        self.root_attrib: dict = {}
        self.bytes_read = 0
        self.records = 0


ProgressFn = Callable[[int, int], None]


def iter_records(
    fileobj: BinaryIO,
    result: Optional[ParseResult] = None,
    chunk_size: int = 16 << 20,
    progress: Optional[ProgressFn] = None,
    progress_every: float = 15.0,
) -> Iterator[ET.Element]:
    """Yield each top-level record element (``sms``, ``mms``, ``call`` ...).

    The yielded element is detached from the tree right after the consumer
    is done with it, so memory stays bounded.  Raises
    ``xml.etree.ElementTree.ParseError`` on malformed / truncated input after
    yielding every record that could be read.
    """
    if result is None:
        result = ParseResult()
    parser = ET.XMLPullParser(events=("start", "end"))
    fixer = RefFixer()
    depth = 0
    root = None
    last_report = time.monotonic()
    while True:
        chunk = fileobj.read(chunk_size)
        final = not chunk
        result.bytes_read += len(chunk)
        data = fixer.feed(chunk, final=final)
        if data:
            parser.feed(data)
        if final:
            parser.close()
        for event, el in parser.read_events():
            if event == "start":
                depth += 1
                if depth == 1:
                    root = el
                    result.root_tag = el.tag
                    result.root_attrib = dict(el.attrib)
            else:
                if depth == 2:
                    result.records += 1
                    yield el
                    root.remove(el)  # type: ignore[union-attr]
                depth -= 1
        if progress is not None:
            now = time.monotonic()
            if now - last_report >= progress_every:
                last_report = now
                progress(result.bytes_read, result.records)
        if final:
            break
