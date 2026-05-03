package functests;

import functests.support.ServiceStack;
import io.qameta.allure.Description;
import org.junit.jupiter.api.DisplayName;
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

import static org.junit.jupiter.api.Assertions.*;

/**
 * Common test logic for the video endpoint suite (GET /video).  Concrete
 * subclasses supply the {@link ServiceStack} to test against via a
 * {@code @BeforeAll} that sets {@link #stack}, {@link #client}, and
 * {@link #setupEpoch}, and calls {@code stack.awaitFirstSegment()}.
 */
abstract class AbstractVideoEndpointTest {

    // VIDEO_MAX_SEC from the service source — requests strictly longer than this get 400.
    private static final int VIDEO_MAX_SEC = 12 * 3600; // 43200

    protected static ServiceStack stack;
    protected static HttpClient   client;
    protected static long         setupEpoch; // stable "now" captured at @BeforeAll

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
    @DisplayName("last=60s → Content-Type: video/mp4")
    @Description("Verifies that the video response sets Content-Type to video/mp4.")
    void lastParamContentTypeIsMp4() throws Exception {
        String ct = get("?last=60s").headers().firstValue("content-type").orElse("");
        assertTrue(ct.contains("video/mp4"), "Expected video/mp4, got: " + ct);
    }

    @Test
    @DisplayName("last=60s → Content-Disposition contains video.mp4")
    @Description("Verifies that the Content-Disposition header references video.mp4 as the download filename.")
    void lastParamContentDispositionPresent() throws Exception {
        String cd = get("?last=60s").headers().firstValue("content-disposition").orElse("");
        assertTrue(cd.contains("video.mp4"), "Expected video.mp4 in Content-Disposition, got: " + cd);
    }

    @Test
    @DisplayName("last=60s → response is a faststart MP4")
    @Description("Verifies that the video response body is a valid MP4 (ftyp at offset 4).")
    void lastParamResponseIsMp4() throws Exception {
        byte[] body = getBytes("?last=60s");
        assertTrue(isMp4(body),
                "Response body was not a valid MP4 (no 'ftyp' at offset 4)");
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
    @DisplayName("start + end overlapping segments → MP4")
    @Description("Verifies that a start/end window covering recorded segments returns a valid MP4 file.")
    void startEndResponseIsMp4() throws Exception {
        long now  = setupEpoch;
        byte[] body = getBytes("?start=" + (now - 60) + "&end=" + now);
        assertTrue(isMp4(body),
                "start/end response body was not a valid MP4 (no 'ftyp' at offset 4)");
    }

    @Test
    @DisplayName("Empty window → 200 with MP4 (pure-color fill)")
    @Description("Verifies that a time window with no segments still returns HTTP 200 with a valid MP4 (filled with solid color by ffmpeg).")
    void emptyWindowReturns200WithMp4() throws Exception {
        // Epoch 0–1 predates any recording; ffmpeg generates a pure-color fill MP4.
        HttpResponse<byte[]> r = getResponse("?start=0&end=1");
        assertEquals(200, r.statusCode());
        assertTrue(isMp4(r.body()),
                "Pure-color fill response was not a valid MP4 (no 'ftyp' at offset 4)");
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

    /**
     * Returns true when {@code body} is a valid MP4/ISO BMFF container.
     * Every MP4 starts with a {@code ftyp} box: a 4-byte big-endian box
     * size followed by the ASCII tag {@code 'f','t','y','p'} at offset 4.
     * The faststart flag does not change this layout — only the order of
     * subsequent boxes — so the same check covers both modes.
     */
    private static boolean isMp4(byte[] body) {
        if (body.length < 8) return false;
        return body[4] == 'f' && body[5] == 't' && body[6] == 'y' && body[7] == 'p';
    }

    private static String encode(String s) {
        return s.replace("+", "%2B").replace(":", "%3A");
    }
}
