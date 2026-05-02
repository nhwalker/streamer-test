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
import java.net.http.HttpTimeoutException;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;

import org.junit.jupiter.api.Order;

import static org.junit.jupiter.api.Assertions.*;

@Order(3)
@DisplayName("Video endpoint (GET /video)")
class VideoEndpointTest {

    // EBML magic bytes identifying a Matroska/WebM container.
    private static final byte[] EBML_MAGIC = {0x1A, 0x45, (byte) 0xDF, (byte) 0xA3};

    // VIDEO_MAX_SEC from the service source — requests strictly longer than this get 400.
    private static final int VIDEO_MAX_SEC = 12 * 3600; // 43200

    private static ServiceStack stack;
    private static HttpClient   client;
    private static long         setupEpoch; // stable "now" captured at @BeforeAll

    @BeforeAll
    static void setup() throws Exception {
        stack      = ServiceStack.getInstance();
        stack.awaitFirstSegment();
        setupEpoch = Instant.now().getEpochSecond();
        client     = HttpClient.newBuilder()
                               .connectTimeout(Duration.ofSeconds(10))
                               .build();
    }

    // ── 400 — missing / invalid parameters ───────────────────────────────────

    @Test
    @DisplayName("No params → 400")
    @Description("Verifies that a request without any query parameters returns HTTP 400.")
    void missingParamsReturns400() throws Exception {
        assertEquals(400, get("").statusCode());
    }

    @Test
    @DisplayName("start without end → 400")
    @Description("Verifies that providing start without end returns HTTP 400.")
    void startWithoutEndReturns400() throws Exception {
        assertEquals(400, get("?start=1000000000").statusCode());
    }

    @Test
    @DisplayName("end without start → 400")
    @Description("Verifies that providing end without start returns HTTP 400.")
    void endWithoutStartReturns400() throws Exception {
        assertEquals(400, get("?end=1000000000").statusCode());
    }

    @Test
    @DisplayName("Unrecognised duration unit → 400")
    @Description("Verifies that a duration with an unsupported unit suffix (e.g. 30x) returns HTTP 400.")
    void invalidDurationUnitReturns400() throws Exception {
        assertEquals(400, get("?last=30x").statusCode());
    }

    @Test
    @DisplayName("Empty duration → 400")
    @Description("Verifies that an empty last= parameter value returns HTTP 400.")
    void emptyDurationReturns400() throws Exception {
        assertEquals(400, get("?last=").statusCode());
    }

    @Test
    @DisplayName("Invalid start timestamp → 400")
    @Description("Verifies that a non-parseable start timestamp returns HTTP 400.")
    void invalidStartTimestampReturns400() throws Exception {
        assertEquals(400, get("?start=not-a-ts&end=1000000000").statusCode());
    }

    @Test
    @DisplayName("Invalid end timestamp → 400")
    @Description("Verifies that a non-parseable end timestamp returns HTTP 400.")
    void invalidEndTimestampReturns400() throws Exception {
        assertEquals(400, get("?start=1000000000&end=not-a-ts").statusCode());
    }

    @Test
    @DisplayName("Duration strictly over 12 hours (last=) → 400")
    @Description("Verifies that last= producing a window longer than 43200 s returns HTTP 400.")
    void lastDurationOver12HoursReturns400() throws Exception {
        // 43201 s > VIDEO_MAX_SEC; the service checks end_ts - start_ts > 43200.
        assertEquals(400, get("?last=" + (VIDEO_MAX_SEC + 1) + "s").statusCode());
    }

    @Test
    @DisplayName("start/end window strictly over 12 hours → 400")
    @Description("Verifies that a start/end window longer than 43200 s returns HTTP 400.")
    void startEndOver12HoursReturns400() throws Exception {
        long end   = setupEpoch;
        long start = end - (VIDEO_MAX_SEC + 1);
        assertEquals(400, get("?start=" + start + "&end=" + end).statusCode());
    }

    // ── 200 — success cases ────────────────────────────────────────────────────

    @Test
    @DisplayName("last=60s → 200")
    @Description("Verifies that a valid last= duration returns HTTP 200.")
    void lastParamReturns200() throws Exception {
        assertEquals(200, get("?last=60s").statusCode());
    }

    @Test
    @DisplayName("last=60s → Content-Type: video/x-matroska")
    @Description("Verifies that the video response sets Content-Type to video/x-matroska.")
    void lastParamContentTypeIsMkv() throws Exception {
        String ct = get("?last=60s").headers().firstValue("content-type").orElse("");
        assertTrue(ct.contains("video/x-matroska"), "Expected video/x-matroska, got: " + ct);
    }

