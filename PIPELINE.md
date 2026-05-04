# GStreamer Pipeline Documentation

This document describes every element in the GStreamer pipelines used by the
caster and service containers, and traces the full WebRTC signalling and media
flow from X11 screen capture to a viewer's browser.  It also covers the HTTP
web server that serves the archive download and video-assembly endpoints.

---

## End-to-End Architecture

```
┌──────────────────────────────────────────────────────────┐
│  CASTER CONTAINER                                        │
│                                                          │
│  ximagesrc → videorate → videoscale → videoconvert       │
│                                    → webrtcsink (H.264)  │
│                                           │              │
│  gst-webrtc-signalling-server (:8443) ◄───┘              │
└──────────────────────────────────────────────────────────┘
                        │ WebRTC (SRTP/RTP + WebSocket)
                        ▼
┌──────────────────────────────────────────────────────────┐
│  SERVICE CONTAINER                                       │
│                                                          │
│  webrtcsrc → videoconvert → tee                          │
│                              ├─ q_arch → encoder         │
│                              │         → h264parse       │
│                              │         → splitmuxsink    │
│                              │   (live .mkv → .mp4 on    │
│                              │    rotation, faststart)   │
│                              └─ q_webrtc → tee_webrtc   │
│                                           ├─ q_full      │
│                                           │  → webrtcsink (full,   :8443) │
│                                           ├─ q_top       │
│                                           │  → videocrop → webrtcsink (top,    :8444) │
│                                           └─ q_bot       │
│                                              → videocrop → webrtcsink (bottom, :8445) │
│                                                          │
│  gst-webrtc-signalling-server ×3 (:8443 / :8444 / :8445) │
│  HTTP web server (:8080)                                 │
└──────────────────────────────────────────────────────────┘
                        │ WebRTC (SRTP/RTP + WebSocket)
                        ▼
              Browser (RTCPeerConnection + <video>)
```

---

## WebRTC Signalling and Connection Sequence

The diagram below covers two independent connection phases: the service
connecting to the caster as a consumer, and a browser connecting to the service
as a viewer.

```mermaid
sequenceDiagram
    participant X11 as X11 Display
    participant CastPipe as Caster Pipeline<br/>(webrtcsink)
    participant CastSig as Caster Signalling<br/>Server :8443
    participant SvcPipe as Service Pipeline<br/>(webrtcsrc)
    participant SvcSig as Service Signalling<br/>Server :8443–8445
    participant Browser

    note over CastPipe,CastSig: Phase 1 – Caster startup
    CastPipe->>CastSig: WebSocket connect
    CastSig-->>CastPipe: Assign producer peer-id ("desktop-caster")
    CastPipe->>CastSig: Register as producer

    note over SvcPipe,CastSig: Phase 2 – Service ingests caster stream
    SvcPipe->>CastSig: WebSocket connect (request producer "desktop-caster")
    CastSig-->>CastPipe: Notify: new consumer arrived
    CastPipe->>CastSig: SDP Offer (H.264 codec + RTP params)
    CastSig-->>SvcPipe: Forward SDP Offer
    SvcPipe->>CastSig: SDP Answer (accept codec)
    CastSig-->>CastPipe: Forward SDP Answer
    CastPipe->>CastSig: ICE candidates (network addresses)
    CastSig-->>SvcPipe: Forward ICE candidates
    SvcPipe->>CastSig: ICE candidates
    CastSig-->>CastPipe: Forward ICE candidates
    note over CastPipe,SvcPipe: DTLS handshake → SRTP keys exchanged
    X11-->>CastPipe: Raw video frames
    CastPipe-->>SvcPipe: H.264 SRTP/RTP stream (UDP)

    note over SvcPipe,SvcSig: Phase 3 – Service registers browser-facing streams
    SvcPipe->>SvcSig: webrtcsink (full)   registers on :8443
    SvcPipe->>SvcSig: webrtcsink (top)    registers on :8444
    SvcPipe->>SvcSig: webrtcsink (bottom) registers on :8445

    note over SvcSig,Browser: Phase 4 – Browser viewer connects
    Browser->>SvcSig: WebSocket connect (picks :8443, :8444, or :8445)
    SvcSig-->>Browser: Assign peer-id
    SvcSig-->>Browser: SDP Offer (VP9 or H.264 options)
    Browser->>SvcSig: SDP Answer (selects codec)
    SvcSig-->>Browser: ICE candidates
    Browser->>SvcSig: ICE candidates
    note over SvcPipe,Browser: DTLS handshake → SRTP keys exchanged
    SvcPipe-->>Browser: Encoded video SRTP/RTP stream (UDP)
    Browser->>Browser: RTCPeerConnection decodes → <video> element
    Browser->>SvcSig: RTCP REMB (bandwidth estimate)
    SvcSig-->>Browser: Forward RTCP feedback
    note over SvcPipe: webrtcsink adjusts encoder bitrate per-browser
```

