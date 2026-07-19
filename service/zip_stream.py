"""
zip_stream.py -- deterministic stored-zip writer for streamed downloads.

Generic (nothing archive-specific lives here): callers describe the
members as ZipEntry tuples, ask zip_stream_size() for the exact byte
length, then write_zip_stream() the same entries to any object with a
.write method — typically an HTTP response socket, with the size sent
ahead as Content-Length.

Entries are STORED (no compression): the intended payload is data that
DEFLATE cannot shrink (H.264 video), and stored entries make the zip's
byte layout a pure function of the entry names and sizes, so the total
size is computable without reading a byte of file data.

The writer is hand-rolled rather than zipfile-based because zipfile's
unseekable-stream mode emits data descriptors (general-purpose flag
bit 3), and STORED-plus-descriptor zips are rejected by streaming
readers such as Java's ZipInputStream (a stored entry's length is
unknowable mid-stream).  Writing real sizes and CRCs into the local
headers instead costs one extra read pass per file (CRC first, then the
data), which is cheap next to the transfer itself.

zip64 is supported and kicks in per-field at 4 GiB / 65535 entries.
"""
import struct
import zlib

from typing import NamedTuple

_ZIP64_LIMIT = 0xFFFF_FFFF   # from this value on, a field moves to zip64


class ZipEntry(NamedTuple):
    arcname:   str
    path:      str
    size:      int
    date_time: tuple   # (Y, M, D, h, m, s) local time, for the zip headers


class _CentralEntry(NamedTuple):
    """What the central directory needs to record about a written member."""
    name:          bytes
    flags:         int
    crc:           int
    size:          int
    header_offset: int
    dtime:         int
    ddate:         int


def _dos_datetime(date_time):
    """(dos_time, dos_date) for a (Y, M, D, h, m, s) tuple; clamps pre-1980."""
    y, mo, d, h, mi, s = date_time[:6]
    if y < 1980:
        y, mo, d, h, mi, s = 1980, 1, 1, 0, 0, 0
    return (h << 11) | (mi << 5) | (s // 2), ((y - 1980) << 9) | (mo << 5) | d


def _entry_data(path, size):
    """Yield exactly `size` bytes of path, zero-filling if it comes up short.

    Callers stage immutable files, so a short read means something
    external mutated them; padding keeps the byte count — and with it the
    HTTP Content-Length framing — valid regardless.
    """
    remaining = size
    with open(path, 'rb') as fh:
        while remaining > 0:
            chunk = fh.read(min(1024 * 1024, remaining))
            if not chunk:
                yield bytes(remaining)
                return
            remaining -= len(chunk)
            yield chunk


def _file_crc(path, size):
    """CRC-32 of exactly the bytes _entry_data will stream for path."""
    crc = 0
    for chunk in _entry_data(path, size):
        crc = zlib.crc32(chunk, crc)
    return crc


def _write_zip(entries, out, include_data):
    """Write a stored zip of `entries` to out (any object with .write).

    With include_data=False no file is opened at all: headers are emitted
    and offsets advanced as if the data were written, which is how
    zip_stream_size computes the exact length without reading a byte.
    """
    def w(data):
        out.write(data)
        return len(data)

    offset  = 0
    central = []
    for e in entries:
        try:
            name  = e.arcname.encode('ascii')
            flags = 0
        except UnicodeEncodeError:
            name  = e.arcname.encode('utf-8')
            flags = 0x0800                      # name is UTF-8
        crc   = _file_crc(e.path, e.size) if include_data else 0
        zip64 = e.size >= _ZIP64_LIMIT
        dtime, ddate = _dos_datetime(e.date_time)
        size_marker  = 0xFFFFFFFF if zip64 else e.size
        extra = (struct.pack('<HHQQ', 0x0001, 16, e.size, e.size)
                 if zip64 else b'')
        header_offset = offset
        offset += w(struct.pack(
            '<IHHHHHIIIHH', 0x04034B50, 45 if zip64 else 20, flags, 0,
            dtime, ddate, crc, size_marker, size_marker,
            len(name), len(extra)))
        offset += w(name)
        offset += w(extra)
        if include_data:
            for chunk in _entry_data(e.path, e.size):
                offset += w(chunk)
        else:
            offset += e.size
        central.append(_CentralEntry(name, flags, crc, e.size,
                                     header_offset, dtime, ddate))

    cd_start  = offset
    n_entries = len(central)
    for c in central:
        extra_fields = []
        size_marker  = c.size
        if c.size >= _ZIP64_LIMIT:
            extra_fields += [c.size, c.size]    # uncompressed, compressed
            size_marker   = 0xFFFFFFFF
        off_marker = c.header_offset
        if c.header_offset >= _ZIP64_LIMIT:
            extra_fields.append(c.header_offset)
            off_marker = 0xFFFFFFFF
        extra = (struct.pack('<HH' + 'Q' * len(extra_fields),
                             0x0001, 8 * len(extra_fields), *extra_fields)
                 if extra_fields else b'')
        version = 45 if extra_fields else 20
        offset += w(struct.pack(
            '<IHHHHHHIIIHHHHHII', 0x02014B50, version, version, c.flags, 0,
            c.dtime, c.ddate, c.crc, size_marker, size_marker,
            len(c.name), len(extra), 0, 0, 0, 0, off_marker))
        offset += w(c.name)
        offset += w(extra)
    cd_size = offset - cd_start

    need_zip64_eocd = (n_entries >= 0xFFFF or cd_size >= _ZIP64_LIMIT
                       or cd_start >= _ZIP64_LIMIT)
    eocd_entries = 0xFFFF     if need_zip64_eocd else n_entries
    eocd_cd_size = 0xFFFFFFFF if need_zip64_eocd else cd_size
    eocd_cd_off  = 0xFFFFFFFF if need_zip64_eocd else cd_start
    if need_zip64_eocd:
        offset += w(struct.pack(
            '<IQHHIIQQQQ', 0x06064B50, 44, 45, 45, 0, 0,
            n_entries, n_entries, cd_size, cd_start))
        offset += w(struct.pack('<IIQI', 0x07064B50, 0,
                                cd_start + cd_size, 1))
    offset += w(struct.pack(
        '<IHHHHIIH', 0x06054B50, 0, 0, eocd_entries, eocd_entries,
        eocd_cd_size, eocd_cd_off, 0))
    return offset


class _NullWriter:
    def write(self, data):
        pass


def zip_stream_size(entries):
    """Exact byte length write_zip_stream will produce for `entries`.

    Costs only header arithmetic — no file is opened or read.  (The size
    comes from _write_zip's offset accounting, which advances past the
    file data even when none is written.)
    """
    return _write_zip(entries, _NullWriter(), include_data=False)


def write_zip_stream(entries, out):
    """Stream a stored zip of `entries` to out (any object with .write)."""
    _write_zip(entries, out, include_data=True)
