package functests;

import functests.support.ServiceStack;
import io.qameta.allure.Allure;
import io.qameta.allure.Description;
import org.junit.jupiter.api.*;
import org.openqa.selenium.JavascriptExecutor;
import org.openqa.selenium.WebDriver;
import org.openqa.selenium.chrome.ChromeDriver;
import org.openqa.selenium.chrome.ChromeOptions;

import javax.imageio.ImageIO;
import java.awt.image.BufferedImage;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.*;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

@DisplayName("Live feed color verification")
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class LiveFeedColorTest {

    private static ServiceStack stack;
    private static WebDriver    driver;

    // Timestamps bracketing the flip test; set by videoFollowsTenColorFlips().
    private static volatile long flipStartEpoch;
    private static volatile long flipEndEpoch;

    // Canvas-based frame capture: returns {stage, width, height, avgR, avgG, avgB, avgA,
    // readyState, currentTime} or an error dict when the video is not yet decodable.
    private static final String CAPTURE_SCRIPT =
        "const v = document.querySelector('video');" +
        "if (!v || !v.videoWidth || !v.videoHeight) {" +
        "    return {stage: 'no-video-size', readyState: v ? v.readyState : -1};" +
        "}" +
        "if (v.readyState < 2) {" +
        "    return {stage: 'not-ready', readyState: v.readyState, currentTime: v.currentTime};" +
        "}" +
        "const c = document.createElement('canvas');" +
        "c.width = v.videoWidth; c.height = v.videoHeight;" +
        "const ctx = c.getContext('2d');" +
        "try { ctx.drawImage(v, 0, 0, c.width, c.height); }" +
        "catch (e) { return {stage: 'drawImage-error', error: String(e)}; }" +
        "let data;" +
        "try { data = ctx.getImageData(0, 0, c.width, c.height).data; }" +
        "catch (e) { return {stage: 'getImageData-error', error: String(e)}; }" +
        "let r = 0, g = 0, b = 0, a = 0;" +
        "const n = data.length / 4;" +
        "for (let i = 0; i < data.length; i += 4) {" +
        "    r += data[i]; g += data[i+1]; b += data[i+2]; a += data[i+3];" +
        "}" +
        "return {stage: 'ok', width: c.width, height: c.height," +
        "        avgR: r/n, avgG: g/n, avgB: b/n, avgA: a/n," +
        "        readyState: v.readyState, currentTime: v.currentTime};";

    @BeforeAll
    static void setup() throws Exception {
        stack = ServiceStack.getInstance();

        // Paint the display red before opening the browser so the first frame is red.
        stack.setDesktopColor("#ff0000");

        ChromeOptions opts = new ChromeOptions();
        opts.addArguments(
                "--headless=new",
                "--no-sandbox",
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-dev-shm-usage",
                "--disable-features=WebRtcHideLocalIpsWithMdns",
                "--allow-loopback-for-peer-connection"
        );
        driver = new ChromeDriver(opts);
        driver.manage().timeouts().pageLoadTimeout(Duration.ofSeconds(30));

        String url = stack.baseUrl() + "/?signalling=ws://localhost:" + stack.wsPort()
                + buildTurnParams();
        driver.get(url);

        // Wait up to 60 s for the video element to start advancing.
        long deadline = System.currentTimeMillis() + 60_000;
        while (System.currentTimeMillis() < deadline) {
            Object t = js().executeScript(
                    "const v = document.querySelector('video'); return v ? v.currentTime : -1;");
            if (t instanceof Number && ((Number) t).doubleValue() > 0) break;
            Thread.sleep(500);
        }

        // Wait up to 30 s for the first red frame to confirm the pipeline is live.
        awaitColor("red", 30_000);
    }

    @AfterAll
    static void teardown() {
        if (driver != null) {
            try { driver.quit(); } catch (Exception ignored) {}
        }
        if (stack != null) {
            stack.stopColorWindow();
        }
    }

    // ── Test 1: live flip verification ────────────────────────────────────────

    @Test
    @Order(1)
    @DisplayName("Video follows 10 desktop color flips")
    @Description("Alternates the desktop color between blue and red 10 times and "
            + "verifies the live WebRTC feed matches each new color within 3 seconds.")
    void videoFollowsTenColorFlips() throws Exception {
        final String[] colorNames = {
            "blue", "red", "blue", "red", "blue",
            "red",  "blue", "red", "blue", "red"
        };
        final String[] hexColors = {
            "#0000ff", "#ff0000", "#0000ff", "#ff0000", "#0000ff",
            "#ff0000", "#0000ff", "#ff0000", "#0000ff", "#ff0000"
        };

        // Bracket the flip test with epoch timestamps so the archive test can
        // request exactly this window from the /video endpoint.
        flipStartEpoch = Instant.now().getEpochSecond() - 1;

        for (int i = 0; i < 10; i++) {
            final String colorName = colorNames[i];
            final String hexColor  = hexColors[i];
            final int    flipNum   = i + 1;
            Allure.<Void>step("Flip " + flipNum + ": desktop → " + colorName, () -> {
                stack.setDesktopColor(hexColor);
                awaitColor(colorName, 3_000);
                // Hold the color for at least 1.5 s after detection so the
                // archive encoder (key-int-max=30, ~1 keyframe/s) captures
                // each color in a sealed MKV cluster.
                Thread.sleep(1_500);
                return null;
            });
        }

        // Final post-flip hold: keep desktop red for an additional 3 s so the
        // archive has solid red trailing the flip sequence.
        Thread.sleep(3_000);

        flipEndEpoch = Instant.now().getEpochSecond();
    }

    // ── Test 2: archived video color check ────────────────────────────────────

    @Test
    @Order(2)
    @DisplayName("Archived video contains both red and blue frames")
    @Description("Downloads the last 120 s of archived video via /video?last=120s after "
            + "the flip test, then extracts frames with ffmpeg and asserts at least one "
            + "predominantly-red and one predominantly-blue frame are present.")
    void videoEndpointContainsColorFlips() throws Exception {
        assumeTrue(flipStartEpoch > 0,
                "Flip test did not record timestamps — wrong execution order?");

        // Wait for the GStreamer pipeline to flush and seal the current MKV cluster.
        // x264enc writes keyframes every ~1 s (key-int-max=30); matroskamux seals a
        // cluster on each keyframe boundary.
        Thread.sleep(5_000);

        // Request the last 120 s of archive.  Using ?last= (relative to server now)
        // rather than exact epoch timestamps avoids a class of failures where the
        // flip window straddles an active-segment boundary: a 120 s window always
        // spans multiple completed 20 s segments, so _build_timeline always finds
        // readable files via ffprobe even if the active segment is still open.
        // The flip test ran ~33 s ago (28 s flips + 5 s sleep), well within 120 s.
        HttpClient httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .build();
        String videoUrl = stack.baseUrl() + "/video?last=120s";
        HttpResponse<byte[]> resp = httpClient.send(
                HttpRequest.newBuilder(URI.create(videoUrl))
                           .GET()
                           .timeout(Duration.ofSeconds(120))
                           .build(),
                HttpResponse.BodyHandlers.ofByteArray());

        assertEquals(200, resp.statusCode(),
                "Expected 200 from /video, got " + resp.statusCode());

        byte[] videoBytes = resp.body();
        assertTrue(videoBytes.length > 4, "Video response body is too short");
        // Sanity-check EBML magic bytes.
        assertEquals(0x1A, videoBytes[0] & 0xFF, "Expected EBML magic byte 0");
        assertEquals(0x45, videoBytes[1] & 0xFF, "Expected EBML magic byte 1");
        assertEquals(0xDF, videoBytes[2] & 0xFF, "Expected EBML magic byte 2");
        assertEquals(0xA3, videoBytes[3] & 0xFF, "Expected EBML magic byte 3");

        Path videoFile = Files.createTempFile("flip-video-", ".mkv");
        Path frameDir  = Files.createTempDirectory("flip-frames-");
        try {
            Files.write(videoFile, videoBytes);

            // Extract 4 fps at small scale — with a 120 s window that's ~480 frames,
            // enough to reliably catch a 1.5 s hold of each color.
            Process ffmpeg;
            try {
                ffmpeg = new ProcessBuilder(
                        "ffmpeg", "-hide_banner", "-y",
                        "-i", videoFile.toString(),
                        "-vf", "fps=4,scale=64:36",
                        frameDir.resolve("frame%04d.png").toString())
                        .redirectOutput(ProcessBuilder.Redirect.DISCARD)
                        .redirectError(ProcessBuilder.Redirect.DISCARD)
                        .start();
            } catch (IOException e) {
                assumeTrue(false, "ffmpeg not available on test host: " + e.getMessage());
                return;
            }
            assertTrue(ffmpeg.waitFor(60, TimeUnit.SECONDS),
                    "ffmpeg frame extraction did not complete within 60 s");

            // Read every extracted PNG and compute average R, G, B.
            List<double[]> frames = new ArrayList<>();
            try (DirectoryStream<Path> ds = Files.newDirectoryStream(frameDir, "*.png")) {
                for (Path png : ds) {
                    BufferedImage img = ImageIO.read(png.toFile());
                    if (img != null) frames.add(averageRgb(img));
                }
            }
            assertFalse(frames.isEmpty(),
                    "ffmpeg produced no PNG frames from the video clip");

            // Dump ALL frame RGB averages to stdout so the failure is self-diagnosable
            // in the CI log even without the Allure HTML report.
            System.out.println("[LiveFeedColorTest] video=" + videoUrl
                    + " frames=" + frames.size());
            for (int i = 0; i < frames.size(); i++) {
                double[] f = frames.get(i);
                System.out.printf("[LiveFeedColorTest] frame%04d R=%.0f G=%.0f B=%.0f%n",
                        i + 1, f[0], f[1], f[2]);
            }

            // Predominantly-red: red is at least 2× both green and blue,
            // and at least 100/255 in absolute terms. Loose enough to tolerate
            // YUV→RGB rounding, motion blur on the X11 buffer, and h264
            // chroma-subsampling. Same shape for blue.
            boolean sawRed  = frames.stream().anyMatch(rgb ->
                    rgb[0] >= 100 && rgb[0] >= 2 * rgb[1] && rgb[0] >= 2 * rgb[2]);
            boolean sawBlue = frames.stream().anyMatch(rgb ->
                    rgb[2] >= 100 && rgb[2] >= 2 * rgb[0] && rgb[2] >= 2 * rgb[1]);

            // Build a compact sample of the first 60 frames for failure messages.
            StringBuilder sample = new StringBuilder();
            int limit = Math.min(60, frames.size());
            for (int i = 0; i < limit; i++) {
                double[] f = frames.get(i);
                sample.append(String.format("[%.0f,%.0f,%.0f]", f[0], f[1], f[2]));
                if (i < limit - 1) sample.append(", ");
            }

            assertTrue(sawRed,
                    "No predominantly-red frame in archived video. "
                    + "url=" + videoUrl + " frames=" + frames.size()
                    + " sample=" + sample);
            assertTrue(sawBlue,
                    "No predominantly-blue frame in archived video. "
                    + "url=" + videoUrl + " frames=" + frames.size()
                    + " sample=" + sample);

        } finally {
            Files.deleteIfExists(videoFile);
            Files.walk(frameDir)
                 .sorted(Comparator.reverseOrder())
                 .forEach(p -> { try { Files.delete(p); } catch (IOException ignored) {} });
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private static void awaitColor(String colorName, long timeoutMs)
            throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMs;
        Map<?, ?> lastStats = null;
        while (System.currentTimeMillis() < deadline) {
            Object result = js().executeScript(CAPTURE_SCRIPT);
            if (result instanceof Map<?, ?> stats) {
                lastStats = stats;
                if (matchesColor(stats, colorName)) return;
            }
            Thread.sleep(200);
        }
        fail("Video did not show '" + colorName + "' within " + timeoutMs
                + " ms. Last frame: " + lastStats);
    }

    @SuppressWarnings("unchecked")
    private static boolean matchesColor(Map<?, ?> raw, String colorName) {
        Map<String, Object> stats = (Map<String, Object>) raw;
        if (!"ok".equals(stats.get("stage"))) return false;
        double avgR = num(stats.get("avgR"));
        double avgG = num(stats.get("avgG"));
        double avgB = num(stats.get("avgB"));
        return switch (colorName) {
            case "red"  -> avgR > 200 && avgG < 60 && avgB < 60;
            case "blue" -> avgR < 60  && avgG < 60 && avgB > 200;
            default     -> false;
        };
    }

    private static double[] averageRgb(BufferedImage img) {
        long r = 0, g = 0, b = 0;
        int w = img.getWidth(), h = img.getHeight();
        for (int y = 0; y < h; y++) {
            for (int x = 0; x < w; x++) {
                int px = img.getRGB(x, y);
                r += (px >> 16) & 0xFF;
                g += (px >>  8) & 0xFF;
                b +=  px        & 0xFF;
            }
        }
        long n = (long) w * h;
        return new double[]{(double) r / n, (double) g / n, (double) b / n};
    }

    private static double num(Object v) {
        return v instanceof Number ? ((Number) v).doubleValue() : 0.0;
    }

    private static JavascriptExecutor js() {
        return (JavascriptExecutor) driver;
    }

    /** Converts GStreamer TURN URL to browser query params, or returns "". */
    private static String buildTurnParams() {
        String gstTurn = System.getProperty("GST_WEBRTC_TURN_SERVER", "");
        if (gstTurn.isEmpty()) return "";
        try {
            // GStreamer format: turn://user:cred@host:port
            // Browser format:  ?turn_uri=turn:host:port&turn_user=user&turn_cred=cred
            URI uri = URI.create(gstTurn.replace("turn://", "http://"));
            String userInfo = uri.getUserInfo();
            int sep = userInfo.indexOf(':');
            String user = userInfo.substring(0, sep);
            String cred = userInfo.substring(sep + 1);
            return "&turn_uri=turn:" + uri.getHost() + ":" + uri.getPort()
                 + "&turn_user=" + user + "&turn_cred=" + cred;
        } catch (Exception e) {
            return "";
        }
    }
}
