# Rewrite Plan: GStreamer/webrtcsink → ffmpeg + MediaMTX

Status: **draft for review — no implementation started**

This plan operationalizes the evaluation brief ("Rewriting a GStreamer WebRTC
Desktop Stream into ffmpeg + MediaMTX") against this repository, incorporating
the codebase review findings and the scoping decisions below.

## 1. Decisions (locked)

| # | Question | Decision |
|---|---|---|
| 1 | Archive subsystem | **In scope** — ported to ffmpeg in the same rewrite |
| 2 | Live quality mode | **Capped VBR, CQ 18** (`-rc vbr -cq 18 -maxrate <cap>`); code comments must document the fixed-CBR alternative and when to prefer it |
| 3 | Tier ladder | **Two tiers: 1.0 + 0.5** (default `WEBRTC_SCALE_LADDER=1.0,0.5`) |
| 4 | Per-screen splits | **Kept** — full frame + one cropped stream per monitor, as today |
| 5 | Base image | **UBI10** (RPM Fusion EL10 ffmpeg 7.1.5, already in the runtime image) |
| 6 | Process model | **Single ffmpeg process** for all live tiers + archive (one x11grab, `filter_complex` fan-out) |
| 7 | MediaMTX sourcing | **Vendored release binary** (pinned version + checksum in internal artifact store) |
| 8 | Audio | **Later phase** — video-only now, but command builder and MediaMTX paths designed so an Opus track maps in without restructuring |

Encode budget under these decisions (2 monitors): full×2 tiers + 2 screens×2
tiers + archive = **7 concurrent NVENC sessions** — inside the 8-session
consumer-GPU cap, comfortable on datacenter cards.

## 2. Target architecture

```
X11 (:0)
  │ x11grab (one capture, native resolution)
  ▼
ffmpeg (single process)
  filter_complex: split → [full 1.0][full 0.5][crop screenN → 1.0, 0.5] (+ format=nv12)
  ├─ live tiers   : h264_nvenc -rc vbr -cq 18 -maxrate $CAP -bufsize ... -g 30 -bf 0
  │                 → RTSP/TCP → rtsp://127.0.0.1:8554/<path>
  ├─ archive      : h264_nvenc quality-mode encode (see §5)
  │                 → -f segment, fragmented MP4 → ARCHIVE_LIVE_DIR
  ▼
MediaMTX (localhost)
  └─ WHEP egress per path → browser  (http://host:8889/<path>/whep)

web_server.py  : static page, /config.json, /archive, /video   (kept, simplified)
index.html     : WHEP client + tier auto-switch + stats gumball (reworked)
hub/           : unchanged
```

Path naming (replaces signalling ports): `desktop` and `desktop_half` for the
full frame, `<screen>` and `<screen>_half` per monitor (e.g. `left`,
`left_half`). `/config.json` carries the path list + WHEP URLs so the page
never hardcodes them.

### What is deleted outright

- `base/Containerfile` — the entire source-build stage: Rust toolchain,
  cargo-c, gst-plugins-rs 0.13.3 clone/pin, usrsctp cmake build, meson rebuild
  of gst-plugins-bad (nvcodec/webrtcbin/dtls/sctp/srtp), signalling-server
  cargo build.
- The js-builder stage (npm build of gstwebrtc-api).
- All GStreamer runtime packages (gstreamer1*, libnice*, libsrtp, libsodium,
  zvbi, libwebp, gtk4) and the `/usr/lib64/gstreamer-1.0` plugin overwrites.
- `gst-webrtc-signalling-server` and the one-server-per-tier port scheme
  (`SIGNALLING_PORT`, `SIGNALLING_PORT_STRIDE` become deprecated no-ops that
  log a warning).
- In `web_server.py`: the mdat-walker + AVCC→Annex-B remux
  (`_iter_active_mdats`, `_avcc_to_annex_b`, `_copy_active_to_stage`) — the
  ffmpeg archive writes `empty_moov` fragmented MP4 that is parseable
  mid-write, so the active segment is served by copy/`-c copy` like any other.

### What is kept

- `desktop_config.py` — RandR auto-detection, splits, tier math. Ports drop
  out; MediaMTX path names and WHEP URLs go in.
- `web_server.py` routes `/config.json`, `/archive`, `/video`, per-screen
  pages; `video_transcode.py`; `archive_times.py`; `archive_purge.py`.
- `hub/` unchanged.
- Podman CDI GPU injection unchanged (`libnvidia-encode.so.1` + `libcuda.so.1`
  are exactly what `h264_nvenc` dlopens).

## 3. Live encoding parameters

Per tier (values via env, defaults shown):

```
-c:v h264_nvenc -preset p4 -tune ll
-rc vbr -cq ${LIVE_CQ:-18} -b:v 0
-maxrate ${LIVE_MAXRATE:-<provisioned cap>} -bufsize ${LIVE_BUFSIZE:-2×maxrate}
-g 30 -bf 0 -profile:v main -pix_fmt yuv420p
-spatial-aq 1 -temporal-aq 1
```

- **`-g 30` (1 s GOP) is a hard requirement**, not a tuning nicety: MediaMTX
  cannot request keyframes from an RTSP publisher (verified against its
  source), so the GOP directly sets worst-case viewer join/recovery latency.
- CQ 18 reproduces today's "visually lossless when possible" behavior; the
  cap absorbs motion bursts as transient clarity dips.
- The command builder must carry a comment block documenting the **fixed-CBR
  alternative** (`-rc cbr -b:v N`) per decision #2: choose CBR when the
  provisioned link must never see bitrate variance, at the cost of visibly
  lower static-content quality.
- No-GPU fallback: `libx264 -preset ultrafast -tune zerolatency -crf 18
  -maxrate/-bufsize` (RPM Fusion ffmpeg includes libx264). VP9 default is
  retired; live codec is H.264 everywhere.
- Audio (later phase): each RTSP output gains `-map` of a single shared
  `libopus` encode; MediaMTX carries Opus natively. The builder should emit
  per-output map lists from day one so this is additive.

## 4. MediaMTX

- Pin a release (latest stable at implementation time), store the linux
  tarball + checksum in the internal artifact store, `COPY` into the image.
  Upgrade procedure = replace one file. (Source-build was rejected:
  `go install` is broken by design — `go:embed` files are produced by
  `go generate`, which itself downloads hls.js from GitHub.)
- Generated `mediamtx.yml` (from desktop_config at entrypoint): RTSP ingest on
  127.0.0.1:8554 only, WebRTC/WHEP on :8889, HLS/RTMP/SRT/API disabled,
  `webrtcAdditionalHosts` from env for the provisioned-network ICE case.
- Document required ports: TCP 8889 (WHEP HTTP) + UDP 8189 (ICE), replacing
  the 8443/8543/8643/8743 signalling ranges.

## 5. Archive port

- Same encoder quality modes, translated (`archive_encoder.py` keeps its
  pure-planner shape, now emitting ffmpeg args):
  - `visually-lossless` → `h264_nvenc -rc constqp -qp 18 -preset p6 -bf 2 -g 30`
  - `lossless` → `-rc constqp -qp 0` (or `-tune lossless`)
  - `legacy` → `-rc vbr -b:v $ARCHIVE_BITRATE -maxrate $ARCHIVE_BITRATE`
  - x264 fallbacks mirror current settings.
- Output: `-f segment -segment_time $ARCHIVE_SEGMENT_SEC -reset_timestamps 1
  -segment_format_options movflags=+frag_keyframe+empty_moov+default_base_moof`
  writing sequential names into `ARCHIVE_LIVE_DIR`, plus
  `-segment_list` (CSV) for rotation detection.
- A small finalize watcher (reusing the existing worker-thread/`.part`-rename
  logic from `pipeline.py`) tails the segment list and renames completed
  segments into `ARCHIVE_DIR` with the same start/end-timestamp naming
  (`archive_times.renamed_segment_path` unchanged). `/archive`, `/video`,
  purge, and the functional-test contract are unchanged from the outside.
- Because segments are `empty_moov` fMP4, the in-progress file is parseable:
  `stage_segments` serves it via plain copy or `ffmpeg -c copy`, deleting the
  mdat-walker path.

## 6. Web page

- Replace gstwebrtc-api with a small WHEP client (vendored npm package or
  ~100-line hand-rolled POST-offer/receive-answer; no signalling WebSocket).
- Keep the tier auto-switch UX: ResizeObserver picks the smallest adequate
  tier from `/config.json` and reconnects to that path's WHEP endpoint
  (`?tier=` / `?signalling=` pins get WHEP-equivalent query params).
- Keep the stats gumball (standard `getStats()` works over WHEP). Retune the
  health classifier: top "lossless" tier no longer applies (shared encode
  never hits QP 0); "visually lossless" maps to QP ≤ ~18 under the cap.

## 7. Phases

**Phase 0 — Spike / measurement (no repo changes beyond a scratch dir).**
Hand-run ffmpeg + vendored MediaMTX in a UBI10 container on a GPU host.
Measure: glass-to-glass latency vs. current stack, viewer join time with
`-g 30`, CPU cost of swscale downscales at target resolution (the current
pipeline is GPU-resident for scaling; this is the known efficiency
regression), NVENC session count/utilization, behavior when a viewer's link
degrades. Exit criteria: latency acceptable, CPU headroom acceptable at
2 monitors × 2 tiers, WHEP works in the target browsers.

**Phase 1 — Container + process supervision.**
New single-stage `service/Containerfile` (UBI10 + RPM Fusion ffmpeg +
vendored MediaMTX; no `base/` dependency). Entrypoint: config → MediaMTX →
web_server → ffmpeg, with restart-on-exit supervision for ffmpeg (single
process is a deliberate availability trade-off — a crash drops live + archive
together until restart; the supervisor plus MediaMTX's tolerance of publisher
reconnects bounds the gap). `pipeline.py` is replaced by a pure, unit-testable
ffmpeg command builder module (same style as `archive_encoder.py`).

**Phase 2 — Archive port** (§5). Acceptance: existing archive unit tests
(adapted) plus `ArchiveEndpointTest`/`VideoEndpointTest` functional tests pass
unmodified in their assertions.

**Phase 3 — Web page** (§6). Acceptance: live-feed color functional tests
pass; tier switch on resize demonstrated; gumball shows sane values.

**Phase 4 — Tests + CI.** Update `tests/` unit suite (config, command
builder, finalize watcher), `ServiceStack` port/env wiring, and
`.github/workflows/container-build.yml` (base-image build step disappears —
CI gets dramatically shorter).

**Phase 5 — Cleanup + docs.** Delete `base/`, GStreamer code paths, and
rewrite README/PIPELINE.md around the new stack. Document deprecated env vars
(`STREAM_CODEC`, `SIGNALLING_PORT*`, `WEBRTC_MIN/START/MAX_BITRATE`) and the
new ones (`LIVE_CQ`, `LIVE_MAXRATE`, `LIVE_BUFSIZE`, MediaMTX ports).

Rollback: phases land on a branch; the current image remains buildable until
Phase 5 merges. The functional-test suite is the regression bar throughout.

## 8. Risks

| Risk | Mitigation |
|---|---|
| swscale CPU cost at multi-monitor resolutions (no `scale_cuda` in RPM Fusion build) | Phase 0 measures it; ladder already trimmed to 2 tiers; can drop the 0.5 tier per screen if needed |
| Single-process stall freezes all outputs incl. archive | Accepted (decision #6); supervisor restart + RTSP/TCP on loopback makes triggers rare; revisit split if Phase 0 shows instability |
| Viewer join blank-time = GOP length | `-g 30` mandated; verify in Phase 0 |
| Latency regression vs. in-process webrtcsink | Phase 0 exit criterion; `-tune ll`/`ull` and MediaMTX buffer knobs are the levers |
| Shared encode: one quality for all viewers, no per-viewer adaptation | Accepted by decisions #2/#3 (provisioned network); CQ-under-cap keeps static quality high |
| MediaMTX version drift (vendored binary) | Pin + checksum; upgrade is a one-file change; subscribe to release notes |

## 9. Open items to settle during Phase 0

1. Concrete `LIVE_MAXRATE` default — needs the real provisioned per-viewer
   bandwidth figure.
2. Latency target number (brief §12.3) — measured against the current stack
   side-by-side.
3. MediaMTX release version to pin.
4. Whether `desktop_half` tiers keep enough text legibility at CQ 18 under
   the cap to be useful, or the ladder becomes full-res only.
