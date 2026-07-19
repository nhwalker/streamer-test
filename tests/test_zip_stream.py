"""
Unit tests for the stored-zip streamer (zip_stream.py).

This writer backs the streamed /archive response: the announced size must
equal the streamed size exactly (it becomes the HTTP Content-Length), and
the output must round-trip through a standard reader.  Entries are built
directly here — mapping staged segment files onto ZipEntry is
archive_export's job and is covered by its own tests.
"""
import io
import zipfile

from zip_stream import ZipEntry, write_zip_stream, zip_stream_size

_DATE_TIME = (2024, 1, 15, 10, 30, 0)


def _entries(tmp_path, files):
    entries = []
    for name, content in sorted(files.items()):
        p = tmp_path / name
        p.write_bytes(content)
        entries.append(ZipEntry(arcname=name, path=str(p),
                                size=len(content), date_time=_DATE_TIME))
    return entries


def _stream(entries):
    buf = io.BytesIO()
    write_zip_stream(entries, buf)
    return buf.getvalue()


class TestZipStream:

    def test_stream_size_matches_bytes_written(self, tmp_path):
        entries = _entries(tmp_path, {
            'stream-00000.mp4': b'a' * 1000,
            'stream-00001.mp4': b'b' * 17,
            'stream-00002.mp4': b'',
        })
        assert len(_stream(entries)) == zip_stream_size(entries)

    def test_roundtrip_through_zipfile(self, tmp_path):
        files = {'stream-00000.mp4': b'aaa', 'stream-00001.mp4': b'bbbb'}
        data = _stream(_entries(tmp_path, files))
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert set(zf.namelist()) == set(files)
            for name, content in files.items():
                assert zf.read(name) == content

    def test_entries_are_stored_not_deflated(self, tmp_path):
        """The intended payload is already-compressed data; STORED skips the
        pointless CPU burn and makes the zip size computable up front."""
        data = _stream(_entries(tmp_path, {'stream-00000.mp4': b'x' * 100}))
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert all(i.compress_type == zipfile.ZIP_STORED
                       for i in zf.infolist())

    def test_no_data_descriptors(self, tmp_path):
        """Flag bit 3 must be clear: STORED entries with data descriptors are
        rejected by streaming readers (e.g. Java's ZipInputStream)."""
        data = _stream(_entries(tmp_path, {'stream-00000.mp4': b'x' * 100}))
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert all(i.flag_bits & 0x0008 == 0 for i in zf.infolist())

    def test_empty_zip(self):
        assert zip_stream_size([]) == 22  # bare end-of-central-directory
        with zipfile.ZipFile(io.BytesIO(_stream([]))) as zf:
            assert zf.namelist() == []

    def test_size_does_not_read_file_data(self, tmp_path):
        """zip_stream_size is header arithmetic only — safe to call before
        sending headers even for huge archives."""
        entry = ZipEntry(arcname='gone.mp4',
                         path=str(tmp_path / 'does-not-exist.mp4'),
                         size=100, date_time=_DATE_TIME)
        assert zip_stream_size([entry]) > 0

    def test_short_file_is_zero_padded_to_declared_size(self, tmp_path):
        """A file smaller than its declared size streams zero-fill so the
        byte count (and HTTP framing) stays valid."""
        p = tmp_path / 'short.mp4'
        p.write_bytes(b'xy')
        entry = ZipEntry(arcname='short.mp4', path=str(p),
                         size=10, date_time=_DATE_TIME)
        data = _stream([entry])
        assert len(data) == zip_stream_size([entry])
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.read('short.mp4') == b'xy' + b'\0' * 8

    def test_pre_1980_mtime_is_clamped(self, tmp_path):
        """DOS timestamps can't represent pre-1980; the header clamps rather
        than raising like zipfile does."""
        p = tmp_path / 'old.mp4'
        p.write_bytes(b'x')
        entry = ZipEntry(arcname='old.mp4', path=str(p),
                         size=1, date_time=(1970, 1, 1, 0, 0, 0))
        with zipfile.ZipFile(io.BytesIO(_stream([entry]))) as zf:
            assert zf.infolist()[0].date_time[0] == 1980

    def test_zip64_roundtrip(self, tmp_path, monkeypatch):
        """Force the zip64 paths with a tiny threshold and verify a standard
        reader still round-trips the archive and the size stays exact."""
        import zip_stream
        monkeypatch.setattr(zip_stream, '_ZIP64_LIMIT', 100)
        entries = _entries(tmp_path, {'stream-00000.mp4': b'x' * 500,
                                      'stream-00001.mp4': b'y' * 30})
        data = _stream(entries)
        assert len(data) == zip_stream_size(entries)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.read('stream-00000.mp4') == b'x' * 500
            assert zf.read('stream-00001.mp4') == b'y' * 30


class TestZipStreamUnzipTool:
    """Belt-and-suspenders: validate with an independent reader when the
    `unzip` binary is available (it is in CI's ubuntu image)."""

    def test_unzip_t_accepts_stream(self, tmp_path):
        import shutil as _shutil
        import subprocess
        if _shutil.which('unzip') is None:
            import pytest
            pytest.skip('unzip binary not available')
        entries = _entries(tmp_path, {'stream-00000.mp4': b'q' * 4096})
        zpath = tmp_path / 'out.zip'
        with open(zpath, 'wb') as fh:
            write_zip_stream(entries, fh)
        r = subprocess.run(['unzip', '-t', str(zpath)], capture_output=True)
        assert r.returncode == 0, r.stdout + r.stderr
