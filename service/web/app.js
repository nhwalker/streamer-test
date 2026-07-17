/*
 * app.js -- WHEP client, tier auto-switching, and the metrics gumball for
 * the desktop-stream page.
 *
 * The stream is received over WHEP (WebRTC-HTTP Egress Protocol): one
 * HTTP POST of an SDP offer to MediaMTX's per-path endpoint
 * (http://<host>:<webrtcPort>/<whepPath>/whep), the SDP answer comes
 * back in the response body, and media flows over a normal
 * RTCPeerConnection.  No signalling WebSocket, no client library.
 *
 * Query parameters:
 *   ?whep=<url>                    Pin the connection to a single WHEP
 *                                  endpoint URL (absolute, or relative to
 *                                  this page).  The default is built from
 *                                  /config.json based on the current URL
 *                                  path.  Disables tier auto-switching.
 *   ?tier=<N>                      Pin a specific tier index (0 = largest /
 *                                  source resolution, N-1 = smallest).
 *                                  Disables the ResizeObserver auto-switch.
 *                                  Used by functional tests so the chosen
 *                                  resolution is deterministic regardless
 *                                  of headless viewport size.
 *   ?stun=stun.l.google.com:19302  Add a STUN server for ICE when the
 *                                  browser and container are on different hosts.
 *   ?turn_uri= / ?turn_user= / ?turn_cred=
 *                                  Add a TURN relay for ICE.
 */
const params   = new URLSearchParams(window.location.search);
const path     = window.location.pathname.replace(/\/$/, '') || '/';
const stunUrl  = params.get('stun');
const turnUri  = params.get('turn_uri');
const turnUser = params.get('turn_user');
const turnCred = params.get('turn_cred');

const video  = document.getElementById('stream');
const status = document.getElementById('status');

const setStatus = (msg, isError = false) => {
  status.textContent = msg;
  status.className   = isError ? 'error' : '';
};

// Resolve the desktop name + tier ladder from the runtime config.
//
// Returns the routed stream's `tiers` array (descending order — entry 0
// is the source-resolution passthrough tier) along with display labels
// and the metric classifier targets.  Each tier carries a fully-formed
// `whepUrl` so callers don't have to know about ports or path naming.
//
// Falls back to a synthesised single tier if /config.json is missing
// (e.g. older container image cached during a rolling upgrade).
async function resolveStreamConfig() {
  let cfg = null;
  try {
    const r = await fetch('/config.json', { cache: 'no-store' });
    if (r.ok) cfg = await r.json();
  } catch (_) {}

  const host       = window.location.hostname;
  const webrtcPort = (cfg && cfg.webrtcPort) || 8889;
  const makeWhepUrl = (whepPath) =>
    `${window.location.protocol}//${host}:${webrtcPort}/${whepPath}/whep`;

  let desktopName  = cfg ? cfg.desktopName : host;
  let screenLabel  = '';
  let targetFps    = cfg ? cfg.framerate : 30;

  // Find the routed stream's tier list (full vs. per-screen).
  let rawTiers = null;
  let streamWidth  = cfg ? cfg.width  : null;
  let streamHeight = cfg ? cfg.height : null;
  if (cfg) {
    const screen = (cfg.screens || []).find(s => s.path === path);
    if (screen) {
      screenLabel  = ` / ${screen.name}`;
      streamWidth  = screen.width;
      streamHeight = screen.height;
      rawTiers     = screen.tiers || null;
    } else {
      rawTiers = cfg.fullTiers || null;
    }
  }

  let tiers;
  if (rawTiers && rawTiers.length) {
    tiers = rawTiers.map(t => ({
      scale:   t.scale,
      width:   t.width,
      height:  t.height,
      whepUrl: makeWhepUrl(t.whepPath),
    }));
  } else {
    // No config — synthesise the conventional tier-0 path from the
    // page's URL path ('/' → full_t0, '/top' → top_t0, ...).
    const streamKey = path === '/' ? 'full' : path.slice(1);
    tiers = [{
      scale: 1.0, width: streamWidth, height: streamHeight,
      whepUrl: makeWhepUrl(`${streamKey}_t0`),
    }];
  }

  // ?whep=… overrides everything to a single pinned endpoint.
  const whepOverride = params.get('whep');
  if (whepOverride) {
    tiers = [{
      scale: 1.0, width: streamWidth, height: streamHeight,
      whepUrl: new URL(whepOverride, window.location.href).href,
    }];
  }

  return { tiers, desktopName, screenLabel,
           targetFps, streamWidth, streamHeight };
}

