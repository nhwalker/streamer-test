package nhwalker.streamer;

import com.github.dockerjava.api.exception.NotFoundException;
import com.github.dockerjava.api.model.AccessMode;
import com.github.dockerjava.api.model.Bind;
import com.github.dockerjava.api.model.Volume;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.openqa.selenium.By;
import org.openqa.selenium.JavascriptExecutor;
import org.openqa.selenium.chrome.ChromeOptions;
import org.openqa.selenium.remote.RemoteWebDriver;
import org.openqa.selenium.support.ui.ExpectedConditions;
import org.openqa.selenium.support.ui.WebDriverWait;
import org.testcontainers.DockerClientFactory;
import org.testcontainers.containers.BrowserWebDriverContainer;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.Network;
import org.testcontainers.containers.wait.strategy.Wait;
import org.testcontainers.images.builder.ImageFromDockerfile;
import org.testcontainers.utility.DockerImageName;

import java.nio.file.Path;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.*;

/**
 * End-to-end integration test for the GStreamer WebRTC desktop streaming pipeline.
 *
 * <p>Three containers start in dependency order:
 * <ol>
 *   <li><b>Xvfb</b> — virtual X display (UBI 9 image built from Dockerfile.xvfb).
 *       The root window is painted solid red via {@code xsetroot -solid red} so the
 *       captured content is a known, verifiable colour.</li>
 *   <li><b>GStreamer</b> — the container under test.  Mounts the X11 socket volume
 *       read-only, captures display :1 via {@code ximagesrc}, encodes with VP8 (faster
 *       keyframes than VP9 in software mode), and streams via WebRTC.</li>
 *   <li><b>Selenium Chrome</b> — {@code selenium/standalone-chrome}.  Opens the
 *       streaming web page on the shared Docker network, waits for the WebRTC video
 *       to start playing, and samples the center pixel to confirm it is red.</li>
 * </ol>
 *
 * <h2>X11 socket sharing</h2>
 * <p>A named Docker volume ({@value #X11_VOLUME_BASE}-&lt;uuid&gt;) is mounted at
 * {@code /tmp/.X11-unix} in both the Xvfb container (rw) and the GStreamer container
 * (ro).  A named volume (no slash in the name) is recognised by the Docker daemon as a
 * managed volume, not a host bind-mount — this works identically on a developer
 * workstation and in a CI runner that is itself a container.</p>
 *
 * <h2>Signalling URL override</h2>
 * <p>{@code index.html} defaults the WebSocket URL to
 * {@code ws://<window.location.hostname>:8443}.  The Selenium driver connects to Chrome
 * via the host-mapped port, so {@code window.location.hostname} would be the host IP,
 * not {@code "gstreamer"}.  The {@code ?signalling=ws://gstreamer:8443} query parameter
 * overrides this; Chrome (running inside the shared Docker network) resolves
 * {@code "gstreamer"} via Docker's embedded DNS.</p>
 */
class WebRtcStreamIT {

    // ── Pixel verification thresholds ─────────────────────────────────────────
    // xsetroot -solid red → VP8 encode → browser decode.
    // VP8 chroma subsampling introduces ≤ ~30 error per channel in practice.
    private static final int RED_MIN   = 170;
    private static final int GREEN_MAX = 60;
    private static final int BLUE_MAX  = 60;

    // Maximum time to wait for the <video> element to start playing.
    // Covers WebRTC ICE negotiation (~2-5 s on a Docker bridge) plus the first
    // VP8 keyframe (~3-5 s in software mode at 640×480 @ 15 fps).
    private static final Duration VIDEO_PLAY_TIMEOUT = Duration.ofSeconds(60);

    // ── Shared Docker resources (class lifecycle) ──────────────────────────────
    private static final String X11_VOLUME_BASE = "webrtc-test-x11";
    private static final String X11_VOLUME =
            X11_VOLUME_BASE + "-" + UUID.randomUUID().toString().replace("-", "").substring(0, 8);

    private static Network                   network;
    private static GenericContainer<?>       xvfb;
    private static GenericContainer<?>       gstreamer;
    private static BrowserWebDriverContainer<?> chrome;
    private static RemoteWebDriver           driver;

    // ── Container lifecycle ────────────────────────────────────────────────────

