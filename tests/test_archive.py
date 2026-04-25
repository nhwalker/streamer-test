"""
Archive tests for the desktop-stream-service container.

The service container tees the incoming H.264 bitstream so that one branch
writes to rotating Matroska (.mkv) segments via splitmuxsink + matroskamux.
These tests confirm the archive branch works end-to-end without relying on
any external tooling (ffprobe etc.).
"""
import os
import time

import pytest


# Every Matroska/EBML file starts with the EBML element header
# 0x1A 0x45 0xDF 0xA3.  We check this rather than a full container parse
# so the test stays self-contained (no ffmpeg dependency).
EBML_MAGIC = b"\x1a\x45\xdf\xa3"


def _wait_for_first_segment(archive_dir, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        files = sorted(
            f for f in os.listdir(archive_dir)
            if f.startswith("stream-") and f.endswith(".mkv")
        )
        if files:
            return os.path.join(archive_dir, files[0])
        time.sleep(0.5)
    return None


class TestArchive:
    """Verifies SRT → h264parse → tee → matroskamux → splitmuxsink."""

    def test_first_segment_appears(self, streaming_container, archive_dir,
                                   _caster, _service):
        """
        At least one stream-NNNNN.mkv file shows up in the archive volume
        within a generous timeout of the service coming up.

        streaming_container is depended on to force the service (and caster)
        fixtures to initialise.  archive_dir is the host tmp path mounted
        into the service container as /archive.  _caster is requested so
        its logs can surface in failure output (helps debug "stream never
        arrived" vs "muxer never wrote").
        """
        first = _wait_for_first_segment(archive_dir, timeout=30.0)
        if first is None:
            service_out, service_err = _service.get_logs()
            caster_out, caster_err   = _caster.get_logs()
            listing = os.listdir(archive_dir) if os.path.isdir(archive_dir) else []
            pytest.fail(
                f"No stream-*.mkv appeared in {archive_dir} within 30 s.\n"
                f"Directory listing: {listing}\n"
                f"===== caster stdout =====\n{caster_out.decode(errors='replace')}\n"
                f"===== caster stderr =====\n{caster_err.decode(errors='replace')}\n"
                f"===== service stdout =====\n{service_out.decode(errors='replace')}\n"
                f"===== service stderr =====\n{service_err.decode(errors='replace')}"
            )

    def test_segment_is_valid_matroska(self, streaming_container, archive_dir,
                                       _service):
        """
        The first segment starts with the EBML magic 1A 45 DF A3.

        Matroska is streaming-by-default, so the file is readable as soon
        as the first cluster is flushed -- no need to wait for the segment
        boundary to rotate.
        """
        first = _wait_for_first_segment(archive_dir, timeout=30.0)
        assert first is not None, "expected at least one .mkv file"

        # Allow up to 10 s for the EBML header to land on disk.  In practice
        # matroskamux flushes the header within milliseconds of the first
        # buffer arriving, well before the first keyframe propagates through
        # the rest of the pipeline.
        deadline = time.monotonic() + 10.0
        header = b""
        while time.monotonic() < deadline:
            try:
                with open(first, "rb") as fh:
                    header = fh.read(4)
            except FileNotFoundError:
                header = b""
            if header == EBML_MAGIC:
                break
            time.sleep(0.5)

        if header != EBML_MAGIC:
            service_out, service_err = _service.get_logs()
            pytest.fail(
                f"Expected EBML magic {EBML_MAGIC.hex()} at the start of "
                f"{first}, got {header.hex()!r} ({len(header)} bytes).\n"
                f"===== service stdout =====\n{service_out.decode(errors='replace')}\n"
                f"===== service stderr =====\n{service_err.decode(errors='replace')}"
            )

    def test_segment_has_content(self, streaming_container, archive_dir):
        """
        The first segment grows past the EBML+SegmentInfo header size.
        A fresh matroskamux file that never got any real frame data is
        under ~1 KB (just EBML + SegmentInfo headers).  Real streamed
        content hits tens of KB within a second or two at 1280x720.
        """
        first = _wait_for_first_segment(archive_dir, timeout=30.0)
        assert first is not None, "expected at least one .mkv file"

        deadline = time.monotonic() + 15.0
        size = 0
        while time.monotonic() < deadline:
            size = os.path.getsize(first)
            if size > 10_000:
                break
            time.sleep(0.5)

        assert size > 10_000, (
            f"Archive segment {first} only {size} bytes after 15 s — "
            "looks like no real video frames were written."
        )