// Smallest tier whose width AND height still meet the video element's
// rendered size (in physical pixels — clientWidth*DPR).  Falls back to
// the largest tier when the viewport is bigger than every tier (the
// common "fullscreen on a high-DPI monitor" case).  Tiers are passed
// largest-first to match the descending order from /config.json.
function pickTier(tiers, videoEl) {
  const dpr   = window.devicePixelRatio || 1;
  const wantW = Math.max(1, videoEl.clientWidth)  * dpr;
  const wantH = Math.max(1, videoEl.clientHeight) * dpr;
  let chosen = tiers[0]; // largest, fallback when viewport exceeds source
  for (const t of tiers) {
    if (t.width >= wantW && t.height >= wantH) chosen = t;
  }
  return chosen;
}

let _metricsTimer = null;
const _snapshots = [];     // rolling window of inbound-rtp counters
const WINDOW_LEN = 10;     // 1Hz × 10 = ~10s rolling window

// Targets the classifier compares against for the top two tiers.
// Populated on page boot from /config.json (and the routed screen, if any).
let _targetFps    = null;
let _targetWidth  = null;
let _targetHeight = null;

// Codec-dependent QP scale ceilings.  Source: codec specs.
//   H.264: 0–51, VP8: 0–127, VP9/AV1: 0–255.
// (Browsers report VP8/VP9 qpSum in the underlying libvpx 0–63 range, so we
// gate VP8/VP9 thresholds against that effective scale instead of the spec
// ceiling.  AV1 follows the same 0–255 convention as VP9 in stats.)
const QP_MAX          = { 'video/H264': 51, 'video/VP8': 127, 'video/VP9': 255, 'video/AV1': 255 };
const QP_YELLOW_RATIO = 0.7;

// "Visually lossless" QP per codec — well-cited rule-of-thumb thresholds.
// Below this, double-blind viewers cannot reliably distinguish from source.
const QP_VISUALLY_LOSSLESS = {
  'video/H264': 18,
  'video/VP8':  30,
  'video/VP9':  60,
  'video/AV1':  60,
};

// "Lossless" QP — the encoder stopped throwing data away.  Allow a tiny
// slack above 0 because rate-controlled encoders may briefly land at QP 1
// on transition frames even when the rest of the stream is QP 0.
const QP_LOSSLESS_MAX = 1;

// Frames cannot stall longer than this for the top two tiers.  Above
// ~100 ms the receiver is absorbing visible jitter even if no freeze fired.
const JBD_TOP_TIER_MAX_MS = 100;

// Per-metric tier classifiers.  Each returns one of:
//   'lossless' | 'visually-lossless' | 'good' | 'yellow' | 'red' | null
// null means "not enough data yet" — the value renders without a colour.
//
// The tiers line up with the overall classifier so a metric in the
// 'yellow' tier is exactly the metric that would push the gumball to
// yellow, and so on.  Most metrics don't differentiate lossless from
// visually-lossless (only QP does); they jump straight from bright-green
// to dark-green.
const QUALITY_CLASSES = ['q-lossless', 'q-visually-lossless',
                         'q-good', 'q-yellow', 'q-red'];

function setQuality(el, tier) {
  el.classList.remove(...QUALITY_CLASSES);
  if (tier) el.classList.add('q-' + tier);
}

