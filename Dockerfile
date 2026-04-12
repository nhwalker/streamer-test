# ─── Build Stage ───────────────────────────────────────────────────────────────
# Compiles:
#   • libgstrswebrtc.so  (webrtcsink GStreamer plugin)
#   • gst-webrtc-signalling-server  (WebSocket signalling server binary)
#   • gstwebrtc-api JS bundle  (browser-side WebRTC client library)
FROM registry.access.redhat.com/ubi9:latest AS builder

# Layer A — OS build dependencies (rarely changes; outermost cache layer)
RUN dnf install -y --setopt=install_weak_deps=False \
        gstreamer1-devel \
        gstreamer1-plugins-base-devel \
        gstreamer1-plugins-bad-free-devel \
        libnice-devel \
        openssl-devel \
        gcc gcc-c++ pkg-config make git curl \
        nodejs npm \
    && dnf clean all

# Layer B — Rust toolchain (invalidated only on toolchain version bump)
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
    sh -s -- -y --default-toolchain 1.78.0 --no-modify-path --profile minimal

# Layer C — cargo-c (needed for `cargo cbuild` / `cargo cinstall`)
RUN cargo install cargo-c --version "^0.9" --locked

# Layer D — Clone gst-plugins-rs at pinned tag
# Pin version: 0.12.7 targets gstreamer-rs 0.22, requiring GStreamer >= 1.22
# UBI9 AppStream ships GStreamer 1.22.x — this tag is the correct match.
# To upgrade: bump tag to 0.13.x once UBI9 ships GStreamer >= 1.24.
ARG GST_PLUGINS_RS_TAG=0.12.7
RUN git clone --depth 1 --branch "${GST_PLUGINS_RS_TAG}" \
    https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git /src

# Layer E — Pre-fetch Cargo dependencies
# net/webrtc/ is its own workspace inside gst-plugins-rs; work from there.
WORKDIR /src/net/webrtc
RUN cargo fetch

# Layer F — Build the webrtcsink GStreamer plugin
# --jobs 2 prevents OOM on build machines with < 8 GB RAM; increase on larger hosts.
# Output .so: /opt/gst-rs/lib/gstreamer-1.0/libgstrswebrtc.so
RUN cargo cinstall -p gst-plugin-webrtc --release --jobs 2 \
        --prefix=/opt/gst-rs --libdir=/opt/gst-rs/lib \
    && echo "=== webrtcsink plugin files ===" \
    && find /opt/gst-rs -name "*.so" -o -name "*.pc" | sort

# Layer G — Build the standalone WebSocket signalling server
# The binary is produced at target/release/gst-webrtc-signalling-server
# relative to the net/webrtc workspace root.
RUN cargo build --release --jobs 2 --bin gst-webrtc-signalling-server \
    && install -m 755 target/release/gst-webrtc-signalling-server \
                       /opt/gst-webrtc-signalling-server

# Layer H — Build the gstwebrtc-api JavaScript bundle
# The dist/ directory will contain the browser-side JS library.
# Verify the output filename with: docker build --target builder ... && find /src/net/webrtc/gstwebrtc-api/dist
WORKDIR /src/net/webrtc/gstwebrtc-api
RUN npm ci && npm run build \
    && echo "=== gstwebrtc-api dist contents ===" \
    && ls -la dist/


# ─── Runtime Stage ─────────────────────────────────────────────────────────────
FROM registry.access.redhat.com/ubi9:latest

# GStreamer runtime + support tools
# gstreamer1-plugins-good  : ximagesrc (X11 capture), VP8/VP9 encoders
# gstreamer1-plugins-bad-free : webrtcbin (WebRTC engine used internally by webrtcsink)
# python3      : serves the web page (replace with nginx in production)
# nmap-ncat    : nc for the signalling server readiness probe in entrypoint.sh
RUN dnf install -y --setopt=install_weak_deps=False \
        gstreamer1 \
        gstreamer1-plugins-base \
        gstreamer1-plugins-good \
        gstreamer1-plugins-bad-free \
        libnice \
        openssl-libs \
        python3 \
        nmap-ncat \
    && dnf clean all

# Copy compiled webrtcsink plugin into the GStreamer plugin search path
# If the .so ends up in lib64/ on your build arch, adjust the source path here.
COPY --from=builder /opt/gst-rs/lib/gstreamer-1.0/ /usr/local/lib64/gstreamer-1.0/

# Copy the signalling server binary
COPY --from=builder /opt/gst-webrtc-signalling-server /usr/local/bin/gst-webrtc-signalling-server
RUN chmod +x /usr/local/bin/gst-webrtc-signalling-server

# Copy gstwebrtc-api JS bundle (served alongside index.html)
COPY --from=builder /src/net/webrtc/gstwebrtc-api/dist/ /var/www/html/gstwebrtc-api/

# Copy web page and startup scripts
COPY web/index.html  /var/www/html/index.html
COPY entrypoint.sh   /usr/local/bin/entrypoint.sh
COPY pipeline.sh     /usr/local/bin/pipeline.sh
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/pipeline.sh

# ── Environment defaults (all overridable at runtime via -e) ──────────────────
# GST_PLUGIN_PATH  : tells GStreamer where to find libgstrswebrtc.so
# DISPLAY          : X11 display to capture (mount /tmp/.X11-unix from host)
# STREAM_CODEC     : vp9 (no EPEL needed) | vp8 | h264 (requires EPEL + x264)
# STREAM_WIDTH/HEIGHT/FRAMERATE : capture resolution and frame rate
# STREAM_BITRATE_KBPS : target encode bitrate in kbps
# SIGNALLING_HOST/PORT : where the signalling server binds
# WEB_PORT         : port for the Python HTTP server serving index.html
# GST_WEBRTC_STUN_SERVER : optional STUN URI, e.g. stun://stun.l.google.com:19302
#                          Required when browser and container are on different hosts
#                          and --network=host is not used.
ENV GST_PLUGIN_PATH=/usr/local/lib64/gstreamer-1.0 \
    DISPLAY=:0 \
    STREAM_CODEC=vp9 \
    STREAM_WIDTH=1920 \
    STREAM_HEIGHT=1080 \
    STREAM_FRAMERATE=30 \
    STREAM_BITRATE_KBPS=2000 \
    SIGNALLING_HOST=0.0.0.0 \
    SIGNALLING_PORT=8443 \
    WEB_PORT=8080 \
    GST_WEBRTC_STUN_SERVER=""

EXPOSE 8080/tcp
EXPOSE 8443/tcp

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
