package functests.support;

import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.wait.strategy.Wait;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.*;
import java.nio.file.attribute.PosixFilePermissions;
import java.time.Duration;
import java.util.Comparator;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Singleton that owns the Xvfb process, caster container, and service container
 * for the duration of the test run. Call {@link #getInstance()} from each test
 * class's {@code @BeforeAll}; the stack is started exactly once and torn down
 * via a JVM shutdown hook.
 *
 * Both containers run with network_mode=host, matching the proven Python test
 * setup. The caster's signalling server uses port 8448 to avoid colliding with
 * the service's own browser-facing signalling servers on 8443/8444/8445.
 */
public final class ServiceStack {

    private static final String CASTER_IMAGE  =
            System.getProperty("CASTER_IMAGE",  "desktop-caster:ci");
    private static final String SERVICE_IMAGE =
            System.getProperty("SERVICE_IMAGE", "desktop-stream-service:ci");
    private static final String TURN_SERVER   =
            System.getProperty("GST_WEBRTC_TURN_SERVER", "");

    // Fixed ports — both containers run with network_mode=host so there is no
    // dynamic port mapping. Caster uses 8448 (not 8443) to avoid conflict with
    // the service's signalling servers on 8443/8444/8445.
    private static final int CASTER_SIGNALLING = 8448;
    private static final int HTTP_PORT         = 8080;
    private static final int WS_PORT           = 8443;
    private static final int WS_PORT_TOP       = 8444;
    private static final int WS_PORT_BOTTOM    = 8445;

    private static final Pattern UUID_PATTERN = Pattern.compile(
            "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            Pattern.CASE_INSENSITIVE);

    // ── Singleton ─────────────────────────────────────────────────────────────
    private static volatile ServiceStack INSTANCE;

    public static ServiceStack getInstance() {
        if (INSTANCE == null) {
            synchronized (ServiceStack.class) {
                if (INSTANCE == null) {
                    INSTANCE = new ServiceStack();
                }
            }
        }
        return INSTANCE;
    }

    // ── Infrastructure handles ────────────────────────────────────────────────
    private final Process             xvfbProcess;
    private final GenericContainer<?> caster;
    private final GenericContainer<?> service;
    private final Path                archiveDir;
    private volatile Process          colorWindow;

    // ── Construction / start ─────────────────────────────────────────────────
    @SuppressWarnings("resource")
    private ServiceStack() {
        try {
            // 1. Xvfb
            xvfbProcess = startXvfb();

            // 2. Caster
            caster = buildCaster();
            caster.start();
            Thread.sleep(1_000); // let webrtcsink register as producer

            // 3. Extract peer ID
            String peerId = extractCasterPeerId();

            // 4. Archive dir
            archiveDir = Files.createTempDirectory("service-archive-");
            Files.setPosixFilePermissions(archiveDir,
                    PosixFilePermissions.fromString("rwxrwxrwx"));

            // 5. Service
            service = buildService(peerId);
            service.start();

            // 6. Poll HTTP until ready
            awaitHttpReady();

            // 7. Shutdown hook
            Runtime.getRuntime().addShutdownHook(new Thread(this::stop));

        } catch (Exception e) {
            throw new IllegalStateException("ServiceStack failed to start", e);
        }
    }

    // ── Xvfb ─────────────────────────────────────────────────────────────────
    private static Process startXvfb() throws IOException, InterruptedException {
        Process proc = new ProcessBuilder("Xvfb", ":99", "-screen", "0", "1280x720x24")
                .redirectOutput(ProcessBuilder.Redirect.DISCARD)
                .redirectError(ProcessBuilder.Redirect.DISCARD)
                .start();

        Path socket = Path.of("/tmp/.X11-unix/X99");
        long deadline = System.currentTimeMillis() + 5_000;
        while (!Files.exists(socket) && System.currentTimeMillis() < deadline) {
            Thread.sleep(100);
        }
        if (!Files.exists(socket)) {
            proc.destroy();
            throw new IllegalStateException(
                    "Xvfb socket " + socket + " did not appear within 5 s. " +
                    "Ensure Xvfb is installed on the test host.");
        }
        return proc;
    }

    // ── Caster container ─────────────────────────────────────────────────────
    @SuppressWarnings("resource")
    private GenericContainer<?> buildCaster() {
        return new GenericContainer<>(CASTER_IMAGE)
                .withNetworkMode("host")
                .withEnv("DISPLAY", ":99")
                .withEnv("STREAM_WIDTH", "1280")
                .withEnv("STREAM_HEIGHT", "720")
                .withEnv("STREAM_FRAMERATE", "30")
                .withEnv("SIGNALLING_PORT", String.valueOf(CASTER_SIGNALLING))
                .withFileSystemBind("/tmp/.X11-unix", "/tmp/.X11-unix")
                .withCreateContainerCmdModifier(cmd ->
                        cmd.getHostConfig().withIpcMode("host"))
                .waitingFor(Wait.forLogMessage(".*Signalling server ready.*", 1)
                        .withStartupTimeout(Duration.ofSeconds(60)));
    }

    private String extractCasterPeerId() throws InterruptedException {
        // webrtcsink logs "registered as a producer" with the assigned UUID.
        long deadline = System.currentTimeMillis() + 15_000;
        while (System.currentTimeMillis() < deadline) {
            String logs = caster.getLogs();
            for (String line : logs.split("\n")) {
                if (line.contains("registered as a producer")) {
                    Matcher m = UUID_PATTERN.matcher(line);
                    if (m.find()) return m.group();
                }
            }
            Thread.sleep(500);
        }
        throw new IllegalStateException(
                "Could not extract caster peer ID from logs within 15 s.\n" +
                "Caster logs:\n" + caster.getLogs());
    }

    // ── Service container ─────────────────────────────────────────────────────
    @SuppressWarnings("resource")
    private GenericContainer<?> buildService(String peerId) {
        GenericContainer<?> c = new GenericContainer<>(SERVICE_IMAGE)
                .withNetworkMode("host")
                .withEnv("CASTER_HOST", "127.0.0.1")
                .withEnv("CASTER_SIGNALLING_PORT", String.valueOf(CASTER_SIGNALLING))
                .withEnv("CASTER_PEER_ID", peerId)
                .withEnv("SIGNALLING_PORT", String.valueOf(WS_PORT))
                .withEnv("CROP_HEIGHT", "360")
                .withEnv("ARCHIVE_SEGMENT_SEC", "20")
                .withEnv("ARCHIVE_DIR", "/archive")
                .withEnv("WEB_PORT", String.valueOf(HTTP_PORT))
                .withFileSystemBind(archiveDir.toString(), "/archive")
                .waitingFor(Wait.forLogMessage(".*web server on port.*", 1)
                        .withStartupTimeout(Duration.ofSeconds(120)));
        if (!TURN_SERVER.isEmpty()) {
            c = c.withEnv("GST_WEBRTC_TURN_SERVER", TURN_SERVER);
        }
        return c;
    }

    private void awaitHttpReady() throws Exception {
        HttpClient client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .build();
        long deadline = System.currentTimeMillis() + 10_000;
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
                "Service HTTP server did not return 200 within 10 s.\n" +
                "Service logs:\n" + service.getLogs());
    }

    // ── Stop ─────────────────────────────────────────────────────────────────
    private void stop() {
        stopColorWindow();
        try { service.stop(); } catch (Exception ignored) {}
        try { caster.stop();  } catch (Exception ignored) {}

        xvfbProcess.destroy();
        try {
            if (!xvfbProcess.waitFor(5, TimeUnit.SECONDS)) xvfbProcess.destroyForcibly();
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }

        if (archiveDir != null && Files.exists(archiveDir)) {
            try {
                Files.walk(archiveDir)
                     .sorted(Comparator.reverseOrder())
                     .forEach(p -> { try { Files.delete(p); } catch (IOException ignored) {} });
            } catch (IOException ignored) {}
        }
    }

    // ── Public API ────────────────────────────────────────────────────────────
    public String baseUrl()     { return "http://localhost:" + HTTP_PORT; }
    public int    wsPort()      { return WS_PORT; }
    public int    wsPortTop()   { return WS_PORT_TOP; }
    public int    wsPortBottom(){ return WS_PORT_BOTTOM; }
    public Path   archiveDir()  { return archiveDir; }

    /**
     * Paints the Xvfb display (:99) with the given hex color by launching
     * an xlogo window that covers the entire screen. Terminates any
     * previously launched color window first.
     *
     * @param hexColor X11 color string such as {@code "#ff0000"} or {@code "#0000ff"}
     */
    public void setDesktopColor(String hexColor) throws IOException {
        stopColorWindow();
        colorWindow = new ProcessBuilder(
                "xlogo",
                "-display", ":99",
                "-geometry", "1280x720+0+0",
                "-bg", hexColor,
                "-fg", hexColor,
                "-bw", "0")
                .redirectOutput(ProcessBuilder.Redirect.DISCARD)
                .redirectError(ProcessBuilder.Redirect.DISCARD)
                .start();
    }

    /** Terminates the color window launched by {@link #setDesktopColor}, if any. */
    public void stopColorWindow() {
        Process p = colorWindow;
        colorWindow = null;
        if (p != null) {
            p.destroy();
            try {
                if (!p.waitFor(2, TimeUnit.SECONDS)) p.destroyForcibly();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
    }

    /**
     * Blocks until a {@code stream-*.mkv} file appears in the archive directory.
     * Polls every 1 s; throws {@link AssertionError} after 60 s.
     */
    public void awaitFirstSegment() throws InterruptedException, IOException {
        long deadline = System.currentTimeMillis() + 90_000;
        while (System.currentTimeMillis() < deadline) {
            try (DirectoryStream<Path> ds =
                         Files.newDirectoryStream(archiveDir, "*_to_*.mkv")) {
                if (ds.iterator().hasNext()) return;
            }
            Thread.sleep(1_000);
        }
        throw new AssertionError(
                "No completed segment (*_to_*.mkv) appeared in " + archiveDir + " within 90 s.\n" +
                "Caster logs:\n" + caster.getLogs() + "\n" +
                "Service logs:\n" + service.getLogs());
    }
}