---

## Caster Pipeline

**Pipeline string** (`caster/pipeline.py`, `main()`):
```
ximagesrc display-name=:0 use-damage=false
  ! videorate
  ! video/x-raw,framerate=30/1
  ! videoscale
  ! video/x-raw,width=1920,height=1080
  ! videoconvert
  ! webrtcsink name=ws video-caps="video/x-h264"
```

### `ximagesrc`

| Property | Value | Purpose |
|---|---|---|
| `display-name` | `:0` | X11 display to capture |
| `use-damage=false` | disabled | Forces full-frame capture on every tick |

Captures raw video frames from the X11 display server. `use-damage=false`
disables the X11 Damage extension, which normally restricts capture to only the
changed screen regions. Disabling it ensures a complete frame is delivered every
tick regardless of X11 damage events, which is required for a stable video
stream.

### `videorate`

Drops or duplicates frames to produce a perfectly constant frame rate. Without
this element, `ximagesrc` output is jitter-prone — frames arrive slightly early
or late depending on system load. `videorate` absorbs that jitter and hands a
steady cadence to the downstream caps filter, preventing encoder stalls and
keeping A/V sync stable.

### Caps filter: `video/x-raw,framerate=30/1`

Locks the frame rate to exactly 30 fps (controlled by `STREAM_FRAMERATE`).
GStreamer uses this caps negotiation step to inform `videorate` of the target
rate. Without an explicit caps constraint here, the downstream elements would
inherit whatever rate `ximagesrc` happens to produce, making the stream
non-deterministic.

### `videoscale`

Resamples the captured frames to the requested output resolution. On a desktop
with a non-standard DPI or multi-monitor setup the raw capture size may not
match the desired stream dimensions. `videoscale` applies a high-quality
bilinear resampler to produce exactly the requested size.

### Caps filter: `video/x-raw,width=1920,height=1080`

Enforces the output resolution (controlled by `STREAM_WIDTH` / `STREAM_HEIGHT`).
Like the framerate caps filter, this is the negotiation contract between
`videoscale` and the next element. GStreamer will reject the pipeline at
link-time if the upstream element cannot produce the specified dimensions,
catching misconfiguration before any frames are processed.

### `videoconvert`

Converts pixel formats between elements whose capabilities do not match. X11
captures in BGR or BGRA; the H.264 encoder expects I420 (YUV planar). Without
`videoconvert` in the chain, caps negotiation would fail when `webrtcsink` tries
to find a common format with `ximagesrc`. Placing `videoconvert` immediately
before `webrtcsink` allows the encoder inside `webrtcsink` to request whichever
input format it prefers and have it satisfied automatically.

### `webrtcsink` (name: `ws`)

| Property | Value | Purpose |
|---|---|---|
| `video-caps` | `video/x-h264` | Restrict codec negotiation to H.264 only |
| `signaller.uri` | `ws://127.0.0.1:8443` | Address of the local signalling server |
| `stun-server` | `$GST_WEBRTC_STUN_SERVER` | Optional STUN for NAT traversal |

`webrtcsink` is a high-level element from `gst-plugins-rs` that bundles an
encoder, an RTP payloader, a `webrtcbin` instance, and a signalling client into
one unit. It:

1. Registers itself as a **producer** on the signalling server.
2. For each consumer that connects, performs an SDP offer/answer exchange and
   ICE candidate exchange through the signalling server.
3. Spins up an independent `webrtcbin` per consumer so bitrate adaptation is
   fully isolated between viewers.
4. Encodes raw video using `nvh264enc` when an NVIDIA GPU is available, falling
   back to `x264enc` otherwise — this selection is made automatically at
   negotiation time.
5. Handles RTCP receiver reports and REMB feedback to adapt the encoder bitrate
   up or down based on each consumer's measured bandwidth.
6. Encrypts the RTP payload with DTLS-SRTP before sending over UDP.

`video-caps="video/x-h264"` is set on the caster because the service's
`webrtcsrc` decodes the stream and re-encodes it for archive and browser
delivery. H.264 decoding is universally hardware-accelerated, making it the
most efficient codec for the internal caster→service leg.

TURN server configuration is applied per `webrtcbin` via the `deep-element-added`
signal (`caster/pipeline.py`, `on_deep_element_added`) because `webrtcsink`
creates a new `webrtcbin` for each peer and TURN credentials must be injected
after creation.

---

## Service Pipeline

The service pipeline is constructed programmatically (`service/pipeline.py`,
`main()`).

### `webrtcsrc` (name: `wsrc`)

| Property | Value | Purpose |
|---|---|---|
| `signaller.uri` | `ws://$CASTER_HOST:8443` | Caster's signalling server address |
| `signaller.producer-peer-id` | `desktop-caster` | Which producer to consume |