function fpsTier(fps, target) {
  if (fps == null || fps === 0) return 'red';
  if (target != null && fps >= target - 1) return 'lossless';
  if (target == null)            return 'good';
  if (fps >= target * 0.9)       return 'good';
  if (fps >= target * 0.5)       return 'yellow';
  return 'red';
}

function latTier(rttSec) {
  if (rttSec == null) return null;
  const ms = rttSec * 500;       // RTT/2
  if (ms <  20) return 'lossless';
  if (ms <  50) return 'good';
  if (ms < 150) return 'yellow';
  return 'red';
}

function resTier(w, h, tw, th) {
  if (!w || !h) return 'red';
  if (tw == null || th == null) return 'good';
  if (w === tw && h === th)     return 'lossless';
  const ratio = (w * h) / (tw * th);
  if (ratio >= 0.5)  return 'good';
  if (ratio >= 0.25) return 'yellow';
  return 'red';
}

function qpTier(avgQp, mimeType) {
  if (avgQp == null) return null;
  if (avgQp <= QP_LOSSLESS_MAX) return 'lossless';
  const vl = QP_VISUALLY_LOSSLESS[mimeType];
  if (vl != null && avgQp <= vl) return 'visually-lossless';
  const yellow = (QP_MAX[mimeType] ?? 51) * QP_YELLOW_RATIO;
  if (avgQp <= yellow) return 'good';
  return 'yellow';
}

function freezeTier(perMin) {
  if (perMin == null) return null;
  if (perMin === 0) return 'lossless';
  if (perMin <= 2)  return 'yellow';
  return 'red';
}

function jbdTier(ms) {
  if (ms == null) return null;
  if (ms <= JBD_TOP_TIER_MAX_MS) return 'lossless';
  if (ms <= 200) return 'good';
  if (ms <= 500) return 'yellow';
  return 'red';
}

