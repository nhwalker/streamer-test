package functests;

import functests.support.BrowserRecorder;
import functests.support.BrowserRecorderExtension;
import functests.support.EvidenceAttachmentExtension;
import functests.support.RecordedBrowserTest;
import functests.support.ServiceStack;
import io.qameta.allure.Allure;
import io.qameta.allure.Description;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.extension.ExtendWith;
import org.openqa.selenium.JavascriptExecutor;
import org.openqa.selenium.WebDriver;
import org.openqa.selenium.chrome.ChromeDriver;
import org.openqa.selenium.chrome.ChromeOptions;

import javax.imageio.ImageIO;
import java.awt.image.BufferedImage;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.*;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.logging.Level;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;
import org.openqa.selenium.logging.LogType;
import org.openqa.selenium.logging.LoggingPreferences;

import static org.junit.jupiter.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

/**
 * Parameterisable live-feed color verification suite.
 *
 * <p>Concrete subclasses supply three parameters:
 * <ul>
 *   <li>{@link #numFlips()} — number of desktop color flips (must be even so
 *       the sequence ends on red)</li>
 *   <li>{@link #flipHoldMs()} — ms to hold each color after detection</li>
 *   <li>{@link #archiveSegmentSec()} — ARCHIVE_SEGMENT_SEC for the service
 *       container; controls how many completed segments exist during the test</li>
 * </ul>
 *
 * <p>{@code @TestInstance(PER_CLASS)} is required so that {@code @BeforeAll}
 * and {@code @AfterAll} run as instance methods and each concrete subclass gets
 * its own isolated set of fields ({@code stack}, {@code driver}, etc.).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
@ExtendWith({EvidenceAttachmentExtension.class, BrowserRecorderExtension.class})
abstract class AbstractLiveFeedColorTest implements RecordedBrowserTest {

    // ── Per-subclass instance state ───────────────────────────────────────────
    ServiceStack     stack;
    WebDriver        driver;
    BrowserRecorder  recorder;

    // Set by @Order(1); read by @Order(2) and @Order(3).
    long flipStartEpoch;
    long flipEndEpoch;

    // ── Parameters (supplied by subclasses) ───────────────────────────────────
    protected abstract int  numFlips();
    protected abstract long flipHoldMs();
    protected abstract int  archiveSegmentSec();

    // ── Canvas frame-capture script ───────────────────────────────────────────
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

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    @BeforeAll
    void setup() throws Exception {
        // Stop the singleton (used by endpoint tests) and any previous LiveFeed
        // stack, then start a fresh one with this subclass's segment duration.
        ServiceStack.closeAndReset();
        Thread.sleep(1_000); // let OS reclaim host ports and Xvfb socket

        stack = ServiceStack.create(archiveSegmentSec());
        stack.awaitFirstSegment(); // ensure ≥1 completed segment before tests run

        // Paint the display red before opening the browser so the first frame is red.
        stack.setDesktopColor("#ff0000");

        LoggingPreferences logPrefs = new LoggingPreferences();
        logPrefs.enable(LogType.BROWSER, Level.ALL);

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
        opts.setCapability("goog:loggingPrefs", logPrefs);
        driver = new ChromeDriver(opts);
        driver.manage().timeouts().pageLoadTimeout(Duration.ofSeconds(30));
        recorder = BrowserRecorder.forDriver(driver);

        // Pin tier 0 so the received resolution is deterministic regardless
        // of the headless viewport size; the page resolves the WHEP endpoint
        // from /config.json.
        String url = stack.baseUrl() + "/?tier=0" + buildTurnParams();
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
    void teardown() {
        if (recorder != null) {
            try { recorder.close(); } catch (Exception ignored) {}
        }
        if (driver != null) {
            try { driver.quit(); } catch (Exception ignored) {}
        }
        if (stack != null) {
            stack.stopColorWindow();
            stack.stop();
        }
    }

    @Override
    public BrowserRecorder browserRecorder() {
        return recorder;
    }

    // ── Test 0: stream metrics display populates ─────────────────────────────

    @Test
    @Order(0)
    @DisplayName("Stream metrics display populates")
    @Description("Verifies the page header populates every stream metric and the health dot. "
            + "Asserts that within 30s of playback all of #m-lat (RTT/2 ms), #m-res (resolution), "
            + "#m-qp (avg QP), #m-frz (freezes/min), #m-jbd (jitter buffer delay ms) show a value "
            + "other than '--' and that #m-health gets one of the five quality classes "
            + "(lossless, visually-lossless, good, yellow, red). No thresholds are asserted on "
            + "the values themselves.")
    void metricsDisplayPopulates() throws InterruptedException {
        long deadline = System.currentTimeMillis() + 30_000;
        @SuppressWarnings("unchecked")
        Map<String, Object> state = null;
        while (System.currentTimeMillis() < deadline) {
            state = (Map<String, Object>) js().executeScript(
                    "return {" +
                    "  lat:    document.getElementById('m-lat').textContent.trim()," +
                    "  res:    document.getElementById('m-res').textContent.trim()," +
                    "  qp:     document.getElementById('m-qp').textContent.trim()," +
                    "  frz:    document.getElementById('m-frz').textContent.trim()," +
                    "  jbd:    document.getElementById('m-jbd').textContent.trim()," +
                    "  health: document.getElementById('m-health').className.trim()" +
                    "};");
            if (allMetricsPopulated(state)) return;
            Thread.sleep(200);
        }
        fail(collectDiagnostics(
                "Stream metrics did not fully populate within 30 s; last state: " + state));
    }

    private static boolean allMetricsPopulated(Map<String, Object> state) {
        if (state == null) return false;
        for (String key : new String[]{"lat", "res", "qp", "frz", "jbd"}) {
            Object v = state.get(key);
            if (!(v instanceof String s) || s.isEmpty() || s.equals("--")) return false;
        }
        Object h = state.get("health");
        // The gumball uses one of five quality tiers; any non-empty tier means
        // the classifier ran and produced a verdict.
        return h instanceof String hs
                && (hs.equals("lossless")
                 || hs.equals("visually-lossless")
                 || hs.equals("good")
                 || hs.equals("yellow")
                 || hs.equals("red"));
    }

    private String collectDiagnostics(String reason) {
        StringBuilder sb = new StringBuilder(reason).append("\n");

        // Page state
        try {
            Object pageState = js().executeScript(
                    "const v = document.querySelector('video');" +
                    "const txt = id => {" +
                    "  const el = document.getElementById(id);" +
                    "  return el ? el.textContent : 'missing';" +
                    "};" +
                    "return {" +
                    "  mFps:   txt('m-fps')," +
                    "  mLat:   txt('m-lat')," +
                    "  mRes:   txt('m-res')," +
                    "  mQp:    txt('m-qp')," +
                    "  mFrz:   txt('m-frz')," +
                    "  mJbd:   txt('m-jbd')," +
                    "  mHealth: document.getElementById('m-health') ? document.getElementById('m-health').className : 'missing'," +
                    "  videoWidth:  v ? v.videoWidth  : -1," +
                    "  currentTime: v ? v.currentTime : -1," +
                    "  readyState:  v ? v.readyState  : -1" +
                    "};");
            sb.append("  page state: ").append(pageState).append("\n");
        } catch (Exception e) {
            sb.append("  page state: (error: ").append(e.getMessage()).append(")\n");
        }

        // Browser console logs (includes [rvfc-diag] and WebRTC errors)
        try {
            var entries = driver.manage().logs().get(LogType.BROWSER).getAll();
            sb.append("  browser console (").append(entries.size()).append(" entries):\n");
            for (var entry : entries) {
                sb.append("    [").append(entry.getLevel()).append("] ")
                  .append(entry.getMessage()).append("\n");
            }
        } catch (Exception e) {
            sb.append("  browser console: (error: ").append(e.getMessage()).append(")\n");
        }

        // Service container logs
        try {
            sb.append("===== service container logs =====\n")
              .append(stack.serviceLogs()).append("\n");
        } catch (Exception e) {
            sb.append("===== service container logs: error: ").append(e.getMessage()).append("\n");
        }

        return sb.toString();
    }

    // ── Test 1: live flip verification ────────────────────────────────────────

    @Test
    @Order(1)
    @DisplayName("Video follows desktop color flips")
    @Description("Alternates the desktop color between blue and red numFlips() times and "
            + "verifies the live WebRTC feed matches each new color within 3 seconds.")
    void videoFollowsColorFlips() throws Exception {
        final int n = numFlips();
        final String[] colorNames = new String[n];
        final String[] hexColors  = new String[n];
        for (int i = 0; i < n; i++) {
            colorNames[i] = i % 2 == 0 ? "blue"    : "red";
            hexColors[i]  = i % 2 == 0 ? "#0000ff" : "#ff0000";
        }

        flipStartEpoch = Instant.now().getEpochSecond() - 1;

        for (int i = 0; i < n; i++) {
            final String colorName = colorNames[i];
            final String hexColor  = hexColors[i];
            final int    flipNum   = i + 1;
            Allure.<Void>step("Flip " + flipNum + ": desktop → " + colorName, () -> {
                stack.setDesktopColor(hexColor);
                awaitColor(colorName, 3_000);
                Thread.sleep(flipHoldMs());
                return null;
            });
        }

        // Final post-flip hold: keep desktop red so the archive has solid red
        // trailing the flip sequence and the MKV cluster is flushed.
        Thread.sleep(3_000);

        flipEndEpoch = Instant.now().getEpochSecond();
    }

    // ── Test 2: archived video color check ────────────────────────────────────

    @Test
    @Order(2)
    @DisplayName("Archived video contains both red and blue frames")
    @Description("Downloads the last 120 s of archived video via /video?last=120s, "
            + "extracts frames with ffmpeg, and asserts at least one predominantly-red "
            + "and one predominantly-blue frame are present.")
    void videoEndpointContainsColorFlips() throws Exception {
        assumeTrue(flipStartEpoch > 0,
                "Flip test did not record timestamps — wrong execution order?");

        // Give the encoder/muxer a moment to flush the current fragment to disk.
        Thread.sleep(5_000);

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
        EvidenceAttachmentExtension.add(
                "video-endpoint-last120s.mp4", "video/mp4", ".mp4", videoBytes);
        assertTrue(videoBytes.length >= 8, "Video response body is too short");
        // Every MP4 starts with a 4-byte box size followed by the ASCII tag
        // 'ftyp' at offset 4.
        assertEquals('f', videoBytes[4], "Expected MP4 'ftyp' tag byte 0");
        assertEquals('t', videoBytes[5], "Expected MP4 'ftyp' tag byte 1");
        assertEquals('y', videoBytes[6], "Expected MP4 'ftyp' tag byte 2");
        assertEquals('p', videoBytes[7], "Expected MP4 'ftyp' tag byte 3");

        Path videoFile = Files.createTempFile("flip-video-", ".mp4");
        Path frameDir  = Files.createTempDirectory("flip-frames-");
        try {
            Files.write(videoFile, videoBytes);

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

            List<double[]> frames = new ArrayList<>();
            try (DirectoryStream<Path> ds = Files.newDirectoryStream(frameDir, "*.png")) {
                for (Path png : ds) {
                    BufferedImage img = ImageIO.read(png.toFile());
                    if (img != null) frames.add(averageRgb(img));
                }
            }
            assertFalse(frames.isEmpty(),
                    "ffmpeg produced no PNG frames from the video clip");

            boolean sawRed  = frames.stream().anyMatch(rgb ->
                    rgb[0] >= 100 && rgb[0] >= 2 * rgb[1] && rgb[0] >= 2 * rgb[2]);
            boolean sawBlue = frames.stream().anyMatch(rgb ->
                    rgb[2] >= 100 && rgb[2] >= 2 * rgb[0] && rgb[2] >= 2 * rgb[1]);

            StringBuilder sample = new StringBuilder();
            int limit = Math.min(60, frames.size());
            for (int i = 0; i < limit; i++) {
                double[] f = frames.get(i);
                sample.append(String.format("[%.0f,%.0f,%.0f]", f[0], f[1], f[2]));
                if (i < limit - 1) sample.append(", ");
            }

            String svcLogs = stack.serviceLogs();
            String svcTail = svcLogs.substring(Math.max(0, svcLogs.length() - 3000));
            assertTrue(sawRed,
                    "No predominantly-red frame in archived video. "
                    + "url=" + videoUrl + " frames=" + frames.size()
                    + " sample=" + sample + "\nservice logs (tail):\n" + svcTail);
            assertTrue(sawBlue,
                    "No predominantly-blue frame in archived video. "
                    + "url=" + videoUrl + " frames=" + frames.size()
                    + " sample=" + sample + "\nservice logs (tail):\n" + svcTail);

        } finally {
            Files.deleteIfExists(videoFile);
            Files.walk(frameDir)
                 .sorted(Comparator.reverseOrder())
                 .forEach(p -> { try { Files.delete(p); } catch (IOException ignored) {} });
        }
    }

    // ── Test 3: archive segment names + exact color-run sequence ─────────────

    @Test
    @Order(3)
    @DisplayName("Archive ZIP has valid segment names and flips in the correct order")
    @Description("Downloads the archive for the exact flip window, verifies every MP4 entry "
            + "name matches the expected timestamp format and segments are in chronological "
            + "order, then extracts frames from each segment in turn and asserts the color "
            + "run sequence matches the expected pattern for numFlips() flips.")
    void archiveEndpointSegmentNamesAndColorFlips() throws Exception {
        assumeTrue(flipStartEpoch > 0,
                "Flip test did not record timestamps — wrong execution order?");

        HttpClient httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .build();
        String archiveUrl = stack.baseUrl() + "/archive"
                + "?start=" + (flipStartEpoch - 5)
                + "&end="   + (flipEndEpoch   + 5);
        HttpResponse<byte[]> resp = httpClient.send(
                HttpRequest.newBuilder(URI.create(archiveUrl))
                           .GET()
                           .timeout(Duration.ofSeconds(30))
                           .build(),
                HttpResponse.BodyHandlers.ofByteArray());

        assertEquals(200, resp.statusCode(), "Expected 200 from /archive");

        // ── Extract and validate segment names ────────────────────────────────
        Pattern segPattern = Pattern.compile(
                "^\\w+_(\\d{8}-\\d{6}\\.\\d{3})_to_(\\d{8}-\\d{6}\\.\\d{3})\\.mp4$");
        DateTimeFormatter segFmt = DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss.SSS")
                .withZone(ZoneOffset.UTC);

        List<Map.Entry<String, byte[]>> segments = new ArrayList<>();
        try (ZipInputStream zis = new ZipInputStream(new ByteArrayInputStream(resp.body()))) {
            ZipEntry entry;
            while ((entry = zis.getNextEntry()) != null) {
                String name = entry.getName();
                if (name.endsWith(".mp4")) {
                    Matcher m = segPattern.matcher(name);
                    assertTrue(m.matches(),
                            "Segment name does not match expected format: " + name);
                    segments.add(Map.entry(name, zis.readAllBytes()));
                }
            }
        }
        assertFalse(segments.isEmpty(), "Archive ZIP contained no .mp4 segments");
        segments.sort(Map.Entry.comparingByKey());

        for (Map.Entry<String, byte[]> seg : segments) {
            EvidenceAttachmentExtension.add(
                    "archive/" + seg.getKey(), "video/mp4", ".mp4", seg.getValue());
        }

        // A segment that overlaps the request window [flipStartEpoch-5, flipEndEpoch+5]
        // may have started up to one full segment duration before the window start.
        // The segment muxer rotates at the next keyframe AFTER segment_time
        // elapses, so actual segment durations exceed the nominal
        // archiveSegmentSec() by up to one keyframe interval; allow a few
        // seconds of slack to absorb that.
        long segmentRotationSlackSec = 5;
        Instant windowStart = Instant.ofEpochSecond(
                flipStartEpoch - 5 - archiveSegmentSec() - segmentRotationSlackSec);
        Instant windowEnd   = Instant.ofEpochSecond(flipEndEpoch   + 65);
        Instant prevEnd     = null;
        for (Map.Entry<String, byte[]> seg : segments) {
            Matcher m = segPattern.matcher(seg.getKey());
            assertTrue(m.matches());
            Instant segStart = Instant.from(segFmt.parse(m.group(1)));
            Instant segEnd   = Instant.from(segFmt.parse(m.group(2)));

            assertTrue(segEnd.isAfter(segStart),
                    "Segment end is not after start: " + seg.getKey());
            assertFalse(segStart.isBefore(windowStart),
                    "Segment start is too far before the request window: " + seg.getKey()
                    + " (earliest expected: " + windowStart + ")");
            assertFalse(segStart.isAfter(windowEnd),
                    "Segment start timestamp is after the request window end: " + seg.getKey());
            if (prevEnd != null) {
                assertFalse(segStart.isBefore(prevEnd.minusMillis(500)),
                        "Segment overlaps its predecessor by >500 ms: " + seg.getKey()
                        + " (prev ended " + prevEnd + ", this starts " + segStart + ")");
            }
            prevEnd = segEnd;
        }

        // ── Extract frames, build color-run sequence ──────────────────────────
        List<String> colorRuns = new ArrayList<>();
        List<double[]> allFrames = new ArrayList<>();

        for (Map.Entry<String, byte[]> seg : segments) {
            Path segFile     = Files.createTempFile("arcseg-", ".mp4");
            Path segFrameDir = Files.createTempDirectory("arcseg-frames-");
            try {
                Files.write(segFile, seg.getValue());

                Process ffmpeg;
                try {
                    ffmpeg = new ProcessBuilder(
                            "ffmpeg", "-hide_banner", "-y",
                            "-i", segFile.toString(),
                            "-vf", "fps=4,scale=64:36",
                            segFrameDir.resolve("frame%04d.png").toString())
                            .redirectOutput(ProcessBuilder.Redirect.DISCARD)
                            .redirectError(ProcessBuilder.Redirect.DISCARD)
                            .start();
                } catch (IOException e) {
                    assumeTrue(false, "ffmpeg not available on test host: " + e.getMessage());
                    return;
                }
                assertTrue(ffmpeg.waitFor(30, TimeUnit.SECONDS),
                        "ffmpeg timed out on segment " + seg.getKey());

                List<Path> pngs = new ArrayList<>();
                try (DirectoryStream<Path> ds = Files.newDirectoryStream(segFrameDir, "*.png")) {
                    for (Path p : ds) pngs.add(p);
                }
                pngs.sort(Comparator.comparing(p -> p.getFileName().toString()));

                for (Path png : pngs) {
                    BufferedImage img = ImageIO.read(png.toFile());
                    if (img == null) continue;
                    double[] rgb = averageRgb(img);
                    allFrames.add(rgb);
                    String color =
                            (rgb[0] >= 100 && rgb[0] >= 2 * rgb[1] && rgb[0] >= 2 * rgb[2]) ? "red"  :
                            (rgb[2] >= 100 && rgb[2] >= 2 * rgb[0] && rgb[2] >= 2 * rgb[1]) ? "blue" : null;
                    if (color != null
                            && (colorRuns.isEmpty() || !color.equals(colorRuns.get(colorRuns.size() - 1)))) {
                        colorRuns.add(color);
                    }
                }
            } finally {
                Files.deleteIfExists(segFile);
                Files.walk(segFrameDir)
                     .sorted(Comparator.reverseOrder())
                     .forEach(p -> { try { Files.delete(p); } catch (IOException ignored) {} });
            }
        }

        // ── Assert exact flip sequence ────────────────────────────────────────
        // Display starts red (@BeforeAll), then numFlips() alternating flips starting
        // with blue. numFlips() must be even so the sequence ends on red.
        // Expected: [red, blue, red, blue, ..., red]  (numFlips()+1 entries)
        List<String> expected = new ArrayList<>();
        expected.add("red");
        for (int i = 0; i < numFlips(); i++) {
            expected.add(i % 2 == 0 ? "blue" : "red");
        }

        String svcLogs = stack.serviceLogs();
        String svcTail = svcLogs.substring(Math.max(0, svcLogs.length() - 3000));
        assertEquals(expected, colorRuns,
                "Color run sequence does not match expected " + numFlips() + "-flip pattern. "
                + "segments=" + segments.stream().map(Map.Entry::getKey).toList()
                + " frames=" + allFrames.size()
                + "\nservice logs (tail):\n" + svcTail);
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    void awaitColor(String colorName, long timeoutMs) throws InterruptedException {
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

    static double[] averageRgb(BufferedImage img) {
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

    private JavascriptExecutor js() {
        return (JavascriptExecutor) driver;
    }

    /** Converts a turn:// URL (CI relay) to browser query params, or returns "". */
    static String buildTurnParams() {
        String gstTurn = System.getProperty("GST_WEBRTC_TURN_SERVER", "");
        if (gstTurn.isEmpty()) return "";
        try {
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