`webrtcsrc` is the consumer counterpart to `webrtcsink`, also from
`gst-plugins-rs`. It:

1. Connects to the caster's signalling server as a **consumer**.
2. Requests the specific producer peer identified by `CASTER_PEER_ID`.
3. Participates in the SDP offer/answer and ICE candidate exchange.
4. Completes the DTLS handshake to establish an SRTP session.
5. Receives the SRTP-protected RTP stream and decrypts it.
6. Decodes the H.264 stream to raw YUV frames.
7. Exposes the decoded video on a **dynamically created** `src` pad.

Because `webrtcsrc` does not know the stream's properties at construction time,
the `src` pad is only added once the remote SDP is processed. The `pad-added`
signal handler (`on_pad_added`) listens for this event and links the first video
pad to `videoconvert`'s sink pad.

### `videoconvert` (name: `vconvert`)

Normalises the pixel format coming out of `webrtcsrc`. The decoded frames may
be in NV12 or I420 depending on which decoder was used (NVDEC vs. software).
`videoconvert` ensures all downstream elements always see a consistent format
regardless of the decoder path, preventing caps negotiation failures further
down the pipeline.

### `tee` (name: `t`)

Duplicates the single decoded video stream into two independent branches: the
**archive branch** and the **browser WebRTC branch**. `tee` pushes each buffer
to all downstream sink pads in turn, so both branches receive every frame.
Using `tee` instead of duplicating the `webrtcsrc` element means the network
and decoding work is done exactly once for both consumers.

---

### Archive Branch

#### `queue` (name: `q_arch`)

Decouples the `tee` from the encoder.  Configured with explicit bounds and
`leaky=downstream` so the WebRTC branch is isolated from a slow archive
encoder:

| Property | Value | Purpose |
|---|---|---|
| `max-size-buffers` | `0` | Use byte/time gating only |
| `max-size-bytes`   | `ARCHIVE_QUEUE_MAX_BYTES` (default 512 MB) | Bound memory; raw 1080p30 is ~93 MB/s so 512 MB ≈ 5.5 s of headroom |
| `max-size-time`    | `ARCHIVE_QUEUE_MAX_SEC × Gst.SECOND` (default 5 s) | Bound time-domain buffering |
| `leaky`            | `2` (downstream) | When full, drop the oldest buffer instead of blocking |

**Why `leaky=downstream` rather than `leaky=0` (block):** a `tee` element's
chain function is synchronous — it pushes each buffer to *all* src pads
before returning.  With `leaky=0`, when `q_arch` filled, the tee push to it
would block, the tee never gets to its push to `q_webrtc`, and after
`q_webrtc` drains downstream the live viewer freezes.  `leaky=downstream`
makes `q_arch.sink.push` return immediately by evicting the oldest buffer,
so the tee continues to `q_webrtc` without delay.  The cost is that a
sustained archive encoder lag produces visible jumps in the recording; an
`overrun` signal handler in `pipeline.py` logs a debounced warning when this
happens (at most one message per 30 s) so the operator can act.

Setting `ARCHIVE_QUEUE_MAX_BYTES=0` and `ARCHIVE_QUEUE_MAX_SEC=0` reverts to
the legacy unbounded behaviour: zero archive frame loss, but the queue can
grow without limit if the encoder cannot keep up.

#### `nvh264enc` or `x264enc` (name: `arch_enc`)

Re-encodes the raw decoded video to H.264 for the archive.  The encoder is
selected at runtime (`build_archive_encoder()`) based on `ARCHIVE_QUALITY`,
which has three modes:

**`ARCHIVE_QUALITY=visually-lossless`** *(default)* — CRF/CQP at
`ARCHIVE_QP` (default 18).  Indistinguishable from the source on screen
content; ~2-4× the file size of `legacy` mode.

| Encoder | Property | Value | Purpose |
|---|---|---|---|
| `nvh264enc` | `rc-mode` | `constqp` | Quality-targeted (not bitrate-targeted) |
| `nvh264enc` | `qp-const{,-i,-p,-b}` | `ARCHIVE_QP` | All QP knobs at the configured value (build-version compat) |
| `nvh264enc` | `preset` | `high-quality` | Trade encode time for compression efficiency |
| `nvh264enc` | `gop-size` | `60` | Keyframe every 2 s at 30 fps |
| `nvh264enc` | `max-bitrate` | `ARCHIVE_BITRATE_CAP` kbps (default 100 000) | Ceiling so a chaotic frame can't blow up segment size |
| `x264enc`   | `pass` | `5` (qual) | CRF (rate-distortion-optimised quality) |
| `x264enc`   | `quantizer` | `ARCHIVE_QP` | Effective CRF value |
| `x264enc`   | `qp-min` / `qp-max` | `0` / `51` | Full QP range |
| `x264enc`   | `speed-preset` | `4` (faster) | Better compression than legacy `ultrafast` |
| `x264enc`   | `key-int-max` | `60` | Keyframe every 2 s at 30 fps |

