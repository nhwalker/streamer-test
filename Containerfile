# ─── Build Stage ─────────────────────────────────────────────────────────────
FROM ubuntu:24.04 AS builder

ARG DEBIAN_FRONTEND=noninteractive

# Enable universe + multiverse so all GStreamer dev packages are reachable.
# Handles both the new DEB822 format (ubuntu.sources) and the legacy format.
RUN sed -i \
        's/Components: main restricted$/Components: main restricted universe multiverse/' \
        /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || \
    sed -i \
        's/main restricted$/main restricted universe multiverse/' \
        /etc/apt/sources.list

# ── A: OS build dependencies ─────────────────────────────────────────────────
# GStreamer dev headers, optional library deps for as many gst-plugins-rs
# packages as Ubuntu 24.04 apt can satisfy, plus Node.js for the JS bundle.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential pkg-config git curl ca-certificates \
        meson ninja-build python3 \
        # GStreamer development headers (core + all plugin sets)
        libgstreamer1.0-dev \
        libgstreamer-plugins-base1.0-dev \
        libgstreamer-plugins-bad1.0-dev \
        # Node.js — builds the gstwebrtc-api browser-side JS library
        nodejs npm \
        # Optional library deps — each enables additional Rust plugin packages
        libssl-dev \
        libnice-dev \
        libdav1d-dev \
        libsodium-dev \
        libzvbi-dev \
        libwebp-dev \
        libgtk-4-dev \
        libcsound64-dev \
    && rm -rf /var/lib/apt/lists/*

# ── B: Rust toolchain ────────────────────────────────────────────────────────
# Rustup gives us the latest stable, sidestepping any MSRV gap between Ubuntu
# 24.04's packaged Rust and gst-plugins-rs's minimum supported Rust version.
# cargo-c is built from crates.io to match the installed cargo version.
RUN curl -sSf https://sh.rustup.rs | \
        sh -s -- -y --default-toolchain stable --no-modify-path \
    && /root/.cargo/bin/cargo install cargo-c
ENV PATH="/root/.cargo/bin:${PATH}"

# ── C: Clone gst-plugins-rs ──────────────────────────────────────────────────
# Tag series 0.13.x requires GStreamer >= 1.24 — matches Ubuntu 24.04 packages.
# Bump to 0.14.x once Ubuntu ships GStreamer >= 1.26.
ARG GST_PLUGINS_RS_TAG=0.13.3
RUN git clone --depth 1 --branch "${GST_PLUGINS_RS_TAG}" \
        https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git /src

WORKDIR /src

# ── D: Pre-fetch all Cargo dependencies offline ──────────────────────────────
RUN cargo fetch

# ── E: Build and install every GStreamer Rust plugin ─────────────────────────
# Iterates over all cdylib targets in the workspace and installs each one,
# printing SKIP for any that fail (e.g. gst-plugin-skia, which requires a
# pre-built Skia library not available in Ubuntu 24.04 apt).
# --jobs 2 prevents OOM on build agents with < 8 GB RAM.
RUN PKGS=$(cargo metadata --format-version 1 --no-deps | python3 -c \
    'import sys,json;d=json.load(sys.stdin);print(" ".join(p["name"] for p in d["packages"] if any("cdylib" in t.get("crate_types",[]) for t in p["targets"])))') \
    && echo "=== Rust plugin packages to build ===" \
    && echo "$PKGS" | tr ' ' '\n' \
    && INSTALLED=0; FAILED=0 \
    && for pkg in $PKGS; do \
        echo "--- Building: $pkg ---"; \
        if cargo cinstall -p "$pkg" --release \
               --prefix=/opt/gst-rs --libdir=lib --jobs 2; then \
            INSTALLED=$((INSTALLED + 1)); \
            echo "+++ OK: $pkg"; \
        else \
            FAILED=$((FAILED + 1)); \
            echo "!!! SKIP: $pkg (build failed — missing native library?)"; \
        fi; \
    done \
    && echo "=== Rust plugin build complete: ${INSTALLED} installed, ${FAILED} skipped ===" \
    && find /opt/gst-rs -name "*.so" | sort

# ── F: Build the WebRTC signalling server binary ─────────────────────────────
# The gst-webrtc-signalling-server binary lives in the net/webrtc package.
# Cargo reuses the already-compiled artifacts from step E.
RUN cargo build --release --jobs 2 --bin gst-webrtc-signalling-server \
    && install -m755 target/release/gst-webrtc-signalling-server \
                     /opt/gst-webrtc-signalling-server

# ── G: Build the gstwebrtc-api JavaScript bundle ─────────────────────────────
WORKDIR /src/net/webrtc/gstwebrtc-api
RUN npm ci && npm run build \
    && echo "=== gstwebrtc-api dist ===" && ls -la dist/

# ── H: Build the nvcodec GStreamer plugin from the GStreamer monorepo ─────────
# nvcodec provides NVIDIA hardware encoders (nvh264enc, nvh265enc) via NVENC
# and decoders via NVDEC.  It uses dlopen for all NVIDIA libs, so it compiles
# without the NVIDIA SDK.  At runtime, nvidia-container-toolkit injects the
# driver libs when the container is started with --gpus.
WORKDIR /
RUN GST_VER="$(pkg-config --modversion gstreamer-1.0)" \
    && echo "=== Building nvcodec plugin for GStreamer ${GST_VER} ===" \
    && git clone --depth 1 --branch "${GST_VER}" \
           https://gitlab.freedesktop.org/gstreamer/gstreamer.git /gst-src \
    && cd /gst-src/subprojects/gst-plugins-bad \
    && meson setup builddir \
           --prefix=/opt/gst-nvcodec \
           --libdir=lib \
           -Dauto_features=disabled \
           -Dnvcodec=enabled \
    && ninja -C builddir -j2 \
    && meson install -C builddir \
    && echo "=== nvcodec artifacts ===" \
    && find /opt/gst-nvcodec -name "*.so*" | sort


# ─── Runtime Stage ────────────────────────────────────────────────────────────
FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive

# Enable universe + multiverse for plugins-ugly, libav, etc.
RUN sed -i \
        's/Components: main restricted$/Components: main restricted universe multiverse/' \
        /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || \
    sed -i \
        's/main restricted$/main restricted universe multiverse/' \
        /etc/apt/sources.list

# ── GStreamer core + all official plugin packages ─────────────────────────────
# gstreamer1.0-plugins-base   : alsa, ogg, vorbis, theora, opus, playback, ...
# gstreamer1.0-plugins-good   : alaw/mulaw, flac, jpeg, png, rtp, v4l2, ...
# gstreamer1.0-plugins-bad    : webrtcbin, dashdemux, hls, mxf, vaapi, ...
# gstreamer1.0-plugins-ugly   : x264, mp3, aac (clear of patent concerns)
# gstreamer1.0-libav           : full FFmpeg codec suite
# gstreamer1.0-nice            : libnice ICE/STUN/TURN for WebRTC
# libgstrtspserver-1.0-0       : GStreamer RTSP server library
RUN apt-get update && apt-get install -y --no-install-recommends \
        gstreamer1.0-tools \
        libgstreamer1.0-0 \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-libav \
        gstreamer1.0-nice \
        libgstrtspserver-1.0-0 \
        # Runtime libs required by the compiled GStreamer Rust plugins
        libssl3 \
        libnice10 \
        libdav1d7 \
        libsodium23 \
        libzvbi0 \
        libwebp7 \
        libgtk-4-1 \
        libcsound64-0 \
        # Application runtime
        python3 \
        netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Copy all compiled GStreamer Rust plugins into the local plugin directory.
COPY --from=builder /opt/gst-rs/lib/gstreamer-1.0/ /usr/local/lib/gstreamer-1.0/

# Copy the nvcodec plugin and its libgstcuda helper library.
COPY --from=builder /opt/gst-nvcodec/lib/gstreamer-1.0/ /usr/local/lib/gstreamer-1.0/
COPY --from=builder /opt/gst-nvcodec/lib/libgstcuda-1.0.so* /usr/local/lib/
RUN echo "/usr/local/lib" > /etc/ld.so.conf.d/gst-local.conf && ldconfig

# Copy the WebRTC signalling server binary.
COPY --from=builder /opt/gst-webrtc-signalling-server /usr/local/bin/gst-webrtc-signalling-server
RUN chmod +x /usr/local/bin/gst-webrtc-signalling-server

# Copy the gstwebrtc-api browser-side JS library.
COPY --from=builder /src/net/webrtc/gstwebrtc-api/dist/ /var/www/html/gstwebrtc-api/

# Copy web page and startup scripts.
COPY web/index.html  /var/www/html/index.html
COPY entrypoint.sh   /usr/local/bin/entrypoint.sh
COPY pipeline.sh     /usr/local/bin/pipeline.sh
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/pipeline.sh

# ── Environment defaults (all overridable at runtime via -e) ──────────────────
# GST_PLUGIN_PATH          : where GStreamer finds the Rust + nvcodec plugins
# DISPLAY                  : X11 display to capture (mount /tmp/.X11-unix from host)
# STREAM_CODEC             : vp9 | vp8 | h264 | h265
#                            h264/h265 use NVENC when --gpus all is passed
# NVIDIA_VISIBLE_DEVICES / NVIDIA_DRIVER_CAPABILITIES
#                          : no-ops when the container runs without --gpus
ENV GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0 \
    DISPLAY=:0 \
    STREAM_CODEC=vp9 \
    STREAM_WIDTH=1920 \
    STREAM_HEIGHT=1080 \
    STREAM_FRAMERATE=30 \
    STREAM_BITRATE_KBPS=2000 \
    SIGNALLING_HOST=0.0.0.0 \
    SIGNALLING_PORT=8443 \
    WEB_PORT=8080 \
    GST_WEBRTC_STUN_SERVER="" \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=video,compute

EXPOSE 8080/tcp
EXPOSE 8443/tcp

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
