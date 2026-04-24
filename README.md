# X11 Desktop Streaming via WebRTC

Streams a Linux desktop (X11) to any modern web browser in real time, with sub-second latency and efficient video compression. Packaged as a single container image based on Red Hat UBI 10.

---

## Table of Contents

1. [How it works — the 30-second version](#how-it-works--the-30-second-version)
2. [Technology primer](#technology-primer)
3. [Architecture](#architecture)
4. [Container internals](#container-internals)
5. [Build process](#build-process)
6. [Running the container](#running-the-container)
7. [NVIDIA GPU encoding](#nvidia-gpu-encoding)
8. [Configuration reference](#configuration-reference)
9. [Verifying it works](#verifying-it-works)
10. [Production upgrade path](#production-upgrade-path)

---

## How it works — the 30-second version

```
Host desktop (X11)
      │  screen pixels
      ▼
 GStreamer pipeline  ──encodes──►  webrtcsink
      │                                 │
      │                        WebRTC signalling
      │                                 │
      ▼                                 ▼
 Container exposes               Browser opens
 two ports:                      http://host:8080
   :8080  web page               and receives live
   :8443  signalling             video stream
```

The desktop screen is captured as a stream of raw video frames, compressed using the VP9 codec, and delivered to the browser over WebRTC — the same protocol used by Google Meet and Zoom. The browser needs no plugin; WebRTC is built into every modern browser.

---

## Technology primer

### GStreamer

GStreamer is a pipeline-based multimedia framework. You assemble a chain of *elements* — each one does one job — and GStreamer moves data between them:

```
[capture] → [resize] → [encode] → [send]
```

Elements are linked with `!` on the command line. For example:

```
ximagesrc ! videoscale ! vp9enc ! ...
```

### WebRTC

WebRTC (Web Real-Time Communication) is an open standard built into all modern browsers that enables peer-to-peer audio/video streaming. Key properties:

- **Low latency** — typically under 500 ms end-to-end
- **Adaptive bitrate** — automatically adjusts quality to available bandwidth
- **Encrypted** — all media is encrypted in transit (DTLS-SRTP)
- **No plugin required** — supported natively in Chrome, Firefox, Safari, Edge

WebRTC requires a **signalling server** to help the two peers (our GStreamer pipeline and the browser) find each other and agree on connection parameters. Once connected, media flows directly between them.

### webrtcsink

`webrtcsink` is a GStreamer *sink element* (a pipeline endpoint) from the [gst-plugins-rs](https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs) project — a collection of GStreamer plugins written in Rust. It handles the entire WebRTC stack:

- Codec negotiation with the browser (decides on VP9, VP8, or H.264)
- Encoding the video
- Setting up the encrypted WebRTC connection
- Sending the compressed video to one or more browser peers simultaneously

From a pipeline perspective, `webrtcsink` is just another element you connect to — but internally it manages all the WebRTC complexity.

### gst-plugins-rs

The official GStreamer project ships additional plugins written in Rust, collected in the `gst-plugins-rs` repository. These are not yet packaged in most Linux distributions, so this project compiles them from source during the Docker build.

### Signalling server

Before two WebRTC peers can exchange video, they need to exchange a small amount of metadata:

- **SDP** (Session Description Protocol) — describes what codecs and formats each side supports
- **ICE candidates** — lists of network addresses through which each peer can be reached

The signalling server (`gst-webrtc-signalling-server`) is a lightweight WebSocket server that acts as a message broker for this exchange. It does **not** carry video — only the setup handshake. Once the peers are connected, the server plays no further role.

### UBI 10 (Universal Base Image)

Red Hat's UBI 10 is a freely redistributable container base image derived from Red Hat Enterprise Linux 10. It provides a stable, enterprise-grade foundation with a consistent package set and long-term security support — suitable for production deployments. RHEL 10 ships GStreamer 1.24.x, which drives the version pinning for the Rust plugins described in the [Build process](#build-process) section.

---

## Architecture

### System overview

```mermaid
graph TD
    subgraph host["Host Machine (Linux)"]
        X11["X11 Display Server\n(:0)"]
        sock["/tmp/.X11-unix\n(Unix socket)"]
        X11 -->|"exposes"| sock
    end

    subgraph container["Docker Container (UBI 10)"]
        direction TB
        gst["GStreamer Pipeline\nximagesrc → encode → webrtcsink"]
        sig["Signalling Server\ngst-webrtc-signalling-server\nws://0.0.0.0:8443"]
        web["Web Server\npython3 http.server\nhttp://0.0.0.0:8080"]
        html["/var/www/html/\nindex.html + gstwebrtc-api.js"]

        gst <-->|"WebSocket\nSDP + ICE"| sig
        web -->|"serves"| html
    end

    subgraph browser["Browser"]
        page["index.html\n(gstwebrtc-api.js)"]
        video["&lt;video&gt; element\nlive desktop stream"]
        page -->|"renders"| video
    end

    sock -->|"mounted ro\n-v /tmp/.X11-unix"| gst
    web -->|"HTTP :8080"| page
    sig <-->|"WebSocket :8443\nSDP + ICE handshake"| page
    gst -->|"WebRTC media\n(VP9 over UDP)"| video
```

### Media flow (frame-by-frame)

```mermaid
sequenceDiagram
    participant X11 as X11 Display
    participant GST as GStreamer Pipeline
    participant SIG as Signalling Server
    participant BR as Browser

    BR->>SIG: Connect (WebSocket)
    SIG-->>BR: Assign peer ID

    Note over GST,SIG: webrtcsink detects new peer
    GST->>SIG: SDP Offer (codec list)
    SIG->>BR: Forward SDP Offer
    BR->>SIG: SDP Answer (chosen codec: VP9)
    SIG->>GST: Forward SDP Answer

    GST->>SIG: ICE candidates (network addresses)
    SIG->>BR: Forward ICE candidates
    BR->>SIG: ICE candidates
    SIG->>GST: Forward ICE candidates

    Note over GST,BR: WebRTC connection established (DTLS handshake)

    loop Every frame (~33 ms at 30 fps)
        X11->>GST: Raw pixels (via /tmp/.X11-unix)
        GST->>GST: Scale to target resolution
        GST->>GST: Encode with VP9
        GST->>BR: Compressed frame (SRTP over UDP)
        BR->>BR: Decode + display in &lt;video&gt;
    end
```

### GStreamer pipeline

```mermaid
flowchart LR
    A["ximagesrc\nCaptures X11\nscreen pixels"] -->|"raw video"| B
    B["videorate\nEnforces\ntarget FPS"] -->|"timed frames"| C
    C["videoscale\nResizes to\ntarget resolution"] -->|"scaled frames"| D
    D["videoconvert\nNormalises\npixel format"] -->|"I420 frames"| E
    E["webrtcsink\nNegotiates codec\nEncodes + sends\nvia WebRTC"]

    style A fill:#2a4a6b,color:#fff
    style E fill:#3a5a2b,color:#fff
```

---

## Container internals

```mermaid
graph TD
    subgraph runtime["Runtime Container"]
        direction TB
        ep["/usr/local/bin/entrypoint.sh"]
        pl["/usr/local/bin/pipeline.py"]
        ss["/usr/local/bin/gst-webrtc-signalling-server"]
        plug["/usr/local/lib/gstreamer-1.0/\nRust plugins (libgstrswebrtc.so, …)\nlibgstnvcodec.so"]
        rep["/usr/lib64/gstreamer-1.0/\nlibgstwebrtc.so (replaced)\nlibgstdtls/sctp/srtp.so (replaced)"]
        www["/var/www/html/\nindex.html\ngstwebrtc-api/gstwebrtc-api.js"]
        sys["System GStreamer\n(gstreamer1-plugins-good\ngstreamer1-plugins-bad-free)"]

        ep -->|"starts"| ss
        ep -->|"starts"| www
        ep -->|"execs"| pl
        pl -->|"gst-launch-1.0"| plug
        plug -->|"uses"| rep
        plug -->|"uses"| sys
    end
```

### Startup sequence

```mermaid
sequenceDiagram
    participant D as Docker
    participant E as entrypoint.sh
    participant S as Signalling Server
    participant W as Web Server (python3)
    participant P as pipeline.py / gst-launch-1.0

    D->>E: container start
    E->>E: X11 pre-flight check\n(1-frame probe via ximagesrc)
    E->>S: start in background (:8443)
    E->>E: readiness probe (nc -z :8443, max 2s)
    E->>W: start in background (:8080)
    E->>P: exec (replaces shell)
    P->>P: build gst-launch-1.0 command\nfrom env vars
    P-->>D: gst-launch-1.0 running\n(foreground process)
```

---

## Build process

The container uses a two-stage build to keep the final image small. The build stage (~5 GB, discarded after build) compiles everything from source. The runtime stage (~400 MB) contains only what is needed to run.

```mermaid
flowchart TD
    subgraph build["Build Stage (UBI 10 + Rust + Meson + Node.js)"]
        direction TB
        A["A: Install OS build deps\n(GStreamer 1.24 devel, meson, cmake,\nnpm, libsrtp-devel, libnice-devel)"]
        A2["A2: Build usrsctp from source\n(cmake — not packaged in RHEL10/EPEL10)\n→ static lib linked into libgstsctp-1.0.so"]
        B["B: Install Rust toolchain\n(rustup stable + cargo-c)"]
        C["C: Clone + prefetch gst-plugins-rs @ 0.13.3\n(cargo fetch)"]
        D["D: Build all Rust plugins\n(cargo cinstall each cdylib)\n→ /opt/gst-rs/lib/gstreamer-1.0/*.so"]
        E["E: Build signalling server\n(cargo build --bin gst-webrtc-signalling-server)\n→ /opt/gst-webrtc-signalling-server"]
        F["F: Build gstwebrtc-api JS bundle\n(npm install && npm run build)\n→ /opt/gstwebrtc-api/"]
        G["G: Build GStreamer monorepo subset\n(meson: nvcodec + webrtcbin + dtls + sctp + srtp)\n→ /opt/gst-nvcodec/"]

        A --> A2
        A --> B --> C --> D --> E
        C --> F
        A2 --> G
        A --> G
    end

    subgraph runtime["Runtime Stage (UBI 10)"]
        direction TB
        H["Install system GStreamer packages\n(plugins-good, plugins-bad-free,\nlibnice, libnice-gstreamer1, libsrtp)"]
        I["COPY Rust plugins + nvcodec\n→ /usr/local/lib/gstreamer-1.0/\n(new — no system equivalent)"]
        J["REPLACE system webrtcbin/dtls/sctp/srtp\n→ /usr/lib64/gstreamer-1.0/\n(overwrites broken RHEL10 builds)"]
        K["COPY signalling server binary\n→ /usr/local/bin/"]
        L["COPY gstwebrtc-api + index.html\n→ /var/www/html/"]
        M["COPY entrypoint.sh + pipeline.py\n→ /usr/local/bin/"]

        H --> I --> J --> K --> L --> M
    end

    D -->|"COPY .so files"| I
    G -->|"COPY libgstnvcodec.so"| I
    G -->|"COPY + overwrite"| J
    E -->|"COPY binary"| K
    F -->|"COPY dist/"| L
```

### Why compile from source?

Four distinct components require source builds; the reasons are different in each case.

| Component | Why not use a package? |
|---|---|
| **gst-plugins-rs** (webrtcsink + all Rust plugins) | Never packaged for RHEL. No EPEL or Rocky equivalent exists. |
| **usrsctp** | Not packaged in EPEL10 or Rocky 10. Required by GStreamer's SCTP plugin for WebRTC data channels. |
| **GStreamer nvcodec** | NVIDIA hardware encoders are not included in any RHEL10 package. |
| **GStreamer webrtcbin, dtls, sctp, srtp** | RHEL10's `gstreamer1-plugins-bad-free` is compiled **without libsrtp2** — DTLS-SRTP negotiation silently fails, making the packaged webrtcbin non-functional for WebRTC. |

**The webrtcbin problem in detail.** Red Hat builds `gstreamer1-plugins-bad-free` without `libsrtp2` because libsrtp is absent from RHEL10's base repositories (it lives in EPEL10, which Red Hat does not depend on during package builds). Without SRTP support, webrtcbin cannot complete DTLS negotiation — every incoming WebRTC session is silently dropped. The pipeline starts and appears healthy but no browser ever receives a frame. Building webrtcbin from the GStreamer monorepo source with `-Dsrtp=enabled -Ddtls=enabled -Dsctp=enabled` (and usrsctp statically linked in via the preceding cmake step) produces a fully functional WebRTC stack. The resulting `.so` files are copied over their system counterparts in `/usr/lib64/gstreamer-1.0/`, along with the rebuilt `libgstwebrtc-1.0.so` and `libgstwebrtcnice-1.0.so` companion libraries that the RHEL10 package omits entirely (it was built without libnice).

**usrsctp.** The user-space SCTP library is the SCTP implementation GStreamer's data-channel code links against. It is absent from both EPEL10 and Rocky 10. Building it as a static library (`-Dsctp_build_shared_lib=OFF`) means the rebuilt `libgstsctp-1.0.so` bundles everything it needs with no new runtime dependency.

**gst-plugins-rs.** The `webrtcsink` element and the WebSocket signalling server both live here. The Rust toolchain makes the build straightforward: `cargo cinstall` compiles every `cdylib` target in the workspace and drops the resulting `.so` files into the GStreamer plugin search path. Plugins whose native library dependencies are unavailable in RHEL10 (e.g. `gst-plugin-csound`) are silently skipped by the build loop without failing the overall build.

### Why pin to gst-plugins-rs 0.13.3?

The Rust GStreamer bindings (`gstreamer-rs`) must match the C GStreamer version on the system. RHEL10/UBI10 ships GStreamer 1.24.x. `gst-plugins-rs` 0.13.x targets `gstreamer-rs 0.23`, which requires GStreamer ≥ 1.24 — a precise match. The tag is set via the `GST_PLUGINS_RS_TAG` build argument; bump it to `0.14.x` once RHEL10 ships GStreamer ≥ 1.26.

The GStreamer monorepo is cloned at the exact version reported by `pkg-config --modversion gstreamer-1.0` in the builder, so the rebuilt plugins are always ABI-compatible with the system GStreamer libraries.

---

## Running the container

### Prerequisites

- Docker on a Linux host with an active X11 display
- The host display must accept connections from the container

```bash
# Allow the container to connect to the host X display
xhost +local:docker
```

### Build

```bash
docker build -t x11-webrtc-streamer .
```

The first build takes 20–40 minutes (Rust compilation). Subsequent builds use Docker layer cache and complete in seconds unless source dependencies change.

### Run

```bash
docker run --rm \
  --network=host \
  -e DISPLAY=:0 \
  -v /tmp/.X11-unix:/tmp/.X11-unix:ro \
  x11-webrtc-streamer
```

Then open **http://localhost:8080** in a browser.

> **Why `--network=host`?**
> WebRTC uses UDP for media. When the browser and container are on the same machine, `--network=host` lets both sides discover the same host network interfaces during ICE negotiation, so they connect directly without needing a STUN relay server. Without `--network=host`, you must supply a STUN server (see `GST_WEBRTC_STUN_SERVER` below).

### Using Xauthority (alternative to xhost)

If you prefer not to use `xhost`, mount the X authority file instead:

```bash
docker run --rm \
  --network=host \
  -e DISPLAY=:0 \
  -e XAUTHORITY=/root/.Xauthority \
  -v /tmp/.X11-unix:/tmp/.X11-unix:ro \
  -v "$HOME/.Xauthority:/root/.Xauthority:ro" \
  x11-webrtc-streamer
```

---

## NVIDIA GPU encoding

When the container runs on a host with an NVIDIA GPU and [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html), it can use NVENC hardware encoding for H.264 and H.265. This offloads video encoding from the CPU to the GPU's dedicated encoder, freeing CPU cores and often improving quality at the same bitrate.

### How it works

The container image includes the GStreamer `nvcodec` plugin, which provides `nvh264enc` and `nvh265enc` encoder elements. The plugin uses `dlopen` to load NVIDIA driver libraries at runtime:

- **With a GPU** (`--gpus all`): nvidia-container-toolkit injects the NVIDIA driver libraries (`libcuda.so`, `libnvidia-encode.so`). The nvcodec plugin loads successfully, and `webrtcsink` automatically prefers the hardware encoders over software ones.
- **Without a GPU**: The plugin cannot load the NVIDIA libraries, so GStreamer silently skips it. Everything works exactly as before (VP9/VP8 software encoding).

### Prerequisites

- NVIDIA GPU with NVENC support (Kepler or newer — GTX 600+, Tesla T4/A10/L4, etc.)
- NVIDIA driver installed on the host
- [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) installed and configured

### Running with GPU

```bash
docker run --rm --gpus all \
  --network=host \
  -e DISPLAY=:0 \
  -e STREAM_CODEC=h264 \
  -v /tmp/.X11-unix:/tmp/.X11-unix:ro \
  x11-webrtc-streamer
```

H.265 is also supported (better compression, but limited browser support — see note below):

```bash
docker run --rm --gpus all \
  --network=host \
  -e DISPLAY=:0 \
  -e STREAM_CODEC=h265 \
  -v /tmp/.X11-unix:/tmp/.X11-unix:ro \
  x11-webrtc-streamer
```

> **H.265 browser support**: H.265 (HEVC) over WebRTC is supported in Chrome and Edge but **not in Firefox**. Use H.264 for maximum browser compatibility, or VP9 if you don't need GPU encoding.

### Verifying GPU encoding

```bash
# Check that the nvcodec plugin loaded and nvh264enc is available
docker run --rm --gpus all x11-webrtc-streamer \
  gst-inspect-1.0 nvh264enc

# During streaming, monitor GPU encoder utilization on the host
nvidia-smi dmon -s u -d 1
```

The container logs will also show GPU detection at startup:
```
[entrypoint] NVIDIA GPU detected:
  Tesla T4, 535.129.03, 15360 MiB
[pipeline] NVIDIA NVENC detected: hardware encoding available (nvh264enc, nvh265enc)
```

---

## Configuration reference

All settings are environment variables passed to `docker run -e`:

| Variable | Default | Description |
|---|---|---|
| `DISPLAY` | `:0` | X11 display to capture |
| `STREAM_CODEC` | `vp9` | Video codec: `vp9`, `vp8`, `h264`, or `h265`\* |
| `STREAM_WIDTH` | `1920` | Capture width in pixels |
| `STREAM_HEIGHT` | `1080` | Capture height in pixels |
| `STREAM_FRAMERATE` | `30` | Frames per second |
| `SIGNALLING_HOST` | `0.0.0.0` | Network interface for the signalling server |
| `SIGNALLING_PORT` | `8443` | Port for the WebSocket signalling server |
| `WEB_PORT` | `8080` | Port for the HTTP page server |
| `GST_WEBRTC_STUN_SERVER` | _(empty)_ | STUN server URI, e.g. `stun://stun.l.google.com:19302` |
| `GST_WEBRTC_TURN_SERVER` | _(empty)_ | TURN relay URI, e.g. `turn://user:pass@host:3478` — applied per-consumer via the `add-turn-server` signal |

\* H.264 and H.265 use NVENC hardware encoding when a GPU is available (`--gpus all`). Without a GPU, H.264 falls back to software encoding via `gstreamer1-plugins-ugly` (x264), which is included in the runtime stage via RPM Fusion Free. H.265 WebRTC is supported in Chrome/Edge but not Firefox.

### Example: 720p stream

```bash
docker run --rm --network=host \
  -e DISPLAY=:0 \
  -v /tmp/.X11-unix:/tmp/.X11-unix:ro \
  -e STREAM_WIDTH=1280 \
  -e STREAM_HEIGHT=720 \
  x11-webrtc-streamer
```

### Example: access the stream from another machine

When the browser is not on the same host as the container, remove `--network=host` and provide a STUN server so both sides can discover each other's public IP:

```bash
docker run --rm \
  -p 8080:8080 \
  -p 8443:8443 \
  -e DISPLAY=:0 \
  -v /tmp/.X11-unix:/tmp/.X11-unix:ro \
  -e GST_WEBRTC_STUN_SERVER=stun://stun.l.google.com:19302 \
  x11-webrtc-streamer
```

Then open `http://<server-ip>:8080?stun=stun.l.google.com:19302` in the browser (the `?stun=` parameter tells the browser's WebRTC stack to use the same STUN server).

---

## Verifying it works

**1 — Check the plugin loaded correctly:**

```bash
docker run --rm x11-webrtc-streamer gst-inspect-1.0 webrtcsink
```

Expected: a long property list including `video-caps` and `signaller`.

**2 — Smoke test without X11 (synthetic video):**

```bash
docker run --rm --network=host x11-webrtc-streamer \
  gst-launch-1.0 videotestsrc pattern=ball \
  ! webrtcsink run-signalling-server=true run-web-server=true
```

Open `https://localhost:9090` (accept the self-signed certificate warning). A bouncing ball should appear.

**3 — Full X11 stream:**

```bash
xhost +local:docker
docker run --rm --network=host \
  -e DISPLAY=:0 \
  -v /tmp/.X11-unix:/tmp/.X11-unix:ro \
  x11-webrtc-streamer
# Open http://localhost:8080
```

**4 — Performance check:**

```bash
docker stats <container-name>
```

VP9 software encoding at 1080p30 typically uses 100–200% CPU (1–2 cores) on modern hardware. If CPU is a concern, reduce resolution or switch to VP8 (`STREAM_CODEC=vp8`), which is cheaper to encode.

---

## Production upgrade path

The PoC uses simple in-process components for convenience. Each can be swapped independently for production:

```mermaid
flowchart LR
    subgraph poc["PoC (this repo)"]
        A1["ximagesrc\n(X11 capture)"]
        B1["webrtcsink"]
        C1["gst-webrtc-signalling-server\n(embedded WebSocket)"]
        D1["python3 http.server\n(serves index.html)"]
    end

    subgraph prod["Production options"]
        A2["pipewiresrc\n(Wayland / PipeWire)"]
        B2["webrtcsink + WHIP signaller\n(HTTP-based, RFC standard)"]
        C2["LiveKit / Janus / mediasoup\n(SFU — scales to 1000s of viewers)"]
        D2["nginx / CDN\n(serves web page)"]
    end

    A1 -.->|"swap source element"| A2
    B1 -.->|"change signaller= property"| B2
    C1 -.->|"replace binary"| C2
    D1 -.->|"replace web server"| D2
```

### Signalling server (Day 2)

Deploy `gst-webrtc-signalling-server` as its own container. Set `SIGNALLING_PORT` in the streaming container to point at it. No other changes needed.

### WHIP (standard WebRTC ingest)

[WHIP](https://www.ietf.org/archive/id/draft-ietf-wish-whip-01.txt) is an HTTP-based WebRTC ingest standard supported by Cloudflare Stream, Janus, LiveKit, and others. Switch `webrtcsink` to use it by changing a single property — no pipeline restructuring:

```bash
# In pipeline.py, replace the signaller properties with:
webrtcsink signaller::uri="https://your-sfu.example.com/whip/ingest" \
           signaller=whipsink
```

### Scaling to many viewers

`webrtcsink` manages multiple browser peers natively. For very large deployments (hundreds of simultaneous viewers), introduce a Selective Forwarding Unit (SFU) such as LiveKit or Janus between `webrtcsink` and the browsers. The SFU receives one stream from the container and fans it out to viewers, reducing upstream bandwidth from the capture host.

### H.264 / H.265 hardware encoding

The container image includes the GStreamer nvcodec plugin. When run with `--gpus all` (requires nvidia-container-toolkit on the host), NVENC hardware encoding is used automatically. Set `STREAM_CODEC=h264` or `STREAM_CODEC=h265` at runtime.

For software H.264 encoding without a GPU, add EPEL and `gstreamer1-plugins-ugly` to the runtime stage of the Dockerfile. For VAAPI hardware encoding on Intel/AMD GPUs, pass `--device /dev/dri` to `docker run`.
