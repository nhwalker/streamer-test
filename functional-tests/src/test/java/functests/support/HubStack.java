package functests.support;

import org.testcontainers.containers.GenericContainer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.time.Duration;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Owns the hub container for the hub endpoint tests.
 *
 * <p>The hub is a static-file server: streams.json is bind-mounted into its
 * web root at runtime and served as a plain file — no backend logic.  A
 * fixed JSON body ({@link #TEST_STREAMS_JSON}) is written to a temp file and
 * mounted read-only, so tests can assert the served bytes round-trip.
 *
 * <p>Runs with host networking on port {@link #HTTP_PORT} (8091), away from
 * the service stack's 8080, so both stacks can coexist in one test run.
 */
public final class HubStack {

    private static final String HUB_IMAGE =
            System.getProperty("HUB_IMAGE", "desktop-stream-hub:ci");

    private static final int HTTP_PORT = 8091;

    /** The streams.json body mounted into the hub container. */
    public static final String TEST_STREAMS_JSON = """
            [{"name": "Workstation A", "url": "http://workstation-a:8080/"}, \
            {"name": "Workstation B", "url": "http://workstation-b:8080/"}]""";

    private final GenericContainer<?> hub;
    private final Path                streamsFile;
    private final AtomicBoolean       stopped = new AtomicBoolean(false);

    @SuppressWarnings("resource")
    public HubStack() {
        try {
            Path dir = Files.createTempDirectory("hub-streams-");
            Files.setPosixFilePermissions(dir,
                    PosixFilePermissions.fromString("rwxrwxrwx"));
            streamsFile = dir.resolve("streams.json");
            Files.writeString(streamsFile, TEST_STREAMS_JSON);

            hub = new GenericContainer<>(HUB_IMAGE)
                    .withNetworkMode("host")
                    .withEnv("WEB_PORT", String.valueOf(HTTP_PORT))
                    .withFileSystemBind(streamsFile.toString(),
                            "/etc/hub/web/streams.json", org.testcontainers.containers.BindMode.READ_ONLY);
            hub.start();
            awaitHttpReady();
        } catch (Exception e) {
            throw new IllegalStateException("HubStack failed to start", e);
        }
    }

    private void awaitHttpReady() throws Exception {
        HttpClient client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5)).build();
        long deadline = System.currentTimeMillis() + 15_000;
        while (System.currentTimeMillis() < deadline) {
            try {
                HttpResponse<Void> r = client.send(
                        HttpRequest.newBuilder(URI.create(baseUrl() + "/"))
                                   .GET().timeout(Duration.ofSeconds(2)).build(),
                        HttpResponse.BodyHandlers.discarding());
                if (r.statusCode() == 200) return;
            } catch (Exception ignored) {
            }
            Thread.sleep(250);
        }
        throw new IllegalStateException(
                "Hub HTTP server did not return 200 within 15 s.\n" +
                "Hub logs:\n" + hub.getLogs());
    }

    public String baseUrl() { return "http://localhost:" + HTTP_PORT; }
    public String logs()    { return hub.getLogs(); }

    public void stop() {
        if (!stopped.compareAndSet(false, true)) return;
        try { hub.stop(); } catch (Exception ignored) {}
        try { Files.deleteIfExists(streamsFile); } catch (Exception ignored) {}
    }
}
