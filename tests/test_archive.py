"""
Archive tests for the desktop-stream-service container.

The service's ffmpeg process writes rotating fragmented-MP4 segments in
/archive-live (segment muxer, movflags empty_moov so the moov atom sits at
the front of the in-progress file).  When a segment rotates, the finalize
watcher renames/moves it into /archive under its timestamped name — no
re-encoding, no remux step.

These tests confirm both halves of that flow without relying on ffprobe.
"""
import os
import time

import pytest


# Every MP4/ISO BMFF file has the bytes 'ftyp' at offset 4 (start of the
# first box header).  Checking this is enough to confirm the file is an
# actual MP4 container, with no external tooling.
MP4_FTYP = b"ftyp"


class TestArchive:
    """Verifies ffmpeg segment output → finalize watcher → /archive."""

    def test_first_segment_appears(self, first_segment):
        """
        At least one stream-NNNNN.mp4 file shows up in the live archive
        volume within a generous timeout of the service coming up.

        The first_segment fixture waits and surfaces container logs on failure.
        """
        assert os.path.isfile(first_segment)

    def test_segment_is_valid_mp4(self, first_segment, _service):
        """
        The live (in-progress) segment starts with `ftyp` at offset 4.

        ffmpeg's segment muxer with movflags empty_moov writes
        `ftyp + moov + moof+mdat ...` once the first encoded frame
        reaches the muxer.  The deadline here is generous on purpose —
        it's bounded by encoder startup under CI contention, not by the
        muxer: once the first frame arrives, the ftyp+moov pair is on
        disk in milliseconds.
        """
        first = first_segment

        deadline = time.monotonic() + 30.0
        header = b""
        while time.monotonic() < deadline:
            try:
                with open(first, "rb") as fh:
                    header = fh.read(12)
            except FileNotFoundError:
                header = b""
            if len(header) >= 8 and header[4:8] == MP4_FTYP:
                break
            time.sleep(0.5)

        if not (len(header) >= 8 and header[4:8] == MP4_FTYP):
            service_out, service_err = _service.get_logs()
            pytest.fail(
                f"Expected 'ftyp' at offset 4 of {first}, got "
                f"{header!r} ({len(header)} bytes).\n"
                f"===== service stdout =====\n{service_out.decode(errors='replace')}\n"
                f"===== service stderr =====\n{service_err.decode(errors='replace')}"
            )

    def test_segment_has_content(self, first_segment):
        """
        The first segment grows past the ftyp+moov prelude size.
        A fresh fragmented-MP4 file that never got any real frame data
        is around 1 KB (just ftyp + moov boxes).  Real streamed content
        hits tens of KB once the first fragment's worth of frames has
        been muxed.

        The 30 s deadline matches the magic-bytes test — the bottleneck
        is encoder startup under CI contention, not muxer fragment
        cadence.  Once the first fragment lands, subsequent ones come
        at ~30 frames/s.
        """
        first = first_segment

        deadline = time.monotonic() + 30.0
        size = 0
        while time.monotonic() < deadline:
            size = os.path.getsize(first)
            if size > 10_000:
                break
            time.sleep(0.5)

        assert size > 10_000, (
            f"Archive segment {first} only {size} bytes after 30 s — "
            "looks like no real video frames were written."
        )

    def test_completed_segment_is_valid_mp4(self, first_segment, archive_dir,
                                            _service):
        """
        After the first fragment rotation, a completed *_to_*.mp4 lands
        in archive_dir and starts with `ftyp` at offset 4 (MP4 magic).
        Because the live container is already fragmented MP4, the
        rollover is a pure rename/move — the same bytes that were live
        in /archive-live are now in /archive under a timestamped name.

        We bypass ffprobe / mp4info here for self-containment; the `ftyp`
        check is sufficient evidence the file is a real MP4 container.
        """
        # Rotation happens once per ARCHIVE_SEGMENT_SEC, plus the
        # finalize worker handles the rename/move.  Generous deadline.
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
