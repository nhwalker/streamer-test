"""
fmp4.py -- minimal ISO-BMFF box walking for mid-write fragmented MP4.

The archive pipeline writes fragmented MP4 (`movflags
+frag_keyframe+empty_moov`, per-packet flushing), so an in-progress file
is a sequence of complete top-level boxes (ftyp, moov, moof/mdat pairs)
possibly ending in a partially-written box.  These helpers answer the
two questions a reader of such a file needs:

  has_complete_moov(head)          is there video metadata yet at all?
  truncate_to_complete_boxes(path) trim a copied snapshot so ffmpeg
                                   never sees a mid-fragment cut.

Only top-level box headers are parsed (4-byte big-endian size + 4-byte
type); nothing here descends into box contents.
"""
import os


def has_complete_moov(head):
    """True when a complete top-level moov box lies within `head`.

    The archive writes moov immediately after ftyp (movflags empty_moov),
    so scanning the first few KB is sufficient.
    """
    off = 0
    while off + 8 <= len(head):
        size = int.from_bytes(head[off:off + 4], 'big')
        if size < 8:
            return False
        if head[off + 4:off + 8] == b'moov':
            return off + size <= len(head)
        off += size
    return False


def truncate_to_complete_boxes(path):
    """Trim an fMP4 copy to the last complete moof+mdat pair; return True
    when at least one complete movie fragment survived.

    A file copied mid-write can end inside a moof/mdat pair.  Browsers
    ignore a truncated trailing fragment, but ffmpeg rejects both a
    mid-box cut and a complete moof whose mdat is missing (the moof
    references sample data that isn't there).  So the walk keeps whole
    top-level boxes only and then also drops a trailing moof left
    without its mdat.

    Unusual headers (size 0 = "to EOF", size 1 = 64-bit extended) stop
    the walk conservatively: everything from that box on is dropped.
    """
    size = os.path.getsize(path)
    boxes = []          # (type, end_offset) of each complete top-level box
    with open(path, 'rb') as fh:
        off = 0
        while off + 8 <= size:
            fh.seek(off)
            hdr = fh.read(8)
            if len(hdr) < 8:
                break
            box_size = int.from_bytes(hdr[:4], 'big')
            if box_size < 8 or off + box_size > size:
                break
            off += box_size
            boxes.append((hdr[4:8], off))
    while boxes and boxes[-1][0] == b'moof':
        boxes.pop()
    keep = boxes[-1][1] if boxes else 0
    if keep and keep < size:
        os.truncate(path, keep)
    return any(t == b'moof' for t, _ in boxes)
