package functests;

import functests.support.ServiceStack;
import io.qameta.allure.Description;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.WebSocket;
import java.time.Duration;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import org.junit.jupiter.api.Order;

import static org.junit.jupiter.api.Assertions.*;

@Order(1)
@DisplayName("Static endpoints (/, /top, /bottom)")
class StaticEndpointTest {

    private static ServiceStack stack;
    private static HttpClient   client;

    @BeforeAll
    static void setup() {
        stack  = ServiceStack.getInstance();
        client = HttpClient.newBuilder()
                           .connectTimeout(Duration.ofSeconds(10))
                           .build();
    }

    // ── GET / ─────────────────────────────────────────────────────────────────

    @Test
    @DisplayName("GET / returns 200")
    @Description("Verifies the root path responds with HTTP 200.")
    void rootReturns200() throws Exception {
        assertEquals(200, get("/").statusCode());
    }

    @Test
    @DisplayName("GET / Content-Type is HTML")
    @Description("Verifies the root path sets a text/html Content-Type header.")
    void rootContentTypeIsHtml() throws Exception {
        String ct = get("/").headers().firstValue("content-type").orElse("");
        assertTrue(ct.contains("text/html"), "Expected text/html, got: " + ct);
    }

    @Test
    @DisplayName("GET / body contains <video> element")
    @Description("Verifies the root page HTML contains a <video> element for the WebRTC stream.")
    void rootBodyContainsVideoElement() throws Exception {
        assertTrue(body("/").contains("<video"), "Expected <video> element in response body");
    }

    @Test
    @DisplayName("GET / references gstwebrtc-api JS bundle")
    @Description("Verifies the root page HTML references the gstwebrtc-api JavaScript bundle via a src attribute.")
    void rootBodyReferencesGstWebRtcApiBundle() throws Exception {
        assertNotNull(extractBundlePath(body("/")),
                "Expected a src= attribute referencing gstwebrtc-api*.js in the root page");
    }

    // ── GET /top ──────────────────────────────────────────────────────────────

    @Test
    @DisplayName("GET /top returns 200")
    @Description("Verifies the /top path responds with HTTP 200.")
    void topReturns200() throws Exception {
        assertEquals(200, get("/top").statusCode());
    }

    @Test
    @DisplayName("GET /top Content-Type is HTML")
    @Description("Verifies the /top path sets a text/html Content-Type header.")
    void topContentTypeIsHtml() throws Exception {
        String ct = get("/top").headers().firstValue("content-type").orElse("");
        assertTrue(ct.contains("text/html"), "Expected text/html, got: " + ct);
    }

    @Test
    @DisplayName("GET /top body contains <video> element")
    @Description("Verifies the /top page HTML contains a <video> element for the top-half WebRTC stream.")
    void topBodyContainsVideoElement() throws Exception {
        assertTrue(body("/top").contains("<video"), "Expected <video> element in /top response body");
    }

    // ── GET /bottom ───────────────────────────────────────────────────────────

    @Test
    @DisplayName("GET /bottom returns 200")
    @Description("Verifies the /bottom path responds with HTTP 200.")
    void bottomReturns200() throws Exception {
        assertEquals(200, get("/bottom").statusCode());
    }

    @Test
    @DisplayName("GET /bottom Content-Type is HTML")
    @Description("Verifies the /bottom path sets a text/html Content-Type header.")
    void bottomContentTypeIsHtml() throws Exception {
        String ct = get("/bottom").headers().firstValue("content-type").orElse("");
        assertTrue(ct.contains("text/html"), "Expected text/html, got: " + ct);
    }

    @Test
    @DisplayName("GET /bottom body contains <video> element")
    @Description("Verifies the /bottom page HTML contains a <video> element for the bottom-half WebRTC stream.")
    void bottomBodyContainsVideoElement() throws Exception {
        assertTrue(body("/bottom").contains("<video"), "Expected <video> element in /bottom response body");
    }

    // ── JS bundle ─────────────────────────────────────────────────────────────

    @Test
    @DisplayName("JS bundle URL is served with HTTP 200")
    @Description("Extracts the gstwebrtc-api bundle src URL from the index page and verifies it returns HTTP 200.")
    void jsBundleIsServed() throws Exception {
        String indexBody  = body("/");
        String bundlePath = extractBundlePath(indexBody);
        assertNotNull(bundlePath, "Could not find gstwebrtc-api bundle src in index HTML");

        String bundleUrl = stack.baseUrl() + (bundlePath.startsWith("/") ? bundlePath : "/" + bundlePath);
        HttpResponse<byte[]> r = client.send(
                HttpRequest.newBuilder(URI.create(bundleUrl))
                           .GET().timeout(Duration.ofSeconds(10)).build(),
                HttpResponse.BodyHandlers.ofByteArray());

        assertEquals(200, r.statusCode(), "JS bundle URL returned non-200: " + bundleUrl);
        assertTrue(r.body().length > 0, "JS bundle body is empty");
    }

    // ── WebSocket signalling ports ─────────────────────────────────────────────

    @Test
    @DisplayName("Main signalling port accepts WebSocket connection")
    @Description("Verifies that the full-stream WebRTC signalling server accepts a WebSocket upgrade on its mapped port.")
    void mainSignallingPortAcceptsWebSocket() throws Exception {
        assertWebSocketConnects(stack.wsPort());
    }

    @Test
    @DisplayName("Top-half signalling port accepts WebSocket connection")
    @Description("Verifies that the top-half WebRTC signalling server accepts a WebSocket upgrade on its mapped port.")
    void topSignallingPortAcceptsWebSocket() throws Exception {
        assertWebSocketConnects(stack.wsPortTop());
    }

    @Test
    @DisplayName("Bottom-half signalling port accepts WebSocket connection")
    @Description("Verifies that the bottom-half WebRTC signalling server accepts a WebSocket upgrade on its mapped port.")
    void bottomSignallingPortAcceptsWebSocket() throws Exception {
        assertWebSocketConnects(stack.wsPortBottom());
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private HttpResponse<String> get(String path) throws Exception {
        return client.send(
                HttpRequest.newBuilder(URI.create(stack.baseUrl() + path))
                           .GET().timeout(Duration.ofSeconds(10)).build(),
                HttpResponse.BodyHandlers.ofString());
    }

    private String body(String path) throws Exception {
        return get(path).body();
    }

    private static final Pattern BUNDLE_SRC = Pattern.compile(
            "src=['\"]([^'\"]*gstwebrtc-api[^'\"]*\\.js)['\"]");

    private static String extractBundlePath(String html) {
        Matcher m = BUNDLE_SRC.matcher(html);
        return m.find() ? m.group(1) : null;
    }

    private void assertWebSocketConnects(int port) throws Exception {
        CompletableFuture<WebSocket> future = client.newWebSocketBuilder()
                .buildAsync(URI.create("ws://localhost:" + port + "/"),
                        new WebSocket.Listener() {});
        WebSocket ws = future.get(10, TimeUnit.SECONDS);
        assertNotNull(ws, "WebSocket connection to port " + port + " returned null");
        ws.sendClose(WebSocket.NORMAL_CLOSURE, "").join();
    }
}
