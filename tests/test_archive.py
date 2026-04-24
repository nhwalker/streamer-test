"""
Archive tests for the desktop-stream-service container.

The service container tees the incoming H.264 bitstream so that one branch
writes to rotating MP4 segments via splitmuxsink + mp4mux.  These tests
confirm the archive branch works end-to-end without relying on any
external tooling (ffprobe etc.).
"""
import os
import time

import pytest


# MP4 files begin with a size-prefixed "ftyp" box.  The second 4 bytes of
# any valid MP4 are the ASCII letters 'ftyp'.  We check this rather than a
# full container parse to keep the test self-contained (no ffmpeg needed).
MP4_FTYP_MAGIC = b"ftyp"


def _wait_for_first_segment(archive_dir, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        files = sorted(
            f for f in os.listdir(archive_dir)
            if f.startswith("stream-") and f.endswith(".mp4")
        )
        if files:
            return os.path.join(archive_dir, files[0])
        time.sleep(0.5)
    return None


class TestArchive:
    """Verifies that SRT → h264parse → tee → mp4mux → splitmuxsink works."""

    def test_first_segment_appears(self, streaming_container, archive_dir,
                                   _service):
        """
        At least one stream-NNNNN.mp4 file shows up in the archive volume
        within a generous timeout of the service coming up.

        streaming_container is depended on to force the service (and caster)
        fixtures to initialise.  archive_dir is the host tmp path mounted
        into the service container as /archive.
        """
        first = _wait_for_first_segment(archive_dir, timeout=30.0)
        if first is None:
            stdout, stderr = _service.get_logs()
            listing = os.listdir(archive_dir) if os.path.isdir(archive_dir) else []
            pytest.fail(
                f"No stream-*.mp4 appeared in {archive_dir} within 30 s.\n"
                f"Directory listing: {listing}\n"
                f"Service stdout:\n{stdout.decode(errors='replace')}\n"
                f"Service stderr:\n{stderr.decode(errors='replace')}"
            )

    def test_segment_is_valid_mp4(self, streaming_container, archive_dir):
        """
        The first segment contains a valid MP4 'ftyp' atom at bytes 4–8.

        A single streamed keyframe + header is enough for splitmuxsink to
        emit a well-formed file, so this runs well inside the session
        timeout even though production segments are 10 min.
        """
        first = _wait_for_first_segment(archive_dir, timeout=30.0)
        assert first is not None, "expected at least one .mp4 file"

        # splitmuxsink writes the file progressively; wait briefly for the
        # ftyp/moov to be flushed to disk before reading.
        deadline = time.monotonic() + 10.0
        header = b""
        while time.monotonic() < deadline:
            try:
                with open(first, "rb") as fh:
                    header = fh.read(12)
            except FileNotFoundError:
                header = b""
            if len(header) >= 8 and MP4_FTYP_MAGIC in header:
                break
            time.sleep(0.5)

        assert MP4_FTYP_MAGIC in header, (
            f"Expected MP4 'ftyp' magic in first 12 bytes of {first}, "
            f"got {header!r}"
        )

    def test_segment_has_content(self, streaming_container, archive_dir):
        """
        The first segment grows past the empty-header size.  A fresh
        splitmuxsink MP4 that never got any real frame data is under ~1 KB
        (just ftyp + placeholder).  Real streamed content hits tens of KB
        within a second or two at 1280x720.
        """
        first = _wait_for_first_segment(archive_dir, timeout=30.0)
        assert first is not None, "expected at least one .mp4 file"

        # Give splitmuxsink a few seconds to accumulate real frames.
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