    /**
     * Starts all three containers in dependency order.
     *
     * <p>Each container's {@code waitingFor()} strategy blocks {@code start()} until
     * the container is genuinely ready, eliminating start-order races — in particular
     * the race between GStreamer's X11 preflight
     * ({@code gst-launch-1.0 ximagesrc num-buffers=1 ! fakesink}) and the Xvfb socket
     * creation inside the shared named volume.
     */
    @BeforeAll
    static void startContainers() {
        network = Network.newNetwork();

        // ── 1. Xvfb ─────────────────────────────────────────────────────────────
        // Built from tests/Dockerfile.xvfb (UBI 9 + Rocky 9 AppStream Xvfb packages).
        // withDockerfile(Path) uses the Dockerfile's parent as the build context, so
        // "Dockerfile.xvfb" resolves to tests/ when Gradle runs tests from tests/.
        // Named volume mounted rw: Xvfb creates /tmp/.X11-unix/X1 inside the volume.
        // Readiness: xdpyinfo -display :1 exits 0 inside the running container.
        xvfb = new GenericContainer<>(
                new ImageFromDockerfile("webrtc-test-xvfb:tc", /* deleteOnExit= */ false)
                        .withDockerfile(Path.of("Dockerfile.xvfb"))
        )
                .withNetwork(network)
                .withNetworkAliases("xvfb")
                .withCreateContainerCmdModifier(cmd ->
                        cmd.getHostConfig().withBinds(
                                new Bind(X11_VOLUME, new Volume("/tmp/.X11-unix"))
                        )
                )
                .waitingFor(
                        Wait.forSuccessfulCommand("xdpyinfo -display :1")
                                .withStartupTimeout(Duration.ofSeconds(30))
                );
        xvfb.start();

        // ── 2. GStreamer ─────────────────────────────────────────────────────────
        // resolveGstreamerImage() returns the pre-built image tag if it already exists
        // in the Docker daemon (set up by the CI workflow), otherwise builds it from
        // ../Dockerfile (project root).
        // Named volume mounted ro: X11 clients connect() to a socket on a read-only
        // filesystem mount; they cannot create new files there, only send/receive data.
        // STREAM_CODEC=vp8: VP8 produces keyframes in ~3-5 s vs VP9's ~10-15 s without
        // a hardware encoder, making VIDEO_PLAY_TIMEOUT reliably achievable in CI.
        // Readiness: "Desktop Stream ready" log message from entrypoint.sh (printed
        // after the signalling server and HTTP server are both listening).
        gstreamer = new GenericContainer<>(resolveGstreamerImage())
                .withNetwork(network)
                .withNetworkAliases("gstreamer")
                .withExposedPorts(8080, 8443)
                .withCreateContainerCmdModifier(cmd ->
                        cmd.getHostConfig().withBinds(
                                new Bind(X11_VOLUME, new Volume("/tmp/.X11-unix"), AccessMode.ro)
                        )
                )
                .withEnv("DISPLAY",             ":1")
                .withEnv("STREAM_CODEC",        "vp8")
                .withEnv("STREAM_WIDTH",        "640")
                .withEnv("STREAM_HEIGHT",       "480")
                .withEnv("STREAM_FRAMERATE",    "15")
                .withEnv("STREAM_BITRATE_KBPS", "500")
                .waitingFor(
                        Wait.forLogMessage(".*Desktop Stream ready.*", 1)
                                .withStartupTimeout(Duration.ofSeconds(120))
                );
        gstreamer.start();

        // ── 3. Selenium Chrome ───────────────────────────────────────────────────
        // --no-sandbox / --disable-dev-shm-usage: required when Chrome runs in Docker.
        // --autoplay-policy: allow <video autoplay> without a user gesture.
        // --use-fake-ui-for-media-stream: suppress getUserMedia permission dialogs.
        // --disable-gpu: no GPU in CI.
        // --headless intentionally omitted: selenium/standalone-chrome runs its own
        // Xvfb virtual display; non-headless mode has better WebRTC compatibility.
        ChromeOptions options = new ChromeOptions();
        options.addArguments(
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--autoplay-policy=no-user-gesture-required",
                "--use-fake-ui-for-media-stream",
                "--disable-gpu"
        );

        chrome = new BrowserWebDriverContainer<>(
                DockerImageName.parse("selenium/standalone-chrome:latest")
        )
                .withCapabilities(options)
                .withNetwork(network)
                .withNetworkAliases("selenium");
        chrome.start();

        driver = chrome.getWebDriver();
    }

    @AfterAll
    static void stopContainers() {
        safeRun(() -> driver.quit());
        safeRun(() -> chrome.stop());
        safeRun(() -> gstreamer.stop());
        safeRun(() -> xvfb.stop());
        safeRun(() -> DockerClientFactory.instance().client()
                .removeVolumeCmd(X11_VOLUME).exec());
        safeRun(() -> network.close());
    }

    private static void safeRun(Runnable r) {
        try { r.run(); } catch (Exception ignored) {}
    }

    // ── Test ──────────────────────────────────────────────────────────────────

