package functests;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import functests.support.HubStack;
import io.qameta.allure.Description;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

import static org.junit.jupiter.api.Assertions.*;

/**
 * HTTP smoke tests for the desktop-stream-hub container — no browser needed.
 * The hub serves a static web root; streams.json is bind-mounted in at
 * runtime and served as a plain file.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class HubEndpointTest {

    private HubStack   hub;
    private HttpClient client;

    @BeforeAll
    void setup() {
        hub = new HubStack();
        client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5)).build();
    }

    @AfterAll
    void teardown() {
        if (hub != null) hub.stop();
    }

    @Test
    @DisplayName("GET / returns 200")
    @Description("Verifies the hub homepage responds with HTTP 200.")
    void rootReturns200() throws Exception {
        assertEquals(200, get("/").statusCode());
    }

    @Test
    @DisplayName("GET / body contains the stream grid element")
    @Description("Verifies the hub homepage HTML contains the id='grid' element the "
            + "stream tiles render into.")
    void rootHasGridElement() throws Exception {
        assertTrue(get("/").body().contains("id=\"grid\""),
                "hub index.html must contain an element with id='grid'");
    }

    @Test
    @DisplayName("GET / references streams.json")
    @Description("Guards against the fetch('/streams.json') call being accidentally "
            + "removed from the hub page.")
    void rootFetchesStreamsJson() throws Exception {
        assertTrue(get("/").body().contains("streams.json"),
                "hub index.html must reference streams.json");
    }

    @Test
    @DisplayName("GET /streams.json returns 200 with a JSON content type")
    @Description("Verifies the mounted streams.json is served with an HTTP 200 and a "
            + "JSON Content-Type header.")
    void streamsJsonServed() throws Exception {
        HttpResponse<String> r = get("/streams.json");
        assertEquals(200, r.statusCode());
        String ct = r.headers().firstValue("content-type").orElse("");
        assertTrue(ct.toLowerCase().contains("json"),
                "streams.json must be served with a JSON content type, got: " + ct);
    }

    @Test
    @DisplayName("GET /streams.json round-trips the mounted config")
    @Description("Verifies the served streams.json parses as a JSON array and matches "
            + "the config that was bind-mounted into the container.")
    void streamsJsonMatchesMountedConfig() throws Exception {
        ObjectMapper mapper = new ObjectMapper();
        JsonNode served   = mapper.readTree(get("/streams.json").body());
        JsonNode expected = mapper.readTree(HubStack.TEST_STREAMS_JSON);
        assertTrue(served.isArray(), "streams.json must be a JSON array");
        assertEquals(expected, served,
                "served streams.json must match the mounted config");
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private HttpResponse<String> get(String path) throws Exception {
        return client.send(
                HttpRequest.newBuilder(URI.create(hub.baseUrl() + path))
                           .GET().timeout(Duration.ofSeconds(10)).build(),
                HttpResponse.BodyHandlers.ofString());
    }
}
