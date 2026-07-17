# Pipeline Documentation

This document describes the media pipeline of the desktop-stream-service in
depth: the single ffmpeg process (capture, filter graph, encoders, outputs),
the MediaMTX egress, the archive subsystem, the browser client, and the HTTP
API. For a high-level overview and quick start, see [README.md](README.md).

---

## End-to-End Architecture

```
X11 display (:0)
   │  x11grab (one capture at native size)
   ▼
scale=WxH,format=nv12          ── one normalisation, shared by all branches
   │  split
   ├──► full   tier 0 (1.0)  ── h264 ──► rtsp://127.0.0.1:8554/full_t0 ─┐
   ├──► full   tier 1 (0.5)  ── scale ── h264 ──► …/full_t1 ────────────┤
   ├──► screen crop ── tier 0 ── h264 ──► …/<name>_t0 ──────────────────┤
   │                └─ tier 1 ── scale ── h264 ──► …/<name>_t1 ─────────┤
   │                                                                    ▼
   │                                                                MediaMTX
   │                                                                    │ WHEP
   │                                                                    ▼
   └──► archive ── h264 (quality mode) ── segment muxer ──► /archive-live
                                                             │ rotate
                                                             ▼
                                              finalize watcher ──► /archive
```

Three cooperating processes, started in order by `entrypoint.sh`:

| Process | Role |
|---|---|
| **MediaMTX** | RTSP ingest on loopback, WebRTC/WHEP egress to browsers. Passthrough only — no transcoding, no GPU. |
| **`web_server.py`** | Static page, `/config.json`, `/archive`, `/video`. |
| **`pipeline.py`** | Spawns and supervises the single ffmpeg process; finalizes archive segments; schedules purging. |

All three read the same runtime config (`/run/desktop-stream/config.json`),
written once at startup by `desktop_config.py` from environment variables +
RandR probes.

---

## The ffmpeg process

`stream_command.py` builds the entire argv as a pure function of
(config, environment, NVENC availability) — unit-testable without ffmpeg.
`pipeline.py` decides NVENC availability once at startup by encoding a
single synthetic frame with `h264_nvenc`; if the NVIDIA driver libraries
aren't dlopen-able the process falls back to `libx264` for every output.

### Capture

```
-f x11grab -framerate $STREAM_FRAMERATE -i $DISPLAY
```

x11grab captures the full root window over XCB into system memory. There is
no GPU-side capture (NVFBC is entitlement-gated and impractical on X11) —
capture cost is CPU-side and proportional to desktop size, same as the old
`ximagesrc`.

### Filter graph

The graph normalises once, then fans out (mirroring the old
`cudaconvertscale → tee` shape, but on CPU):

```
[0:v]scale=1920:1080,format=nv12,split=4[v_full][v_top][v_bottom][v_arch];
[v_full]split=2[full_t0_raw][full_t1_raw];
[full_t0_raw]null[full_t0];
[full_t1_raw]scale=960:540[full_t1];
[v_top]crop=1920:540:0:0,split=2[top_t0_raw][top_t1_raw];
...
```

Properties worth noting:

- **The first `scale=W:H,format=nv12` runs once**, converting BGRA→NV12 and
  applying any `STREAM_WIDTH`/`STREAM_HEIGHT` resize before the fan-out.
  Every downstream crop/scale then operates on chroma-subsampled NV12
  (half the bytes of BGRA).
- **Each screen is cropped once**, before its tier split — never once per
  tier.
- **Passthrough tiers** (scale 1.0) get a `null` filter that just renames
  the pad; no pixels are touched.
- Scaling runs on CPU (swscale): the RPM Fusion ffmpeg build has no CUDA
  scale filters. This is the one efficiency regression vs. the GPU-resident
  GStreamer graph, and the reason the default ladder is short.

### Live encoder settings

Per tier (see `live_encoder_args()` in `stream_command.py`):

```
-c:v h264_nvenc -preset p4 -tune ll
-rc vbr -cq $LIVE_CQ -b:v 0
-maxrate <cap> -bufsize <2x cap>
-g $LIVE_GOP -bf 0 -profile:v main
-spatial_aq 1 -temporal_aq 1
```

CPU fallback:

```
-c:v libx264 -preset ultrafast -tune zerolatency
-crf $LIVE_CQ -maxrate <cap> -bufsize <2x cap> -g $LIVE_GOP
```

**Rate control — capped constant quality.** The encoder targets a constant
quality (`-cq 18`, the conventional visually-lossless threshold for H.264)
and the VBV cap (`-maxrate`/`-bufsize`) bounds what the network ever sees:

