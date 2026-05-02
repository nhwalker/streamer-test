package functests;

import functests.support.ServiceStack;
import io.qameta.allure.Description;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.List;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Common test logic for the archive endpoint suite (GET /archive).  Concrete
 * subclasses supply the {@link ServiceStack} to test against via a
 * {@code @BeforeAll} that sets {@link #stack} and {@link #client}, and calls
 * {@code stack.awaitFirstSegment()} so there is at least one completed segment
 * before the tests run.
 */
abstract class AbstractArchiveEndpointTest {

    protected static ServiceStack stack;
    protected static HttpClient   client;

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

    // ── 200 — success cases ────────────────────────────────────────────────────

    @Test
    @DisplayName("last=60s → 200")
    @Description("Verifies that a valid last= duration returns HTTP 200.")
    void lastParamReturns200() throws Exception {
        assertEquals(200, get("?last=60s").statusCode());
    }

    @Test
    @DisplayName("last=60s → Content-Type: application/zip")
    @Description("Verifies that the archive response sets Content-Type to application/zip.")
    void lastParamContentTypeIsZip() throws Exception {
        String ct = get("?last=60s").headers().firstValue("content-type").orElse("");
        assertTrue(ct.contains("application/zip"), "Expected application/zip, got: " + ct);
    }

    @Test
    @DisplayName("last=60s → Content-Disposition contains archive.zip")
    @Description("Verifies that the Content-Disposition header references archive.zip as the download filename.")
    void lastParamContentDispositionPresent() throws Exception {
        String cd = get("?last=60s").headers().firstValue("content-disposition").orElse("");
        assertTrue(cd.contains("archive.zip"), "Expected archive.zip in Content-Disposition, got: " + cd);
    }

    @Test
    @DisplayName("last=60s → valid ZIP body")
    @Description("Verifies that the archive response body is a well-formed ZIP file (PK magic bytes).")
    void lastParamResponseIsValidZip() throws Exception {
        byte[] body = getBytes("?last=60s");
        assertTrue(isValidZip(body), "Response body did not start with ZIP magic bytes");
    }

    @Test
    @DisplayName("last=120s → ZIP contains .mkv files")
    @Description("Verifies that the archive ZIP contains at least one .mkv segment file.")
    void lastParamZipContainsMkvFiles() throws Exception {
        byte[] body = getBytes("?last=120s");
        assertTrue(isValidZip(body), "Response body is not a valid ZIP");
        List<String> entries = zipEntryNames(body);
        assertTrue(entries.stream().anyMatch(n -> n.endsWith(".mkv")),
                "Expected at least one .mkv entry in ZIP; entries: " + entries);
    }

    @Test
    @DisplayName("start + end as epoch seconds → 200")
    @Description("Verifies that start and end expressed as Unix epoch seconds return HTTP 200.")
    void startEndEpochParamsReturn200() throws Exception {
        long now = Instant.now().getEpochSecond();
        assertEquals(200, get("?start=" + (now - 120) + "&end=" + now).statusCode());
    }

    @Test
    @DisplayName("start + end as ISO 8601 → 200")
    @Description("Verifies that start and end expressed as ISO 8601 timestamps return HTTP 200.")
    void startEndIsoParamsReturn200() throws Exception {
        DateTimeFormatter fmt = DateTimeFormatter.ISO_INSTANT;
        String end   = fmt.format(Instant.now().atZone(ZoneOffset.UTC));
        String start = fmt.format(Instant.now().minusSeconds(120).atZone(ZoneOffset.UTC));
        assertEquals(200, get("?start=" + encode(start) + "&end=" + encode(end)).statusCode());
    }

    @Test
    @DisplayName("start + end overlapping segments → ZIP contains .mkv")
    @Description("Verifies that a start/end window covering recorded segments produces a ZIP with .mkv files.")
    void startEndResponseContainsMkvFiles() throws Exception {
        long now = Instant.now().getEpochSecond();
        byte[] body = getBytes("?start=" + (now - 120) + "&end=" + now);
        assertTrue(isValidZip(body), "Response body is not a valid ZIP");
        List<String> entries = zipEntryNames(body);
        assertTrue(entries.stream().anyMatch(n -> n.endsWith(".mkv")),
                "Expected .mkv entries for window covering last 120 s; entries: " + entries);
    }

    @Test
    @DisplayName("Window with no matching segments → 200 with valid empty ZIP")
    @Description("Verifies that a time window before any recordings were made returns HTTP 200 with a valid, empty ZIP archive.")
    void emptyWindowReturns200WithValidEmptyZip() throws Exception {
        // Epoch 0–1 predates any recording this service could have made.
        HttpResponse<byte[]> r = getResponse("?start=0&end=1");
        assertEquals(200, r.statusCode());
        byte[] body = r.body();
        assertTrue(isValidZip(body), "Response for empty window is not a valid ZIP");
        assertTrue(zipEntryNames(body).isEmpty(), "Expected empty ZIP for an empty window");
    }

    @Test
    @DisplayName("/archive has no 12-hour duration limit")
    @Description("Verifies that /archive accepts durations longer than 12 hours (unlike /video, which caps at 43200 s).")
    void archiveHasNoMaxDurationLimit() throws Exception {
        // 43201 s is just over the /video limit; /archive has no such restriction.
        assertEquals(200, get("?last=43201s").statusCode());
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private HttpResponse<byte[]> getResponse(String query) throws Exception {
        return client.send(
                HttpRequest.newBuilder(URI.create(stack.baseUrl() + "/archive" + query))
                           .GET().timeout(Duration.ofSeconds(30)).build(),
                HttpResponse.BodyHandlers.ofByteArray());
    }

    private HttpResponse<byte[]> get(String query) throws Exception {
        return getResponse(query);
    }

    private byte[] getBytes(String query) throws Exception {
        return getResponse(query).body();
    }

    private static boolean isValidZip(byte[] body) {
        // Both populated ZIPs (PK\x03\x04) and empty ZIPs (PK\x05\x06) start with 0x50 0x4B.
        return body.length >= 4 && (body[0] & 0xFF) == 0x50 && (body[1] & 0xFF) == 0x4B;
    }

    private static List<String> zipEntryNames(byte[] body) throws IOException {
        List<String> names = new ArrayList<>();
        try (ZipInputStream zis = new ZipInputStream(new ByteArrayInputStream(body))) {
            ZipEntry e;
            while ((e = zis.getNextEntry()) != null) {
                names.add(e.getName());
            }
        }
        return names;
    }

    private static String encode(String s) {
        return s.replace("+", "%2B").replace(":", "%3A");
    }
}