**`ARCHIVE_QUALITY=lossless`** — true lossless (QP=0).  Files can be very
large (50–200 Mbps during heavy motion); set `ARCHIVE_MAX_BYTES`.

| Encoder | Property | Value |
|---|---|---|
| `nvh264enc` | `preset` (if available) | `lossless-hp` |
| `nvh264enc` | `rc-mode` | `constqp` |
| `nvh264enc` | `qp-const{,-i,-p,-b}` | `0` |
| `nvh264enc` | `gop-size` | `60` |
| `x264enc`   | `pass` | `4` (quant — true CQP) |
| `x264enc`   | `quantizer` | `0` |
| `x264enc`   | `qp-min` / `qp-max` | `0` / `0` |
| `x264enc`   | `speed-preset` | `4` (faster) |

**`ARCHIVE_QUALITY=legacy`** — byte-for-byte compatible with the
configuration shipped before the archive quality work.  `ARCHIVE_BITRATE`
(default 6000 kbps) is consulted only in this mode.

| Encoder | Property | Value |
|---|---|---|
| `nvh264enc` | `preset` | `low-latency-hq` |
| `nvh264enc` | `rc-mode` | `vbr-hq` |
| `nvh264enc` | `bitrate` / `max-bitrate` | `ARCHIVE_BITRATE` kbps |
| `nvh264enc` | `gop-size` | `30` |
| `x264enc`   | `tune` | `0x4` (zerolatency) |
| `x264enc`   | `speed-preset` | `1` (ultrafast) |
| `x264enc`   | `bitrate` | `ARCHIVE_BITRATE` kbps |
| `x264enc`   | `key-int-max` | `30` |

The archive encoder runs independently of the browser-facing `webrtcsink`
instances and the network — its quality is fixed at the configured QP rather
than reactive to viewer bandwidth.  This means the archived recording always
carries the configured quality, even when individual viewers are throttled.

The `visually-lossless` and `lossless` modes deliberately drop the
`tune=zerolatency` / `preset=low-latency-hq` settings: viewers see the
screen via the WebRTC branch, so the archive encoder has no latency
requirement and is free to use larger lookahead and B-frames for better
compression.

#### `h264parse` (name: `arch_h264`)

| Property | Value | Purpose |
|---|---|---|
| `config-interval` | `-1` | Inject SPS/PPS before every keyframe |

Parses the raw H.264 byte stream and manages SPS (Sequence Parameter Set) and
PPS (Picture Parameter Set) NAL units. With `config-interval=-1` the SPS/PPS
are repeated before every IDR (keyframe). This is critical for archive
segmentation: each segment must be independently decodable from its first
frame. Without inline SPS/PPS, a segment starting on a non-IDR frame would be
undecodable without seeking back to the previous segment.  The same property
is also what allows the post-rotation MP4 remux to be a pure `-c copy`
operation — the bitstream already carries the codec parameters in-band, so
the new MP4 muxer can synthesize the `avcC` box without re-encoding.

#### `splitmuxsink` (name: `archive`)

| Property | Value | Purpose |
|---|---|---|
| `muxer-factory` | `matroskamux` | Live container; readable mid-write |
| `location` | `${ARCHIVE_LIVE_DIR}/{prefix}-%05d.mkv` | Output path for the in-progress segment |
| `max-size-time` | `ARCHIVE_SEGMENT_SEC × Gst.SECOND` | Rotate to a new file every N seconds |

Muxes the H.264 stream into rotating Matroska fragments. `splitmuxsink` opens
a new file automatically when the current segment reaches the
`max-size-time` limit, ensuring no single file grows unboundedly.

**Why MKV here, MP4 elsewhere?**  Matroska handles non-monotonic timestamps
and open-ended streams gracefully, and does not require a final `moov` atom
to be readable — meaning the in-progress fragment can be opened and read
mid-write.  This is what lets `/archive` include the active segment in
responses while a recording is still ongoing.  Web players, however, want
MP4 (universal browser support) with the `moov` atom at the front of the
file (faststart, so playback can begin before the full file has been
received).  We get both by keeping MKV as the live container and remuxing
to MP4 on rotation.

#### Post-rotation: MKV → MP4 finalize

When `splitmuxsink` rotates a fragment, `pipeline.py` enqueues the
just-completed `.mkv` for remux on a single background daemon thread.
ffmpeg writes the new MP4 alongside the source in `ARCHIVE_LIVE_DIR`
under a `.part` suffix, then the finished file is published into
`ARCHIVE_DIR` via an atomic rename:

```
# 1. ffmpeg writes the new MP4 next to the source, in ARCHIVE_LIVE_DIR
ffmpeg -y -nostdin -hide_banner -loglevel error \
       -fflags +genpts -i ${LIVE}/${prefix}-NNNNN.mkv \
       -c copy -map 0:v:0 -movflags +faststart \
       ${LIVE}/${prefix}_YYYYMMDD-HHMMSS.SSS_to_YYYYMMDD-HHMMSS.SSS.mp4.part

# 2. Move into ARCHIVE_DIR under .part; cross-fs copies land there, not
#    under the final name.  Then atomic rename to the final name.
shutil.move(    ${LIVE}/${name}.mp4.part,    ${ARCHIVE}/${name}.mp4.part )
os.rename(      ${ARCHIVE}/${name}.mp4.part, ${ARCHIVE}/${name}.mp4     )

# 3. Source MKV deleted only after publication succeeds.
os.unlink(${LIVE}/${prefix}-NNNNN.mkv)
```

Key properties:

- **`-c copy`** — the H.264 packets are remuxed verbatim.  No re-encoding,
  no quality loss, near-instant compared to a transcode.
- **`-movflags +faststart`** — ffmpeg does a 2-pass write to place the
  `moov` atom at the front of the MP4, allowing web players to start
  decoding from the first received bytes.  The 2-pass intermediate
  states stay confined to `ARCHIVE_LIVE_DIR` because the work file is
  written there.
- **Atomic publication** — `ARCHIVE_DIR`'s `*.mp4` glob never matches a
  partially-written file.  ffmpeg's output is hidden under `.part` in
  `ARCHIVE_LIVE_DIR`; the cross-filesystem copy lands under `.part` in
  `ARCHIVE_DIR`; only the final `os.rename` makes the new segment
  visible to readers.
- **Background worker** — the `format-location-full` callback returns
  immediately; the remux runs off the GStreamer streaming thread so it
  cannot back up the pipeline queues.
- **Fallback** — if ffmpeg ever fails (corrupt fragment, missing tool),
  the pipeline cleans up the partial `.mp4.part` and moves the original
  `.mkv` into `ARCHIVE_DIR` (also via a `.part`-then-rename publication)
  so a recording is never lost.  The fallback `.mkv` is not picked up by
  `/archive` (which globs `*.mp4`); it sits on disk until an admin
  recovers it.

Keeping the live write path on its own filesystem (e.g. tmpfs or a fast
local disk) and the completed archive on a slower bulk volume is
supported transparently — the cross-fs copy and the in-place rename are
both handled correctly.

---

### Segment Naming and Timestamping

Segments are written into `ARCHIVE_LIVE_DIR` with sequential numeric names
(`{prefix}-00000.mkv`, `{prefix}-00001.mkv`, …).  When a segment is
completed it is remuxed (no re-encode) to a faststart MP4 in `ARCHIVE_DIR`
under its final timestamp-based name:

```
{prefix}_YYYYMMDD-HHMMSS.SSS_to_YYYYMMDD-HHMMSS.SSS.mp4
```

**How timestamps are derived:**

The `format-location-full` signal fires on `splitmuxsink` at the start of each
new fragment.  The callback (`_on_format_location_full`) stamps the boundary
with `time.time_ns()`.  This value simultaneously becomes the **end**
timestamp of the just-completed fragment and the **start** timestamp of the
new one — both reads happen in the same callback call, so adjacent segments
abut exactly.  The old fragment is enqueued for finalize (MKV → MP4 remux
on a background worker), with the new name computed by
`archive_times.renamed_segment_path(..., ext='.mp4')`.

On pipeline shutdown (EOS), `format-location-full` does not fire for the
final fragment.  The EOS handler stamps it with `time.time_ns()`, enqueues
it, and waits for the queue to drain so the last segment lands in
`ARCHIVE_DIR` before the process exits.

The timestamp precision is millisecond-level, matching the filename format
`YYYYMMDD-HHMMSS.SSS`.  The start of segment N+1 is always exactly equal to the
end of segment N — there are no gaps or overlaps between adjacent completed
segments.

---

### Archive Retention (Purge)

When either `ARCHIVE_MAX_BYTES` or `ARCHIVE_MAX_AGE_DAYS` is non-zero,
`archive_purge.purge_archive()` runs:

- **At pipeline startup**, before the GLib main loop starts.
- **Every `ARCHIVE_SEGMENT_SEC` seconds** thereafter, scheduled via
  `GLib.timeout_add_seconds`.

The purge logic (`archive_purge.py`):

1. Sorts all `.mp4` files in `ARCHIVE_DIR` (completed segments only) by mtime.
2. Exempts the most recent file as a safety margin so at least one segment
   always survives.  The currently-writing segment lives in
   `ARCHIVE_LIVE_DIR` and is therefore never visible to the purger.
