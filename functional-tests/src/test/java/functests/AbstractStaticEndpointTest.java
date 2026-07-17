package functests;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import functests.support.ServiceStack;
import io.qameta.allure.Description;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

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

    // ── /config.json ──────────────────────────────────────────────────────────

    @Test
    @DisplayName("GET /config.json exposes the runtime config")
    @Description("Verifies /config.json returns the desktop name, the WHEP port, the "
            + "top/bottom screen split, and a two-tier ladder (1.0, 0.5) whose per-tier "
            + "whepPath values follow the <stream>_t<index> convention.")
    void configEndpointExposesRuntimeConfig() throws Exception {
        HttpResponse<String> r = get("/config.json");
        assertEquals(200, r.statusCode());
        JsonNode cfg = new ObjectMapper().readTree(r.body());

        assertTrue(cfg.hasNonNull("desktopName"), "config must carry desktopName");
        assertEquals(stack.webrtcPort(), cfg.path("webrtcPort").asInt());

        List<String> names = new ArrayList<>();
        for (JsonNode s : cfg.path("screens")) names.add(s.path("name").asText());
        assertEquals(List.of("top", "bottom"), names,
                "expected top/bottom split from DESKTOP_SPLITS; got " + names);

        JsonNode fullTiers = cfg.path("fullTiers");
        assertEquals(2, fullTiers.size(), "default ladder has two tiers (1.0, 0.5)");
        assertEquals(1.0, fullTiers.get(0).path("scale").asDouble());
        assertEquals(0.5, fullTiers.get(1).path("scale").asDouble());
        assertEquals("full_t0", fullTiers.get(0).path("whepPath").asText());
        assertEquals("full_t1", fullTiers.get(1).path("whepPath").asText());
        for (JsonNode s : cfg.path("screens")) {
            String name = s.path("name").asText();
            JsonNode tiers = s.path("tiers");
            assertEquals(name + "_t0", tiers.get(0).path("whepPath").asText());
            assertEquals(name + "_t1", tiers.get(1).path("whepPath").asText());
        }
    }

    // ── WHEP protocol behaviour ───────────────────────────────────────────────

    @Test
    @DisplayName("Every (stream x tier) WHEP endpoint answers preflight")
    @Description("Enumerates every whepPath from /config.json and verifies MediaMTX "
            + "answers a WHEP OPTIONS request on each — proving all paths are "
            + "registered in the MediaMTX path table, not just tier 0.")
    void everyTierWhepEndpointReachable() throws Exception {
        JsonNode cfg = new ObjectMapper().readTree(get("/config.json").body());
        List<String> paths = new ArrayList<>();
        for (JsonNode t : cfg.path("fullTiers")) paths.add(t.path("whepPath").asText());
        for (JsonNode s : cfg.path("screens")) {
            for (JsonNode t : s.path("tiers")) paths.add(t.path("whepPath").asText());
        }
        assertFalse(paths.isEmpty(), "config.json enumerated no whepPaths");
        for (String path : paths) {
            assertWhepEndpointReachable(path);
        }
    }

    @Test
    @DisplayName("WHEP rejects a junk SDP offer with 4xx")
    @Description("POSTing a non-SDP body to a WHEP endpoint must produce a 4xx "
            + "client error — not a hang and not a 5xx.")
    void whepRejectsJunkOffer() throws Exception {
        URI uri = URI.create("http://localhost:" + stack.webrtcPort()
                + "/full_t0/whep");
        HttpResponse<Void> r = client.send(
                HttpRequest.newBuilder(uri)
                           .header("Content-Type", "application/sdp")
                           .POST(HttpRequest.BodyPublishers.ofString("not-an-sdp-offer"))
                           .timeout(Duration.ofSeconds(10)).build(),
                HttpResponse.BodyHandlers.discarding());
        assertTrue(r.statusCode() >= 400 && r.statusCode() < 500,
                "junk WHEP offer returned " + r.statusCode() + ", expected 4xx");
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