async function updateMetrics() {
  const pc = window._streamPc;
  if (!pc || typeof pc.getStats !== 'function') return;
  let stats;
  try { stats = await pc.getStats(); } catch (_) { return; }

  const codecsById = new Map();
  stats.forEach(r => { if (r.type === 'codec') codecsById.set(r.id, r.mimeType); });

  const snap = { t: performance.now() };
  let curFps = null;
  let rttSec = null;

  stats.forEach(r => {
    if (r.type === 'inbound-rtp' && r.kind === 'video') {
      curFps              = (typeof r.framesPerSecond === 'number') ? r.framesPerSecond : null;
      snap.framesDecoded  = r.framesDecoded     ?? 0;
      snap.qpSum          = (typeof r.qpSum === 'number') ? r.qpSum : null;
      snap.freezeCount    = r.freezeCount       ?? 0;
      snap.packetsLost    = r.packetsLost       ?? 0;
      snap.packetsReceived= r.packetsReceived   ?? 0;
      snap.pliCount       = r.pliCount          ?? 0;
      snap.jbDelay        = (typeof r.jitterBufferDelay         === 'number') ? r.jitterBufferDelay         : null;
      snap.jbEmitted      = (typeof r.jitterBufferEmittedCount  === 'number') ? r.jitterBufferEmittedCount  : null;
      snap.frameWidth     = r.frameWidth        ?? 0;
      snap.frameHeight    = r.frameHeight       ?? 0;
      snap.mimeType       = codecsById.get(r.codecId) || null;
      // Decoder implementation name (e.g. "ExternalDecoder", "libvpx",
      // "FFmpeg", "OpenH264") and the spec-defined power-efficient hint.
      // Either may be missing on browsers that haven't shipped the
      // RTCInboundRtpStreamStats fields yet; both are best-effort.
      snap.decoderImpl     = (typeof r.decoderImplementation === 'string') ? r.decoderImplementation : null;
      snap.powerEfficient  = (typeof r.powerEfficientDecoder === 'boolean') ? r.powerEfficientDecoder : null;
    }
    // Per-stream RTT (preferred): from RTCP RR/SR exchange for this video track.
    if (r.type === 'remote-inbound-rtp' && r.kind === 'video') {
      if (typeof r.roundTripTime === 'number') rttSec = r.roundTripTime;
    }
  });

  // Fallback: ICE candidate-pair RTT (network-wide, not video-specific).
  // Used until the first RTCP report exchange completes.
  if (rttSec == null) {
    stats.forEach(r => {
      if (r.type === 'candidate-pair' && r.nominated && r.state === 'succeeded') {
        if (typeof r.currentRoundTripTime === 'number') rttSec = r.currentRoundTripTime;
      }
    });
  }

  _snapshots.push(snap);
  while (_snapshots.length > WINDOW_LEN + 1) _snapshots.shift();

  const old    = _snapshots[0];
  const winSec = (snap.t - old.t) / 1000;

  // FPS: prefer Chrome's instantaneous value; fallback to window-averaged delta.
  let fps = curFps;
  if (fps == null && winSec > 0 && snap.framesDecoded != null && old.framesDecoded != null) {
    fps = (snap.framesDecoded - old.framesDecoded) / winSec;
  }

  // Average QP per frame over the window.
  const dFrames = (snap.framesDecoded ?? 0) - (old.framesDecoded ?? 0);
  const avgQp   = (snap.qpSum != null && old.qpSum != null && dFrames > 0)
      ? (snap.qpSum - old.qpSum) / dFrames
      : null;

  // Loss percentage over the window.
  const dLost   = snap.packetsLost      - old.packetsLost;
  const dRecv   = snap.packetsReceived  - old.packetsReceived;
  const lossPct = (dRecv + dLost) > 0 ? (dLost / (dRecv + dLost)) * 100 : null;

  // Freezes per minute, rescaled from the window length.
  const dFreezes      = snap.freezeCount - old.freezeCount;
  const freezesPerMin = winSec > 0 ? (dFreezes / winSec) * 60 : null;

  // Keyframe requests over the window — even one means severe loss.
  const dPli = snap.pliCount - old.pliCount;

  // Average jitter buffer delay per frame over the window, in ms.
  // Rising = network getting bursty; usually predicts freezes.
  const avgJbMs = (snap.jbDelay != null && old.jbDelay != null
                   && snap.jbEmitted != null && old.jbEmitted != null
                   && (snap.jbEmitted - old.jbEmitted) > 0)
      ? ((snap.jbDelay - old.jbDelay) / (snap.jbEmitted - old.jbEmitted)) * 1000
      : null;

  const res = (snap.frameWidth && snap.frameHeight)
      ? `${snap.frameWidth}×${snap.frameHeight}`
      : null;

  // Health classifier — needs a few samples before it's meaningful.
  //
  // Five tiers, evaluated worst-first.  The two top tiers (lossless,
  // visually-lossless) require positive evidence: low QP, full target
  // resolution, near-target fps, and zero faults.  Without QP data
  // (some browsers don't populate qpSum for hardware decoders) the
  // gumball can't claim either top tier and falls through to "good".
  let health = null;
  if (_snapshots.length >= 3) {
    const qpYellow = (QP_MAX[snap.mimeType] ?? 51) * QP_YELLOW_RATIO;
    const qpVL     = QP_VISUALLY_LOSSLESS[snap.mimeType];

    const isRed = (freezesPerMin != null && freezesPerMin > 2)
               || dPli > 0
               || (lossPct != null && lossPct > 5);

    const isYellow = (avgQp != null && avgQp > qpYellow)
                  || (lossPct != null && lossPct > 1)
                  || (avgJbMs != null && avgJbMs > 200);

    // Top-tier prerequisites: full delivery, no faults of any size.
    const noFaults = !isRed
                  && !isYellow
                  && (freezesPerMin == null || freezesPerMin === 0)
                  && (lossPct       == null || lossPct       === 0)
                  && dPli === 0
                  && (avgJbMs == null || avgJbMs <= JBD_TOP_TIER_MAX_MS);

    const fpsAtTarget = fps != null
                     && _targetFps != null
                     && fps >= _targetFps - 1;

    const resAtTarget = _targetWidth != null
                     && _targetHeight != null
                     && snap.frameWidth  === _targetWidth
                     && snap.frameHeight === _targetHeight;

    if (isRed) {
      health = 'red';
    } else if (isYellow) {
      health = 'yellow';
    } else if (noFaults && fpsAtTarget && resAtTarget
               && avgQp != null && avgQp <= QP_LOSSLESS_MAX) {
      health = 'lossless';
    } else if (noFaults && fpsAtTarget && resAtTarget
               && qpVL != null && avgQp != null && avgQp <= qpVL) {
      health = 'visually-lossless';
    } else if (fps != null && fps > 0) {
      health = 'good';
    }
  }

  const fpsEl   = document.getElementById('m-fps');
  const latEl   = document.getElementById('m-lat');
  const resEl   = document.getElementById('m-res');
  const qpEl    = document.getElementById('m-qp');
  const frzEl   = document.getElementById('m-frz');
  const jbdEl   = document.getElementById('m-jbd');
  const codecEl = document.getElementById('m-codec');
  const hwEl    = document.getElementById('m-hw');

  fpsEl.textContent = fps    != null ? fps.toFixed(1) : '--';
  // RTT/2 in ms = rttSec * 1000 / 2 = rttSec * 500
  latEl.textContent = rttSec != null ? Math.round(rttSec * 500).toString() : '--';
  resEl.textContent = res    ?? '--';
  qpEl .textContent = avgQp  != null ? Math.round(avgQp).toString() : '--';
  frzEl.textContent = freezesPerMin != null ? freezesPerMin.toFixed(1) : '--';
  jbdEl.textContent = avgJbMs != null ? Math.round(avgJbMs).toString() : '--';

  // Codec name: stats report "video/H264" etc.; strip the "video/" prefix
  // for a tighter readout.
  codecEl.textContent = snap.mimeType
      ? snap.mimeType.replace(/^video\//, '')
      : '--';

  // Hardware acceleration: prefer the spec's powerEfficientDecoder flag,
  // since it's the only signal explicitly defined to indicate hardware
  // backing.  When that's absent, fall back to a name heuristic on
  // decoderImplementation: known software decoders are listed below;
  // anything else (e.g. "ExternalDecoder", "MediaCodecVideoDecoder",
  // "VTVideoDecodeAccelerator") is treated as hardware.  When neither
  // field is populated yet we show '?' rather than guessing.
  const impl = snap.decoderImpl;
  let hwAccel = null;
  if (snap.powerEfficient != null) {
    hwAccel = snap.powerEfficient;
  } else if (impl) {
    const swNames = /^(libvpx|ffmpeg|openh264|dav1d|libaom|unknown)/i;
    hwAccel = !swNames.test(impl);
  }
  if (hwAccel == null) {
    hwEl.textContent = impl ? `? (${impl})` : '?';
  } else {
    hwEl.textContent = (hwAccel ? '✓ HW' : '✗ SW')
                     + (impl ? ` (${impl})` : '');
  }

  // Colour each value according to its individual tier, so the user can
  // see at a glance which metric is keeping the overall gumball below
  // bright-green.  Tier helpers run only after we have a few samples,
  // mirroring the gating on the overall classifier.
  if (_snapshots.length >= 3) {
    setQuality(fpsEl, fpsTier(fps, _targetFps));
    setQuality(latEl, latTier(rttSec));
    setQuality(resEl, resTier(snap.frameWidth, snap.frameHeight,
                              _targetWidth, _targetHeight));
    setQuality(qpEl,  qpTier(avgQp, snap.mimeType));
    setQuality(frzEl, freezeTier(freezesPerMin));
    setQuality(jbdEl, jbdTier(avgJbMs));
  }
  const dot = document.getElementById('m-health');
  dot.classList.remove('lossless', 'visually-lossless', 'good',
                       'yellow', 'red');
  if (health) dot.classList.add(health);
}

function startMetrics() {
  stopMetrics();
  _metricsTimer = setInterval(updateMetrics, 1000);
  updateMetrics();
}

function stopMetrics() {
  if (_metricsTimer != null) {
    clearInterval(_metricsTimer);
    _metricsTimer = null;
  }
  _snapshots.length = 0;
  for (const id of ['m-fps', 'm-lat', 'm-res', 'm-qp', 'm-frz', 'm-jbd',
                    'm-codec', 'm-hw']) {
    const el = document.getElementById(id);
    el.textContent = '--';
    setQuality(el, null);
  }
  document.getElementById('m-health').classList.remove(
    'lossless', 'visually-lossless', 'good', 'yellow', 'red');
}

(async () => {
  const { tiers, desktopName, screenLabel, targetFps,
          streamWidth, streamHeight } = await resolveStreamConfig();

  _targetFps = targetFps;
  document.getElementById('m-name').textContent   = desktopName;
  document.getElementById('m-screen').textContent = screenLabel;
  document.title = screenLabel
    ? `${desktopName}${screenLabel} — Desktop Stream`
    : `${desktopName} — Desktop Stream`;

  const iceServers = [];
  if (stunUrl) iceServers.push({ urls: `stun:${stunUrl}` });
  if (turnUri) iceServers.push({ urls: turnUri, username: turnUser ?? undefined, credential: turnCred ?? undefined });

  // Connection lifecycle state.  Each call to connectToTier() tears
  // down the prior peer connection / WHEP session and opens a new
  // one.  `connectSeq` stamps every async attempt so callbacks and
  // retry timers from a superseded connection are ignored — the user
  // should only see "Stream ended" when the producer actually
  // disappears, never during a tier switch.
  let currentPc         = null;
  let currentSessionUrl = null;   // WHEP resource (Location) for DELETE
  let currentTier       = null;
  let connectSeq        = 0;
  let retryTimer        = null;
  // Exposed for functional tests: assert which tier the browser picked.
  window._activeTier  = null;

  const WHEP_RETRY_MS = 2000;

  function teardownCurrent() {
    connectSeq += 1;              // invalidate in-flight attempts
    if (retryTimer != null) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (currentSessionUrl) {
      // Best-effort: free the server-side WHEP session immediately
      // instead of waiting for its ICE timeout.
      fetch(currentSessionUrl, { method: 'DELETE' }).catch(() => {});
      currentSessionUrl = null;
    }
    if (currentPc) {
      try { currentPc.close(); } catch (_) {}
      currentPc = null;
    }
    video.srcObject = null;
    stopMetrics();
  }

  // Resolve once ICE gathering finishes (or after a short timeout —
  // host candidates on a LAN gather in milliseconds; the timeout only
  // guards against a wedged mDNS/STUN lookup delaying the POST).
  function waitIceGathering(pc, timeoutMs = 1000) {
    if (pc.iceGatheringState === 'complete') return Promise.resolve();
    return new Promise((resolve) => {
      const timer = setTimeout(resolve, timeoutMs);
      pc.addEventListener('icegatheringstatechange', () => {
        if (pc.iceGatheringState === 'complete') {
          clearTimeout(timer);
          resolve();
        }
      });
    });
  }

  function scheduleRetry(tier, seq, delayMs) {
    if (seq !== connectSeq) return;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      if (seq === connectSeq) startWhep(tier, seq);
    }, delayMs);
  }

  async function startWhep(tier, seq) {
    const pc = new RTCPeerConnection({ iceServers });
    currentPc = pc;
    window._streamPc = pc;   // metrics loop reads this
    pc.addTransceiver('video', { direction: 'recvonly' });

    pc.addEventListener('track', (e) => {
      if (pc !== currentPc) return;
      video.srcObject = (e.streams && e.streams.length > 0)
        ? e.streams[0]
        : new MediaStream([e.track]);
      // Chrome's autoplay policy rejects play() without a user
      // gesture unless the element is muted.  The stream has no
      // audio track so muting is harmless.
      video.muted = true;
      const p = video.play();
      if (p && typeof p.catch === 'function') {
        p.catch((err) => console.warn('[stream] video.play() rejected:', err));
      }
    });

    pc.addEventListener('connectionstatechange', () => {
      if (pc !== currentPc || seq !== connectSeq) return;
      if (['failed', 'disconnected', 'closed'].includes(pc.connectionState)) {
        setStatus('Stream ended. Reconnecting…');
        video.srcObject = null;
        stopMetrics();
        try { pc.close(); } catch (_) {}
        scheduleRetry(tier, seq, WHEP_RETRY_MS);
      }
    });

    const hideBanner = () => { status.className = 'hidden'; };
    video.addEventListener('playing', hideBanner, { once: true });
    video.addEventListener('loadeddata', hideBanner, { once: true });
    video.addEventListener('playing', startMetrics, { once: true });

    try {
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await waitIceGathering(pc);
      if (seq !== connectSeq) return;

      const resp = await fetch(tier.whepUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/sdp' },
        body: pc.localDescription.sdp,
      });
      if (seq !== connectSeq) return;

      if (resp.status === 404) {
        // Path exists but nothing is publishing yet (ffmpeg still
        // starting, or restarting after a crash).  Keep polling.
        setStatus('Waiting for stream…');
        try { pc.close(); } catch (_) {}
        scheduleRetry(tier, seq, WHEP_RETRY_MS);
        return;
      }
      if (!resp.ok) {
        setStatus(`WHEP request failed: HTTP ${resp.status}`, true);
        try { pc.close(); } catch (_) {}
        scheduleRetry(tier, seq, WHEP_RETRY_MS * 2);
        return;
      }

      const loc = resp.headers.get('Location');
      currentSessionUrl = loc
        ? new URL(loc, tier.whepUrl).href
        : null;
      const answer = await resp.text();
      await pc.setRemoteDescription({ type: 'answer', sdp: answer });
      if (seq !== connectSeq) return;
      setStatus('Stream connected — waiting for video…');
    } catch (err) {
      if (seq !== connectSeq) return;
      setStatus(`Connection error: ${err.message ?? err}. Retrying…`, true);
      try { pc.close(); } catch (_) {}
      scheduleRetry(tier, seq, WHEP_RETRY_MS * 2);
    }
  }

  function connectToTier(tier) {
    if (currentTier && tier.whepUrl === currentTier.whepUrl) return;
    teardownCurrent();
    currentTier = tier;
    window._activeTier = {
      scale: tier.scale, width: tier.width, height: tier.height,
      whepUrl: tier.whepUrl,
    };
    // Update the gumball's "at-target resolution" classifier so the
    // active tier counts as 'at target' — not the source resolution.
    _targetWidth  = tier.width;
    _targetHeight = tier.height;

    setStatus(`Connecting to ${tier.whepUrl} (scale ${tier.scale}) …`);
    startWhep(tier, connectSeq);
  }

  // Pin a specific tier via ?tier=<index> (functional tests), or via
  // ?whep=… (single-entry tier list built by resolveStreamConfig).
  let pinnedTier = null;
  const tierParam = params.get('tier');
  if (tierParam != null) {
    const idx = Number.parseInt(tierParam, 10);
    if (Number.isFinite(idx) && idx >= 0 && idx < tiers.length) {
      pinnedTier = tiers[idx];
    }
  }
  if (tiers.length === 1) pinnedTier = tiers[0];  // whep override

  const chosen = pinnedTier || pickTier(tiers, video);
  connectToTier(chosen);

  // Only auto-switch when no override pinned us to a specific tier.
  if (!pinnedTier) {
    // Debounce ResizeObserver: window drags fire dozens of events per
    // second; we only want to act on the size the user lands on.
    let resizeTimer = null;
    const onResize = () => {
      if (resizeTimer != null) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        resizeTimer = null;
        const next = pickTier(tiers, video);
        if (currentTier && next.whepUrl !== currentTier.whepUrl) {
          connectToTier(next);
        }
      }, 250);
    };
    new ResizeObserver(onResize).observe(document.getElementById('video-area'));
  }
})();