3. If `ARCHIVE_MAX_AGE_DAYS` is set, deletes every remaining file whose mtime
   is older than the cutoff.
4. If `ARCHIVE_MAX_BYTES` is set, deletes the oldest remaining files one by
   one until the total size of surviving files is within the limit.

---

### Browser WebRTC Branch

#### `queue` (name: `q_webrtc`)

Decouples the main `tee` from the browser sub-tree for the same reason as
`q_arch`: prevents browser encoding/network back-pressure from stalling the
archive branch.

#### `tee` (name: `t_webrtc`)

Splits the single decoded stream into three parallel browser feeds: full frame,
top half, and bottom half. Each feed is sent to a separate `webrtcsink` on its
own signalling port, allowing a browser to subscribe to any one of the three
views independently.

#### `queue` → `webrtcsink` (full stream, name: `ws_full`)

| Property | Value | Purpose |
|---|---|---|
| `signaller.uri` | `ws://127.0.0.1:8443` | Browser-facing signalling server |
| `video-caps` | `video/x-vp9;video/x-h264` | Offer VP9 and H.264 to browsers |
| `stun-server` | `$GST_WEBRTC_STUN_SERVER` | Optional STUN for NAT traversal |

`q_full` isolates `ws_full`'s encoder from the sibling sinks. `ws_full` then
operates identically to the caster's `webrtcsink` but serves browser viewers
instead of the service. It handles per-peer encoding, SDP/ICE negotiation, DTLS,
and adaptive bitrate entirely internally.

Both VP9 and H.264 are offered because VP9 delivers better quality at lower
bitrates (benefiting viewers on slow connections) while H.264 has broader
hardware decode support on older devices. The browser selects whichever codec it
prefers.

#### `queue` → `videocrop` → `webrtcsink` (top half, name: `ws_top`)

| Element | Property | Value | Purpose |
|---|---|---|---|
| `q_top` | — | — | Isolate top branch from sibling branches |
| `crop_top` | `bottom` | `CROP_HEIGHT` px | Remove the bottom half of the frame |
| `ws_top` | `signaller.uri` | `ws://127.0.0.1:8444` | Top-half signalling server |

`videocrop` removes pixels from the named edge. Setting `bottom=CROP_HEIGHT`
removes `CROP_HEIGHT` rows from the bottom, leaving only the top half of the
frame. The resulting cropped video is then streamed to browsers via `ws_top`
with the same per-peer adaptive bitrate behaviour as `ws_full`.

#### `queue` → `videocrop` → `webrtcsink` (bottom half, name: `ws_bot`)

| Element | Property | Value | Purpose |
|---|---|---|---|
| `q_bot` | — | — | Isolate bottom branch from sibling branches |
| `crop_bot` | `top` | `CROP_HEIGHT` px | Remove the top half of the frame |
| `ws_bot` | `signaller.uri` | `ws://127.0.0.1:8445` | Bottom-half signalling server |

Mirror of the top-half branch. Setting `top=CROP_HEIGHT` removes `CROP_HEIGHT`
rows from the top of the frame, leaving only the bottom half. Together,
`ws_top` and `ws_bot` allow one physical screen to be presented as two
independent sub-streams, each served through its own signalling endpoint and
each with independent per-viewer adaptive bitrate.

---

## Queue Strategy and Isolation

Every branch that originates from a `tee` begins with a `queue`. This is a
deliberate design choice:

- `tee` pushes each buffer **synchronously** to all downstream pads by default.
  If one downstream element blocks (e.g., a browser encoder is briefly busy),
  `tee` would stall and every other branch would stop receiving frames.
- Placing a `queue` after each `tee` output pad decouples the branches into
  separate threads. Each branch drains its queue at its own pace.
- This means an archiving stall does not drop browser frames, and a slow
  browser connection does not delay the archive write.

---

## Adaptive Bitrate (RTCP / REMB)

`webrtcsink` integrates RTCP feedback processing automatically:

1. The remote browser sends **REMB** (Receiver Estimated Maximum Bitrate)
   packets back through the SRTP session reporting its available bandwidth.
2. `webrtcsink` reads these reports and adjusts the encoder's target bitrate
   up or down per peer.
3. Because each peer has its own `webrtcbin` and encoder instance inside
   `webrtcsink`, one viewer's bandwidth constraint never affects another's
   quality.

The archive encoder is completely independent of this loop; its bitrate is
fixed at `ARCHIVE_BITRATE` kbps and is never reduced due to network conditions.

---

## TURN Server Configuration