- Static desktop content encodes at a very low bitrate with QP ≈ `LIVE_CQ`.
- Motion bursts raise QP under the cap — a transient clarity dip — instead
  of a bitrate spike beyond `LIVE_MAXRATE`.
- `LIVE_MAXRATE` describes the **full-resolution tier**; smaller tiers are
  capped proportionally to their pixel count (with a 500 kbps floor), so
  ladder bandwidth stays predictable.

This is a deliberate replacement for the old per-viewer REMB adaptation
(a webrtcsink capability): there is no feedback loop from viewers to the
encoder, so the cap must be pre-committed to the worst-case provisioned
bandwidth. The alternative — fixed CBR for strictly constant link usage —
is documented in the decision-record comment at the top of
`stream_command.py`, along with the trade-offs.

**Keyframes and join latency.** MediaMTX cannot request a keyframe from an
RTSP publisher, and it forwards RTP to a new WHEP viewer immediately — the
browser simply stays black until the next keyframe. `LIVE_GOP` (default one
second of frames) is therefore the worst-case viewer join / loss-recovery
horizon, not a tuning nicety. `-bf 0` keeps B-frames out of the live path
(latency + WebRTC decoder compatibility).

### Archive encoder settings

`archive_encoder.py` maps `ARCHIVE_QUALITY` to an ffmpeg argv fragment:

