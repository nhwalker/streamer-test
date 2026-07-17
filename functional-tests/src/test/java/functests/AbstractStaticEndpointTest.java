package functests;

import functests.support.ServiceStack;
import io.qameta.allure.Description;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Common test logic for the static endpoint suite (/, /top, /bottom, WHEP
 * endpoints).  Concrete subclasses supply the {@link ServiceStack} to test
 * against via a {@code @BeforeAll} that sets {@link #stack} and {@link #client}.
 */
abstract class AbstractStaticEndpointTest {

    // Set by the concrete subclass's @BeforeAll.  Sequential test execution
    // means the shared static field is safe despite being inherited by multiple
    // subclasses.
    protected static ServiceStack stack;
    protected static HttpClient   client;

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
    @DisplayName("GET / references the WHEP client script")
    @Description("Verifies the root page loads the same-origin WHEP/WebRTC client script (no build step, no external bundle).")
    void rootBodyReferencesWhepClient() throws Exception {
        String html = body("/");
        assertTrue(html.contains("src=\"/app.js\""),
                "Expected the page to load /app.js");
        String js = body("/app.js");
        assertTrue(js.contains("/whep"),
                "Expected app.js to build WHEP endpoint URLs");
        assertTrue(js.contains("RTCPeerConnection"),
                "Expected the WebRTC client in app.js");
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

    // ── WHEP endpoints ─────────────────────────────────────────────────────────

    @Test
    @DisplayName("Full-stream WHEP endpoint answers preflight")
    @Description("Verifies MediaMTX answers a WHEP OPTIONS request on the full-stream tier-0 path.")
    void fullStreamWhepEndpointReachable() throws Exception {
        assertWhepEndpointReachable("full_t0");
    }

    @Test
    @DisplayName("Top-half WHEP endpoint answers preflight")
    @Description("Verifies MediaMTX answers a WHEP OPTIONS request on the top-half tier-0 path.")
    void topWhepEndpointReachable() throws Exception {
        assertWhepEndpointReachable("top_t0");
    }

    @Test
    @DisplayName("Bottom-half WHEP endpoint answers preflight")
    @Description("Verifies MediaMTX answers a WHEP OPTIONS request on the bottom-half tier-0 path.")
    void bottomWhepEndpointReachable() throws Exception {
        assertWhepEndpointReachable("bottom_t0");
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

    private void assertWhepEndpointReachable(String whepPath) throws Exception {
        URI uri = URI.create("http://localhost:" + stack.webrtcPort()
                + "/" + whepPath + "/whep");
        HttpResponse<Void> r = client.send(
                HttpRequest.newBuilder(uri)
                           .method("OPTIONS", HttpRequest.BodyPublishers.noBody())
                           .timeout(Duration.ofSeconds(10)).build(),
                HttpResponse.BodyHandlers.discarding());
        assertTrue(r.statusCode() == 200 || r.statusCode() == 204,
                "WHEP OPTIONS for " + whepPath + " returned " + r.statusCode());
    }
}