TURN server credentials are injected per `webrtcbin` instance via the
`deep-element-added` pipeline signal (both `caster/pipeline.py` and
`service/pipeline.py`, `on_deep_element_added`). `webrtcsink` and `webrtcsrc`
create a new internal `webrtcbin` element for each peer connection; the signal
fires each time one is added to the pipeline hierarchy, at which point
`element.emit('add-turn-server', TURN)` registers the relay. This approach is
required because there is no single `webrtcbin` element to configure upfront —
it is a dynamic, per-peer resource.

---

## HTTP Web Server

`web_server.py` serves on `WEB_PORT` (default 8080) and handles four routes.

### `GET /<screen-name>`

Each configured screen path (e.g. `/top`, `/bottom`, `/left`, `/right`, or
`/screen1`…) serves `index.html` directly.  The page reads `/config.json`
to pick the correct WebRTC signalling port for its path.  All other paths
are served as static files from `WEB_DIR`.

### `GET /config.json`

Returns the runtime config — `desktopName`, `mode`, `width`, `height`,
`fullSignallingPort`, and a `screens` list (each entry has `name`, `path`,
`signallingPort`, `x`, `y`, `width`, `height`).  Read by `index.html` on
load.

### `GET /archive`

Returns a `.zip` of all faststart `.mp4` segments whose recorded time
overlaps the requested window.

**Parameters** (one form required):

| Form | Example |
|---|---|
| `last=<duration>` | `last=30m` |
| `start=<ts>&end=<ts>` | `start=1700000000&end=1700003600` |

`<duration>` is a number followed by `s`, `m`, or `h` (e.g. `90s`, `1.5h`).

`<ts>` accepts:
- Unix epoch seconds (integer or float)
- ISO 8601 datetime with optional timezone (`Z` or `±HH:MM`); no timezone
  assumes UTC.

**Implementation:** `stage_segments()` copies overlapping segments into a
temporary directory, then `zip_segments()` packs them into a zip file streamed
directly to the client.

### `GET /video`

Returns a single faststart `.mp4` covering **exactly** the requested time
window.  Requests longer than 12 hours are rejected with 400.

**Parameters:** same as `/archive`.

**Implementation** (`stage_segments()` → `transcode_to_video()`):