| Mode | NVENC | libx264 fallback |
|---|---|---|
| `visually-lossless` (default) | `-preset p6 -rc constqp -qp $ARCHIVE_QP -bf 2` | `-preset ultrafast -tune zerolatency -qp $ARCHIVE_QP` |
| `lossless` | `-preset p6 -tune lossless` (the encoder's dedicated lossless mode) | `-qp 0` |
| `legacy` | `-preset p4 -tune ll -rc vbr -b:v/-maxrate $ARCHIVE_BITRATE k` | `-b:v $ARCHIVE_BITRATE k` |

The archive has no latency requirement, so the NVENC modes use a
quality-oriented preset with B-frames. The libx264 fallback keeps the
real-time-safe ultrafast/zerolatency profile so a GPU-less host can archive
alongside the live encodes. All modes share the live GOP (one keyframe per
second) so every MP4 fragment is exactly one GOP.

### Outputs

Each live tier:

```
-map [<label>] <encoder args> -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/<whepPath>
```

RTSP-over-TCP on loopback is flow-controlled and memory-speed; SRT/WHIP
would only add overhead on a real lossy network, which this hop is not.

The archive:

```
-map [v_arch] <encoder args>
-f segment -segment_time $ARCHIVE_SEGMENT_SEC -reset_timestamps 1
-segment_format mp4
-segment_format_options movflags=+frag_keyframe+empty_moov+default_base_moof:flush_packets=1
-segment_list $ARCHIVE_LIVE_DIR/segments.csv -segment_list_type csv
-segment_start_number <first free number>
$ARCHIVE_LIVE_DIR/<prefix>-%05d.mp4
```

The `movflags` triple is what makes the archive simple:

- `empty_moov` writes the moov atom **at the front of the file** — the
  in-progress segment is a valid, parseable MP4 at every moment of its
  life. (The old mp4mux wrote moov at EOS, which forced ~200 lines of
  mdat-walking/Annex-B remux code in `web_server.py` to serve the active
  segment. That code is gone; the active segment is now a plain byte-copy.)
- `frag_keyframe` closes one fragment per keyframe → 1-second fragments.
- `default_base_moof` keeps the fragments standards-conformant for
  browsers' MSE parsers.
- `flush_packets=1` forces the bytes onto disk as they are produced.
  Without it, the muxer's 256 KB AVIO buffer keeps the on-disk active
  file at a bare `ftyp` for most of a low-bitrate segment's lifetime
  (a static desktop encodes at a few KB/s).  It must live inside
  `segment_format_options`: the segment muxer opens its own files, so a
  top-level `-flush_packets` never reaches them.  As a second line of
  defence, the active-segment copy in `archive_export.py` verifies a
  complete moov before staging and skips the file otherwise.

### Process supervision and back pressure

One ffmpeg process is a deliberate trade-off (single capture, minimal CPU)
accepted at planning time: a crash or a wedged output drops **all** outputs,
including the archive, until the supervisor restarts it.

- `pipeline.py` restarts ffmpeg on unexpected exit with exponential backoff
  (2 s → 30 s, reset once a run survives a minute). MediaMTX tolerates the
  publisher vanishing and re-appearing; the browser's WHEP client retries
  on connection failure — viewers see a short freeze.
- On restart, archive numbering resumes past any leftover sequential files
  (`-segment_start_number`), so orphans from a crash are never overwritten.
- `SIGTERM`/`SIGINT` are forwarded to ffmpeg as SIGINT — its graceful-quit
  path — so the final segment gets its trailer and its segment-list entry
  before exit. A 10 s grace period escalates to SIGKILL.
- Back pressure: the RTSP/TCP writes basically cannot block on loopback;
  MediaMTX reads the publisher eagerly and keeps per-viewer queues,
  dropping for slow readers — one struggling viewer cannot stall other
  viewers or ffmpeg. Residual stall risk is host-level CPU/GPU saturation,
  which is a sizing problem, not a protocol one.

---

## Archive finalization

ffmpeg appends one CSV line per **completed** segment to
`$ARCHIVE_LIVE_DIR/segments.csv`:

```
stream-00000.mp4,0.000000,600.000000     # name, start_s, end_s (stream time)
```

`archive_finalize.SegmentFinalizer` (polled every 0.5 s from `pipeline.py`)
tails that file and, for each new line:

1. Reconstructs wall-clock timestamps as `epoch_anchor + stream_time`,
   where the anchor is `time.time()` at ffmpeg spawn (the capture is
   realtime, so stream time tracks wall time).
2. Renames/moves the file into `ARCHIVE_DIR` as
   `{prefix}_YYYYMMDD-HHMMSS.SSS_to_YYYYMMDD-HHMMSS.SSS.mp4`
   (`archive_times.renamed_segment_path`, unchanged from the previous
   stack).

The move is staged through a `.part` name so `/archive`'s `*.mp4` glob
never sees a half-copied file: same-filesystem moves are two cheap renames;
cross-filesystem moves write bytes into `.part` and publish with one atomic
rename. A failed move leaves the source in the live dir under its
sequential name for manual recovery. Partial CSV lines (mid-write) are left
unconsumed and re-read on the next poll.

### Retention

`archive_purge.purge_archive` (unchanged) deletes oldest-first when
`ARCHIVE_MAX_BYTES` is exceeded and/or drops segments older than
`ARCHIVE_MAX_AGE_DAYS`; scheduled every `ARCHIVE_SEGMENT_SEC` from a
daemon thread in `pipeline.py`.

---

## MediaMTX configuration

`stream_command.build_mediamtx_config()` renders `mediamtx.yml` at container
start (entrypoint runs `python3 stream_command.py > /run/desktop-stream/mediamtx.yml`):

```yaml
rtsp: yes
rtspAddress: 127.0.0.1:8554     # loopback only — the publisher is co-located
rtspTransports: [tcp]

rtmp: no
hls: no
srt: no
moq: no                         # every default-on protocol must be listed —
api: no                         # an unlisted one keeps its default listener
                                # bound and collides across instances

webrtc: yes
webrtcAddress: :8889            # WHEP + ICE over HTTP
webrtcLocalUDPAddress: :8189    # SRTP media
webrtcIPsFromInterfaces: yes
# webrtcAdditionalHosts: [...]  # from WEBRTC_ADDITIONAL_HOSTS

paths:
  full_t0: {}                   # exactly the configured (stream x tier) paths;
  full_t1: {}                   # unknown publish/read attempts are rejected
  ...
```

MediaMTX serves each path's WHEP endpoint at
`http://<host>:8889/<path>/whep`. It repackages the H.264 RTP stream into
SRTP toward each viewer, retransmitting lost packets (NACK/RTX). It does
**not** adapt bitrate or resolution per viewer — that is fixed at the
encoder (see rate control above).

---

## Streams, tiers, and path naming

`desktop_config.py` computes, per stream (full frame + one per screen), a
tier ladder from `LIVE_SCALE_LADDER` (default `1.0,0.5`):

- Tier dimensions snap to even numbers (H.264 4:2:0 requirement); tiers
  below 64 px in either dimension are dropped; duplicate sizes dedup.
- Each (stream, tier) gets a stable MediaMTX path: `<stream>_t<index>` —
  `full_t0`, `full_t1`, `left_t0`, … The path is the single identifier
  shared by the ffmpeg publish URL, the MediaMTX path table, and the
  browser's WHEP endpoint, and is surfaced in `/config.json` as
  `whepPath`.
- Screen naming is unchanged: two side-by-side monitors are `left`/`right`,
  stacked are `top`/`bottom`, otherwise `screen1..N` in reading order.
  Regions come from `DESKTOP_SPLITS` (frame coordinates, verbatim) or RandR
  auto-detection (native pixels, scaled into the configured frame with
  even-snapped shared edges).

**Cost model:** every tier is a continuously-running encode (one NVENC
session or one x264 instance each) plus the archive. Example budget with
two monitors and the default ladder: (1 full + 2 screens) × 2 tiers + 1
archive = **7 encoder sessions** — inside the 8-session limit of consumer
GeForce GPUs (datacenter GPUs are unrestricted). This is why the ladder
default is two tiers, not four as in the webrtcsink era when idle tiers
were free.

---

## Browser client

`service/web/` is three static files — `index.html`, `style.css`, and the
WHEP client `app.js` — served same-origin; no bundle, no build step.

### Connection (WHEP)

1. Fetch `/config.json`; select the routed stream's tier list (URL path
   `/` → `fullTiers`, `/left` → that screen's tiers).
2. Pick the smallest tier whose dimensions still cover the rendered video
   element (`ResizeObserver` re-evaluates on resize, debounced 250 ms;
   `?tier=N` or `?whep=<url>` pins a tier and disables switching).
3. `RTCPeerConnection` with a `recvonly` video transceiver; wait for ICE
   gathering (1 s timeout guard); POST the SDP offer to
   `http://<host>:<webrtcPort>/<whepPath>/whep`; apply the SDP answer.
4. A `404` means the path exists but nothing is publishing (ffmpeg still
   starting or restarting) — the client polls every 2 s.
5. Teardown DELETEs the WHEP session URL (from the `Location` header) and
   closes the peer connection; connection failure/disconnect triggers
   automatic reconnect to the same tier.

### Metrics ("gumball")

The header shows FPS, RTT/2, resolution, average QP, freezes/min, jitter
buffer delay, codec, and hardware-decode status, sampled from
`RTCPeerConnection.getStats()` once per second over a 10 s rolling window —
unchanged from the previous stack (the stats API is standard WebRTC and
works identically over WHEP). The five-tier health classifier is also
unchanged; note that with the shared capped-CQ encode the top "lossless"
tier is effectively unreachable (the encoder holds QP ≈ `LIVE_CQ`), so a
healthy stream shows **visually-lossless** (green).

---

## HTTP Web Server

`web_server.py` serves on `WEB_PORT` (default 8080):

### `GET /<screen-name>`

Each configured screen path (`/top`, `/left`, `/screen1`, …) serves
`index.html`; the page picks its stream from `/config.json` by URL path.

### `GET /config.json`

The runtime config: `desktopName`, `width`, `height`, `framerate`,
`webrtcPort`, `fullTiers` (scale/width/height/whepPath), and `screens`
(name, path, geometry, tiers).

### `GET /archive?start=<ts>&end=<ts>` / `GET /archive?last=<duration>`

Zip of the segments overlapping the window. Completed segments are copied
byte-for-byte. The active segment (highest sequential name in the live dir)
is included when the window extends past the last completed segment — as a
plain byte-copy, since the fMP4's moov is at the front and a truncated
trailing fragment is ignored by players. The fd-based copy pins the inode,
so a rotation mid-copy cannot corrupt the download.

### `GET /video?start=<ts>&end=<ts>` / `GET /video?last=<duration>`

One faststart MP4 covering exactly the window: segments are laid on a
solid-color base track at their true temporal offsets (gaps filled with
`VIDEO_FILL_COLOR`), re-encoded at `VIDEO_QP` (defaults to `ARCHIVE_QP`).
Windows over 12 hours are rejected. At most `VIDEO_MAX_CONCURRENT`
(default 2) transcodes run at once — each is a full ffmpeg encode that
competes with the live encoders — and overflow requests receive
`503` + `Retry-After`.

Timestamps accept Unix epoch or ISO 8601 (UTC assumed when no timezone);
durations are `30s` / `60m` / `1.5h`.

---

## Environment Variable Reference

See [README.md — Configuration reference](README.md#configuration-reference)
for the authoritative table.

---

## Testing

| Suite | What it covers |
|---|---|
| `tests/` (pytest, pure Python) | unit tests only: config/tier math, ffmpeg argv builders, MediaMTX config, finalizer CSV handling, archive staging/zip, `/video` timeline assembly. No containers, no browser — `make test` |
| `functional-tests/` (Java + Selenium + testcontainers) | all container/browser integration coverage: endpoint availability, `/config.json` shape, WHEP reachability for every tier, junk-offer rejection, real browser playback, per-screen crop correctness (two-tone Xvfb), metrics population, end-to-end color-flip recording (live + `/archive` + `/video`), hub endpoints, evidence capture via Allure — `make functional` |
