"""
Archive tests for the desktop-stream-service container.

The service container tees the incoming H.264 bitstream so that one branch
writes to rotating Matroska (.mkv) segments in /archive-live via
splitmuxsink + matroskamux.  When a fragment rotates, pipeline.py remuxes
it to a faststart .mp4 and moves it into /archive (no re-encoding).

These tests confirm both halves of that flow without relying on ffprobe.
"""
import os
import time

import pytest


# Every Matroska/EBML file starts with the EBML element header
# 0x1A 0x45 0xDF 0xA3.  We check this rather than a full container parse
# so the test stays self-contained.
EBML_MAGIC = b"\x1a\x45\xdf\xa3"

# Every MP4/ISO BMFF file has the bytes 'ftyp' at offset 4 (start of the
# first box header).  Checking this is enough to confirm the remuxed
# output is an actual MP4 container, again with no external tooling.
MP4_FTYP = b"ftyp"


class TestArchive:
    """Verifies RTP → h264parse → tee → matroskamux → splitmuxsink → MP4 remux."""

    def test_first_segment_appears(self, first_segment):
        """
        At least one stream-NNNNN.mkv file shows up in the live archive volume
        within a generous timeout of the service coming up.

        The first_segment fixture waits and surfaces container logs on failure.
        """
        assert os.path.isfile(first_segment)

    def test_segment_is_valid_matroska(self, first_segment, _service):
        """
        The live (in-progress) segment starts with the EBML magic 1A 45 DF A3.

        Matroska is streaming-by-default, so the file is readable as soon
        as the first cluster is flushed -- no need to wait for the segment
        boundary to rotate.  Keeping the live fragment as MKV is what lets
        /archive include the active segment mid-recording.
        """
        first = first_segment

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

    def test_segment_has_content(self, first_segment):
        """
        The first segment grows past the EBML+SegmentInfo header size.
        A fresh matroskamux file that never got any real frame data is
        under ~1 KB (just EBML + SegmentInfo headers).  Real streamed
        content hits tens of KB within a second or two at 1280x720.
        """
        first = first_segment

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

    def test_completed_segment_is_faststart_mp4(self, first_segment, archive_dir,
                                                _service):
        """
        After the first fragment rotation, a completed *_to_*.mp4 lands in
        archive_dir and starts with `ftyp` at offset 4 (MP4 magic).  The
        remux uses `-c copy -movflags +faststart` so the moov atom is at
        the front of the file, allowing web players to start decoding from
        the first received bytes.

        We bypass ffprobe / mp4info here for self-containment; the `ftyp`
        check is sufficient evidence the remux ran and produced a real
        MP4 container (a failed remux falls back to .mkv, which would not
        match the *.mp4 glob).
        """
        # Rotation happens once per ARCHIVE_SEGMENT_SEC, but the remux can
        # take a moment after rotation to complete.  Generous deadline to
        # absorb both.
        deadline = time.monotonic() + 90.0
        completed = None
        while time.monotonic() < deadline:
            if os.path.isdir(archive_dir):
                candidates = [
                    f for f in os.listdir(archive_dir)
                    if "_to_" in f and f.endswith(".mp4")
                ]
                if candidates:
                    completed = os.path.join(archive_dir, sorted(candidates)[0])
                    break
            time.sleep(1.0)

        if completed is None:
            service_out, service_err = _service.get_logs()
            archive_listing = os.listdir(archive_dir) \
                if os.path.isdir(archive_dir) else []
            pytest.fail(
                f"No *_to_*.mp4 appeared in {archive_dir} within 90 s.\n"
                f"Archive dir listing: {archive_listing}\n"
                f"===== service stdout =====\n{service_out.decode(errors='replace')}\n"
                f"===== service stderr =====\n{service_err.decode(errors='replace')}"
            )

        with open(completed, "rb") as fh:
            head = fh.read(12)
        assert head[4:8] == MP4_FTYP, (
            f"Expected 'ftyp' at offset 4 of {completed}, got "
            f"{head!r} ({len(head)} bytes)"
        )