    /**
     * Verifies the full X11 → GStreamer → WebRTC → browser pipeline end-to-end:
     * <ol>
     *   <li>The streaming web page loads in Chrome.</li>
     *   <li>The {@code <video id="stream">} element starts playing within
     *       {@link #VIDEO_PLAY_TIMEOUT}.</li>
     *   <li>The center pixel of the decoded video frame is approximately red,
     *       matching the {@code xsetroot -solid red} background on Xvfb.</li>
     * </ol>
     */
    @Test
    @Timeout(value = 120, unit = TimeUnit.SECONDS)
    void webrtcStreamPlaysAndShowsRedFrame() {
        driver.get("http://gstreamer:8080/?signalling=ws://gstreamer:8443");

        // Wait for the video element to appear in the DOM.
        new WebDriverWait(driver, Duration.ofSeconds(15))
                .until(ExpectedConditions.presenceOfElementLocated(By.id("stream")));

        // Poll until the video is actively playing.
        waitForVideoPlaying();

        // Draw the current video frame to a hidden canvas and sample the center pixel.
        // WebRTC srcObject streams are never cross-origin, so getImageData() does not
        // throw SecurityError (the canvas is not tainted).
        @SuppressWarnings("unchecked")
        List<Number> pixel = (List<Number>) driver.executeScript(SAMPLE_CENTER_PIXEL_JS);

        int r = pixel.get(0).intValue();
        int g = pixel.get(1).intValue();
        int b = pixel.get(2).intValue();

        assertTrue(r > RED_MIN,
                String.format("Red channel too low: R=%d G=%d B=%d — "
                        + "expected red frame from xsetroot -solid red.", r, g, b));
        assertTrue(g < GREEN_MAX,
                String.format("Green channel too high: R=%d G=%d B=%d.", r, g, b));
        assertTrue(b < BLUE_MAX,
                String.format("Blue channel too high: R=%d G=%d B=%d.", r, g, b));
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    /** Draws the current video frame to a canvas and returns the center pixel as [R, G, B]. */
    private static final String SAMPLE_CENTER_PIXEL_JS =
            "var video = document.getElementById('stream');" +
            "var w = video.videoWidth  || 640;" +
            "var h = video.videoHeight || 480;" +
            "var canvas = document.createElement('canvas');" +
            "canvas.width = w; canvas.height = h;" +
            "var ctx = canvas.getContext('2d');" +
            "ctx.drawImage(video, 0, 0, w, h);" +
            "var px = ctx.getImageData(Math.floor(w/2), Math.floor(h/2), 1, 1).data;" +
            "return [px[0], px[1], px[2]];";

    /** Returns the current video playback state as a map. */
    private static final String VIDEO_STATE_JS =
            "var v = document.getElementById('stream');" +
            "if (!v) return {error: 'element #stream not found'};" +
            "return {" +
            "  readyState:  v.readyState," +
            "  currentTime: v.currentTime," +
            "  paused:      v.paused," +
            "  mediaError:  v.error ? v.error.code : null" +
            "};";

    /**
     * Polls {@code <video id="stream">} until it is actively playing:
     * {@code readyState >= 3} (HAVE_FUTURE_DATA), {@code currentTime > 0.5 s},
     * and {@code paused == false}.
     *
     * <p>Fails immediately if the video element reports a {@code MediaError}.
     */
    private void waitForVideoPlaying() {
        long deadline = System.currentTimeMillis() + VIDEO_PLAY_TIMEOUT.toMillis();
        Map<?, ?> lastState = Map.of();

        while (System.currentTimeMillis() < deadline) {
            @SuppressWarnings("unchecked")
            Map<String, Object> state =
                    (Map<String, Object>) driver.executeScript(VIDEO_STATE_JS);
            lastState = state;

            Number mediaError = (Number) state.get("mediaError");
            if (mediaError != null) {
                fail("Video reported MediaError code " + mediaError
                        + " — codec unsupported or stream error.");
            }

            Number  readyState  = (Number)  state.get("readyState");
            Number  currentTime = (Number)  state.get("currentTime");
            Boolean paused      = (Boolean) state.get("paused");

            if (readyState  != null && readyState.intValue()    >= 3
             && currentTime != null && currentTime.doubleValue() > 0.5
             && Boolean.FALSE.equals(paused)) {
                return;
            }

            try {
                Thread.sleep(1_000);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                fail("Interrupted while waiting for video to play.");
            }
        }

        fail("Video did not start playing within " + VIDEO_PLAY_TIMEOUT.toSeconds()
                + "s. Last state: " + lastState);
    }

    /**
     * Returns the GStreamer image to use for the container under test.
     *
     * <p>If {@code webrtc-test-gstreamer:latest} is already present in the local
     * Docker daemon (loaded from the CI workflow's tar cache, or from a previous local
     * build) it is returned directly — no rebuild.  Otherwise the image is built from
     * the project root {@code Dockerfile} ({@code ../Dockerfile} relative to the
     * {@code tests/} working directory); this takes 20–40 minutes on a cold cache due
     * to Rust compilation of gst-plugins-rs.
     *
     * <p>{@code deleteOnExit=false}: the image is never removed automatically because
     * rebuilding it is expensive.
     */
    private static DockerImageName resolveGstreamerImage() {
        final String tag = "webrtc-test-gstreamer:latest";
        try {
            DockerClientFactory.instance().client()
                    .inspectImageCmd(tag).exec();
            // Image already present in the local daemon — skip the build.
            return DockerImageName.parse(tag);
        } catch (NotFoundException ignored) {
            // Image not found; fall through to build from source.
        }

        // withDockerfile(Path) uses the Dockerfile's parent directory as the build
        // context. "../Dockerfile" → parent is project root → context is project root.
        new ImageFromDockerfile(tag, /* deleteOnExit= */ false)
                .withDockerfile(Path.of("../Dockerfile"))
                .build();

        return DockerImageName.parse(tag);
    }
}