    @Test
    @DisplayName("last=60s → Content-Disposition contains video.mkv")
    @Description("Verifies that the Content-Disposition header references video.mkv as the download filename.")
    void lastParamContentDispositionPresent() throws Exception {
        String cd = get("?last=60s").headers().firstValue("content-disposition").orElse("");
        assertTrue(cd.contains("video.mkv"), "Expected video.mkv in Content-Disposition, got: " + cd);
    }

    @Test
    @DisplayName("last=60s → response starts with EBML magic bytes")
    @Description("Verifies that the video response body is a valid Matroska file by checking its EBML magic header.")
    void lastParamResponseStartsWithEbmlMagic() throws Exception {
        byte[] body = getBytes("?last=60s");
        assertTrue(startsWithEbmlMagic(body),
                "Response body did not start with EBML magic bytes (1A 45 DF A3)");
    }

    @Test
    @DisplayName("start + end as epoch seconds → 200")
    @Description("Verifies that start and end expressed as Unix epoch seconds return HTTP 200.")
    void startEndEpochParamsReturn200() throws Exception {
        long now = setupEpoch;
        assertEquals(200, get("?start=" + (now - 60) + "&end=" + now).statusCode());
    }

    @Test
    @DisplayName("start + end as ISO 8601 → 200")
    @Description("Verifies that start and end expressed as ISO 8601 timestamps return HTTP 200.")
    void startEndIsoParamsReturn200() throws Exception {
        DateTimeFormatter fmt = DateTimeFormatter.ISO_INSTANT;
        String end   = fmt.format(Instant.ofEpochSecond(setupEpoch).atZone(ZoneOffset.UTC));
        String start = fmt.format(Instant.ofEpochSecond(setupEpoch - 60).atZone(ZoneOffset.UTC));
        assertEquals(200, get("?start=" + encode(start) + "&end=" + encode(end)).statusCode());
    }

    @Test
    @DisplayName("start + end overlapping segments → EBML magic")
    @Description("Verifies that a start/end window covering recorded segments returns a valid Matroska file.")
    void startEndResponseStartsWithEbmlMagic() throws Exception {
        long now  = setupEpoch;
        byte[] body = getBytes("?start=" + (now - 60) + "&end=" + now);
        assertTrue(startsWithEbmlMagic(body),
                "start/end response body did not start with EBML magic bytes");
    }

    @Test
    @DisplayName("Empty window → 200 with EBML magic (pure-color fill)")
    @Description("Verifies that a time window with no segments still returns HTTP 200 with a valid MKV (filled with solid color by ffmpeg).")
    void emptyWindowReturns200WithEbmlMagic() throws Exception {
        // Epoch 0–1 predates any recording; ffmpeg generates a pure-color fill MKV.
        HttpResponse<byte[]> r = getResponse("?start=0&end=1");
        assertEquals(200, r.statusCode());
        assertTrue(startsWithEbmlMagic(r.body()),
                "Pure-color fill MKV did not start with EBML magic bytes");
    }

    @Test
    @DisplayName("Exactly 12-hour window is accepted (boundary)")
    @Description("Verifies that a window of exactly 43200 s is not rejected with 400 (limit is strictly greater than, not >=). Encoding 12h takes too long to download, so a short per-request timeout is used: an immediate 400 is caught; a timeout means the server accepted.")
    void exactly12HourWindowIsAccepted() throws Exception {
        long end   = setupEpoch;
        long start = end - VIDEO_MAX_SEC; // exactly at the boundary — must not be 400
        try {
            HttpResponse<Void> r = client.send(
                    HttpRequest.newBuilder(URI.create(stack.baseUrl() + "/video?start=" + start + "&end=" + end))
                               .GET().timeout(Duration.ofSeconds(20)).build(),
                    HttpResponse.BodyHandlers.discarding());
            assertNotEquals(400, r.statusCode(),
                    "Expected the 43200 s boundary to be accepted, but got 400");
        } catch (HttpTimeoutException ignored) {
            // Server accepted and is still encoding (not rejected with 400) — test passes.
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private HttpResponse<byte[]> getResponse(String query) throws Exception {
        return client.send(
                HttpRequest.newBuilder(URI.create(stack.baseUrl() + "/video" + query))
                           .GET().timeout(Duration.ofSeconds(120)).build(),
                HttpResponse.BodyHandlers.ofByteArray());
    }

    private HttpResponse<byte[]> get(String query) throws Exception {
        return getResponse(query);
    }

    private byte[] getBytes(String query) throws Exception {
        return getResponse(query).body();
    }

    private static boolean startsWithEbmlMagic(byte[] body) {
        if (body.length < 4) return false;
        return (body[0] & 0xFF) == 0x1A
            && (body[1] & 0xFF) == 0x45
            && (body[2] & 0xFF) == 0xDF
            && (body[3] & 0xFF) == 0xA3;
    }

    private static String encode(String s) {
        return s.replace("+", "%2B").replace(":", "%3A");
    }
}