1. **Staging** (`stage_segments()`): overlapping segments are normalized into
   a temp directory as faststart `.mp4` so downstream code only ever sees
   one container format.

   - **Completed segments** (timestamp names, already `.mp4` after the
     pipeline's MKV → MP4 finalize): copied as-is.
   - **Active segment** (sequential numeric `.mkv` — the fragment
     currently being written by `splitmuxsink`): its start time is taken
     from the end timestamp of the last completed segment (the same
     boundary `pipeline.py` will record when it finalises the file).  The
     file is opened by file descriptor before ffmpeg starts; ffmpeg reads
     via `/proc/self/fd/<n>`, so a rotation by the pipeline after that
     point does not interrupt the read because the fd holds the inode
     reference.  If the rotation fires before the `os.open()` call, the
     now-completed `.mp4` is found in `ARCHIVE_DIR` via a fresh directory
     scan.  The active fragment is remuxed (`-c copy -movflags
     +faststart` — no re-encode) into a faststart `.mp4` named with the
     same `{prefix}_..._to_..._mp4` convention so the assembly step can
     treat it identically to a completed segment.

2. **Assembly** (`video_transcode.py`, `transcode_to_video()`): builds and runs
   an ffmpeg `filter_complex`:

   - A solid-colour **base video** of exactly `end_ts − start_ts` seconds fills
     the entire output duration.  Its colour is `VIDEO_FILL_COLOR` (ARGB);
     its dimensions and frame rate are taken from the first available segment.
   - Each segment is **overlaid** on the base at its correct temporal position
     using `overlay=eof_action=pass`.  Overlays are applied in chronological
     order.
   - A segment whose recording start is **before** `start_ts` has its leading
     content trimmed (`trim=start=<offset>`).
   - A segment whose recording end is **after** `end_ts` is clipped implicitly
     when the base video ends — no explicit trim needed.
   - Gaps (periods with no recorded content) are filled by the base colour
     showing through where no overlay is present.

   The output is encoded with `libx264 -preset ultrafast`, written with
   `-movflags +faststart` so the `moov` atom is at the front of the file,
   and streamed to the client as `video/mp4`.

---

## Environment Variable Reference

### Caster

| Variable | Default | Description |
|---|---|---|
| `DISPLAY` | `:0` | X11 display to capture |
| `STREAM_WIDTH` | `1920` | Capture width in pixels |
| `STREAM_HEIGHT` | `1080` | Capture height in pixels |
| `STREAM_FRAMERATE` | `30` | Target frames per second |
| `SIGNALLING_PORT` | `8443` | Local signalling server port |
| `GST_WEBRTC_STUN_SERVER` | `` | STUN URI (e.g. `stun://stun.l.google.com:19302`) |
| `GST_WEBRTC_TURN_SERVER` | `` | TURN URI (e.g. `turn://user:pass@host:3478`) |

### Service

| Variable | Default | Description |
|---|---|---|
| `CASTER_HOST` | *(required)* | Hostname or IP of the caster container |
| `CASTER_SIGNALLING_PORT` | `8443` | Caster's signalling server port |
| `CASTER_PEER_ID` | `desktop-caster` | Producer peer-id to request from caster |
| `DESKTOP_NAME` | `desktop` | Label shown in the page header and used as the archive filename prefix |
| `STREAM_WIDTH` | `1920` (caster) / native (host) | Capture width.  In host mode, leaving it unset reads the X server's native width via `xrandr` |
| `STREAM_HEIGHT` | `1080` (caster) / native (host) | Capture height.  In host mode, leaving it unset reads the X server's native height via `xrandr` |
| `DESKTOP_SPLITS` | _(empty)_ | `WxH+X+Y;WxH+X+Y;…` regions.  In host mode unset triggers `xrandr --listmonitors` auto-detection.  In caster mode unset falls back to a `CROP_HEIGHT`-based top/bottom split |
| `ARCHIVE_DIR` | `/archive` | Directory for completed (timestamp-named) faststart `.mp4` segments |
| `ARCHIVE_LIVE_DIR` | `/archive-live` | Directory the in-progress `.mkv` segment is written into; each segment is remuxed to `.mp4` (`-c copy -movflags +faststart`) and moved into `ARCHIVE_DIR` when it rotates |
| `ARCHIVE_SEGMENT_SEC` | `600` | Segment duration in seconds |
| `ARCHIVE_QUALITY` | `visually-lossless` | Encoder quality mode: `visually-lossless` (CRF/CQP at `ARCHIVE_QP`), `lossless` (true QP=0), or `legacy` (fixed-bitrate VBR using `ARCHIVE_BITRATE`) |
| `ARCHIVE_QP` | `18` | QP for `visually-lossless` mode (0–51, lower is better; 18 is the conventional visually-lossless threshold for H.264).  Ignored in other modes |
| `ARCHIVE_BITRATE_CAP` | `100000` | kbps ceiling on instantaneous bitrate in CQP modes — caps a chaotic frame from blowing up segment size |
| `ARCHIVE_BITRATE` | `6000` | Archive H.264 bitrate in kbps; consulted only when `ARCHIVE_QUALITY=legacy` |
| `ARCHIVE_QUEUE_MAX_BYTES` | `536870912` (512 MB) | Bytes of raw video the `q_arch` queue may buffer before dropping the oldest frame.  Set to `0` to disable the byte gate |
| `ARCHIVE_QUEUE_MAX_SEC` | `5` | Seconds of running-time the `q_arch` queue may buffer before dropping the oldest frame.  Set to `0` to disable the time gate |
| `ARCHIVE_MAX_BYTES` | `0` | Delete oldest segments when archive exceeds this size; `0` = unlimited |
| `ARCHIVE_MAX_AGE_DAYS` | `0` | Delete segments older than this many days; `0` = unlimited |
| `VIDEO_QP` | `ARCHIVE_QP` (`18`) | CRF (libx264) / QP (h264_nvenc) used by `/video` to assemble its output.  Defaults to `ARCHIVE_QP` so /video preserves whatever quality the archive carries |
| `SIGNALLING_PORT` | `8443` | Base port for browser-facing signalling servers; screen `i` uses `SIGNALLING_PORT + 1 + i` |
| `CROP_HEIGHT` | _(unset)_ | Legacy split point; only consulted when `DESKTOP_SPLITS` is unset and (host mode) xrandr returns fewer than 2 monitors |
| `WEB_PORT` | `8080` | HTTP server listening port |
| `WEB_DIR` | `/var/www/html` | Static file root for the HTTP web server |
| `VIDEO_FILL_COLOR` | `0xFF000000` | ARGB fill colour for gaps in `/video` output (default: opaque black) |
| `VIDEO_DEFAULT_WIDTH` | `1920` | Output width for `/video` when no segments are available |
| `VIDEO_DEFAULT_HEIGHT` | `1080` | Output height for `/video` when no segments are available |
| `GST_WEBRTC_STUN_SERVER` | `` | STUN URI |
| `GST_WEBRTC_TURN_SERVER` | `` | TURN URI |

Each configured screen runs its own signalling server on
`SIGNALLING_PORT + 1 + i` (zero-indexed).  Screen names are auto-assigned
from geometry:

* exactly 2 regions side-by-side  → `left` / `right`
* exactly 2 regions stacked       → `top`  / `bottom`
* anything else                   → `screen1`, `screen2`, … in reading order
  (top to bottom, then left to right within each row).

The runtime config (desktop name, capture resolution, screen list) is
written once at container start to `/run/desktop-stream/config.json` and is
also served by the web server at `GET /config.json`.
