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
│                              │           (.mkv segments) │
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

Decouples the `tee` from the encoder. Without a queue here, back-pressure from
a slow encoder (particularly `x264enc` on a loaded CPU) would stall the `tee`
and starve the browser WebRTC branch. The queue absorbs bursts and lets each
branch run at its own pace.

#### `nvh264enc` or `x264enc` (name: `arch_enc`)

Re-encodes the raw decoded video to H.264 for the archive. The encoder is
selected at runtime (`build_archive_encoder()`):

**NVIDIA GPU present — `nvh264enc`:**

| Property | Value | Purpose |
|---|---|---|
| `preset` | `low-latency-hq` | Balance encode speed with quality |
| `rc-mode` | `vbr-hq` | Variable bitrate, high quality |
| `bitrate` | `6000` kbps | Target archive bitrate |
| `max-bitrate` | `6000` kbps | Cap to prevent segment size spikes |
| `gop-size` | `30` | Keyframe every 30 frames (1 s at 30 fps) |

**No GPU — `x264enc`:**

| Property | Value | Purpose |
|---|---|---|
| `tune` | `0x4` (zerolatency) | Minimise encode latency |
| `speed-preset` | `1` (ultrafast) | Use fewest CPU cycles per frame |
| `bitrate` | `6000` kbps | Target archive bitrate |
| `key-int-max` | `30` | Keyframe every 30 frames |

The archive encoder runs independently of the browser-facing `webrtcsink`
instances, so its bitrate is fixed rather than adaptive. This ensures the
archived recording is always at full quality even if a browser viewer has poor
bandwidth.

#### `h264parse` (name: `arch_h264`)

| Property | Value | Purpose |
|---|---|---|
| `config-interval` | `-1` | Inject SPS/PPS before every keyframe |

Parses the raw H.264 byte stream and manages SPS (Sequence Parameter Set) and
PPS (Picture Parameter Set) NAL units. With `config-interval=-1` the SPS/PPS
are repeated before every IDR (keyframe). This is critical for archive
segmentation: each `.mkv` segment must be independently decodable from its
first frame. Without inline SPS/PPS, a segment starting on a non-IDR frame
would be undecodable without seeking back to the previous segment.

#### `splitmuxsink` (name: `archive`)

| Property | Value | Purpose |
|---|---|---|
| `muxer-factory` | `matroskamux` | Wrap H.264 in Matroska (.mkv) container |
| `location` | `/archive/{prefix}-%05d.mkv` | Output path with zero-padded segment index |
| `max-size-time` | `ARCHIVE_SEGMENT_SEC × Gst.SECOND` | Rotate to a new file every N seconds |

Muxes the H.264 stream into rotating Matroska segments. `splitmuxsink` opens a
new file automatically when the current segment reaches the `max-size-time`
limit, ensuring no single file grows unboundedly. Matroska was chosen over MP4
because it handles non-monotonic timestamps and open-ended streams gracefully,
and does not require a final `moov` atom write to be playable.

---

### Segment Naming and Timestamping

Segments are written with sequential numeric names
(`{prefix}-00000.mkv`, `{prefix}-00001.mkv`, …).  When a segment is completed,
it is atomically renamed to embed its precise wall-clock recording interval:

```
{prefix}_YYYYMMDD-HHMMSS.SSS_to_YYYYMMDD-HHMMSS.SSS.mkv
```

**How timestamps are derived:**

The `format-location-full` signal fires on `splitmuxsink` at the start of each
new fragment.  The callback (`_on_format_location_full`) computes the wall-clock
time of the first frame in the new fragment as:

```
now_ns = pipeline.get_base_time() + buf.pts
```

(falling back to `time.time()` if `buf.pts` is unavailable)

This value simultaneously becomes the **end** timestamp of the just-completed
fragment and the **start** timestamp of the new one.  The old fragment is
renamed immediately using `archive_times.renamed_segment_path()`.

On pipeline shutdown (EOS), `format-location-full` does not fire for the final
fragment.  The EOS handler (`on_message`) renames it using
`pipeline.get_base_time() + pipeline.query_position(Gst.Format.TIME)` as the
end timestamp, falling back to `time.time()` if the clock query fails.

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

1. Sorts all `.mkv` files in `ARCHIVE_DIR` by mtime.
2. Exempts the most recent file (it may still be open for writing).
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

Returns a `.zip` of all `.mkv` segments whose recorded time overlaps the
requested window.

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

Returns a single `.mkv` covering **exactly** the requested time window.
Requests longer than 12 hours are rejected with 400.

**Parameters:** same as `/archive`.

**Implementation** (`stage_segments()` → `transcode_to_video()`):

1. **Staging** (`stage_segments()`): overlapping segments are copied to a temp
   directory.  All staged files receive timestamp-embedded names so downstream
   code can rely purely on filename parsing.

   - **Completed segments** (timestamp names): copied as-is.
   - **Active segment** (sequential numeric name — the file currently being
     written by `splitmuxsink`): its start time is taken from the end timestamp
     of the last completed segment (the same boundary `pipeline.py` will record
     when it finalises the file).  The file is opened by file descriptor before
     the copy begins; an `os.rename` by the pipeline after that point does not
     interrupt the copy because the fd holds the inode reference.  If the rename
     fires before the `open()` call, the now-completed file is found via a fresh
     directory scan.

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

   The output is encoded with `libx264 -preset ultrafast` and streamed to the
   client as `video/x-matroska`.

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
| `ARCHIVE_DIR` | `/archive` | Output directory for `.mkv` segments |
| `ARCHIVE_SEGMENT_SEC` | `600` | Segment duration in seconds |
| `ARCHIVE_BITRATE` | `6000` | Archive H.264 bitrate in kbps |
| `ARCHIVE_MAX_BYTES` | `0` | Delete oldest segments when archive exceeds this size; `0` = unlimited |
| `ARCHIVE_MAX_AGE_DAYS` | `0` | Delete segments older than this many days; `0` = unlimited |
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
