package functests;

import functests.support.ServiceStack;
import io.qameta.allure.Description;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.openqa.selenium.JavascriptExecutor;
import org.openqa.selenium.WebDriver;
import org.openqa.selenium.chrome.ChromeDriver;
import org.openqa.selenium.chrome.ChromeOptions;

import java.time.Duration;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Proves /top and /bottom serve the correct halves of the source frame, at
 * the correct cropped dimensions.
 *
 * <p>The Xvfb desktop is painted two-tone — top half red, bottom half blue
 * ({@link ServiceStack#setDesktopTwoTone}).  The /top page must therefore
 * decode red frames and /bottom blue frames; if the ffmpeg crop y-offset
 * were missing or wrong (e.g. both crops reading from y=0), /bottom would
 * deliver red pixels and fail.  Frame dimensions are asserted as
 * 1280x360 — the DESKTOP_SPLITS half-height regions — proving the crop
 * filters are active and sized correctly, not just recoloured.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class SplitCropColorTest {

    private static final int EXPECT_WIDTH  = 1280;
    private static final int EXPECT_HEIGHT = 360;

    private ServiceStack stack;
    private WebDriver    driver;

    // Same canvas-average capture as AbstractLiveFeedColorTest.
    private static final String CAPTURE_SCRIPT =
        "const v = document.querySelector('video');" +
        "if (!v || !v.videoWidth || !v.videoHeight) {" +
        "    return {stage: 'no-video-size', readyState: v ? v.readyState : -1};" +
        "}" +
        "if (v.readyState < 2) {" +
        "    return {stage: 'not-ready', readyState: v.readyState};" +
        "}" +
        "const c = document.createElement('canvas');" +
        "c.width = v.videoWidth; c.height = v.videoHeight;" +
        "const ctx = c.getContext('2d');" +
        "try { ctx.drawImage(v, 0, 0, c.width, c.height); }" +
        "catch (e) { return {stage: 'drawImage-error', error: String(e)}; }" +
        "let data;" +
        "try { data = ctx.getImageData(0, 0, c.width, c.height).data; }" +
        "catch (e) { return {stage: 'getImageData-error', error: String(e)}; }" +
        "let r = 0, g = 0, b = 0;" +
        "const n = data.length / 4;" +
        "for (let i = 0; i < data.length; i += 4) {" +
        "    r += data[i]; g += data[i+1]; b += data[i+2];" +
        "}" +
        "return {stage: 'ok', width: c.width, height: c.height," +
        "        avgR: r/n, avgG: g/n, avgB: b/n};";

    @BeforeAll
    void setup() throws Exception {
        ServiceStack.closeAndReset();
        Thread.sleep(1_000); // let OS reclaim host ports and the Xvfb socket

        stack = ServiceStack.create(20);
        stack.setDesktopTwoTone("#ff0000", "#0000ff");

        ChromeOptions opts = new ChromeOptions();
        opts.addArguments(
                "--headless=new",
                "--no-sandbox",
                "--use-fake-ui-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-dev-shm-usage",
                "--disable-features=WebRtcHideLocalIpsWithMdns",
                "--allow-loopback-for-peer-connection");
        driver = new ChromeDriver(opts);
        driver.manage().timeouts().pageLoadTimeout(Duration.ofSeconds(30));
    }

    @AfterAll
    void teardown() {
        if (driver != null) {
            try { driver.quit(); } catch (Exception ignored) {}
        }
        if (stack != null) {
            stack.stopColorWindow();
            stack.stop();
        }
    }

    @Test
    @DisplayName("/top plays the red upper half at 1280x360")
    @Description("Loads /top pinned to tier 0 against a two-tone desktop (top red, "
            + "bottom blue) and verifies the decoded frames are red and exactly "
            + "half-height — the crop filter is active, sized right, and reads "
            + "from y=0.")
    void topStreamShowsRedHalf() throws Exception {
        Map<String, Object> stats = playAndCapture("/top");
        assertFrame(stats, "/top");
        assertTrue(avg(stats, "avgR") > 200 && avg(stats, "avgG") < 60
                        && avg(stats, "avgB") < 60,
                "/top frame is not red (expected R>200, G<60, B<60): " + stats);
    }

    @Test
    @DisplayName("/bottom plays the blue lower half at 1280x360")
    @Description("Loads /bottom pinned to tier 0 against the same two-tone desktop "
            + "and verifies the decoded frames are blue — proving the crop y-offset "
            + "selects the lower half, not a duplicate of the top rows.")
    void bottomStreamShowsBlueHalf() throws Exception {
        Map<String, Object> stats = playAndCapture("/bottom");
        assertFrame(stats, "/bottom");
        assertTrue(avg(stats, "avgR") < 60 && avg(stats, "avgG") < 60
                        && avg(stats, "avgB") > 200,
                "/bottom frame is not blue (expected R<60, G<60, B>200): " + stats);
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private Map<String, Object> playAndCapture(String pagePath) throws Exception {
        driver.get(stack.baseUrl() + pagePath + "?tier=0"
                + AbstractLiveFeedColorTest.buildTurnParams());

        long deadline = System.currentTimeMillis() + 60_000;
        while (System.currentTimeMillis() < deadline) {
            Object t = js().executeScript(
                    "const v = document.querySelector('video');"
                    + "return v ? v.currentTime : -1;");
            if (t instanceof Number n && n.doubleValue() > 0) break;
            Thread.sleep(500);
        }

        Map<String, Object> stats = null;
        deadline = System.currentTimeMillis() + 30_000;
        while (System.currentTimeMillis() < deadline) {
            @SuppressWarnings("unchecked")
            Map<String, Object> s =
                    (Map<String, Object>) js().executeScript(CAPTURE_SCRIPT);
            stats = s;
            if (s != null && "ok".equals(s.get("stage"))) return s;
            Thread.sleep(500);
        }
        fail(pagePath + " never produced a decodable frame within 30 s; "
                + "last capture: " + stats
                + "\n===== service container logs =====\n" + stack.serviceLogs());
        return null; // unreachable
    }

    private void assertFrame(Map<String, Object> stats, String label) {
        assertEquals(EXPECT_WIDTH, ((Number) stats.get("width")).intValue(),
                label + " frame width");
        assertEquals(EXPECT_HEIGHT, ((Number) stats.get("height")).intValue(),
                label + " frame height — crop is not producing half-height frames");
    }

    private static double avg(Map<String, Object> stats, String key) {
        return ((Number) stats.get(key)).doubleValue();
    }

    private JavascriptExecutor js() {
        return (JavascriptExecutor) driver;
    }
}
