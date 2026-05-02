package functests;

import functests.support.ServiceStack;
import io.qameta.allure.Allure;
import io.qameta.allure.Description;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.openqa.selenium.JavascriptExecutor;
import org.openqa.selenium.WebDriver;
import org.openqa.selenium.chrome.ChromeDriver;
import org.openqa.selenium.chrome.ChromeOptions;

import java.net.URI;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.fail;

@DisplayName("Live feed color verification")
class LiveFeedColorTest {

    private static ServiceStack stack;
    private static WebDriver    driver;

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
        driver.manage().timeouts().pageLoadTimeout(java.time.Duration.ofSeconds(30));

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

    @Test
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

        for (int i = 0; i < 10; i++) {
            final String colorName = colorNames[i];
            final String hexColor  = hexColors[i];
            final int    flipNum   = i + 1;
            Allure.<Void>step("Flip " + flipNum + ": desktop → " + colorName, () -> {
                stack.setDesktopColor(hexColor);
                awaitColor(colorName, 3_000);
                return null;
            });
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
