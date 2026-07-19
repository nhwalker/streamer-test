package functests.support;

import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.wait.strategy.Wait;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.*;
import java.nio.file.attribute.PosixFilePermissions;
import java.time.Duration;
import java.util.Comparator;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Owns the Xvfb process and the service container for one test class.
 * The service container captures the Xvfb display directly via ffmpeg
 * x11grab and serves viewers over WHEP via its embedded MediaMTX.
 *
 * <h3>Usage patterns</h3>
 * <ul>
 *   <li><b>Singleton</b> — {@link #getInstance()} returns a lazily-created
 *       instance (20 s archive segments). Used by the endpoint tests which
 *       share one stack for the whole suite.</li>
 *   <li><b>Per-class</b> — {@link #create(int)} starts a fresh instance with
 *       a custom {@code archiveSegmentSec}. The caller is responsible for
 *       calling {@link #stop()} in {@code @AfterAll}. Call
 *       {@link #closeAndReset()} first to tear down the singleton (if any)
 *       before starting a new instance on the same host ports.</li>
 * </ul>
 *
 * The container runs with network_mode=host, matching the proven Python test
 * setup. MediaMTX serves WHEP on 8889 (ICE/UDP on 8189) on the host.
 */
public final class ServiceStack {

    private static final String SERVICE_IMAGE =
            System.getProperty("SERVICE_IMAGE", "desktop-stream-service:ci");

    // Fixed ports — service runs with network_mode=host so there is no
    // dynamic port mapping.
    private static final int HTTP_PORT = 8080;
    private static final int WHEP_PORT = 8889;

    // 1280x720 Xvfb split into top (0..360) / bottom (360..720) regions.
    private static final String DESKTOP_SPLITS =
            "1280x360+0+0;1280x360+0+360";

    // ── Singleton ────────────────────────────────────────────────────────────
    private static volatile ServiceStack INSTANCE;

    /** Returns the lazily-created singleton (20 s segments). */
    public static ServiceStack getInstance() {
        if (INSTANCE == null) {
            synchronized (ServiceStack.class) {
                if (INSTANCE == null) {
                    INSTANCE = new ServiceStack(20);
                }
            }
        }
        return INSTANCE;
    }

    /**
     * Stops the singleton (if one exists) and clears the reference so that
     * {@link #getInstance()} or {@link #create(int)} can start a fresh stack.
     * Safe to call when no singleton exists.
     */
    public static synchronized void closeAndReset() {
        ServiceStack inst = INSTANCE;
        INSTANCE = null;
        if (inst != null) inst.stop();
    }

    /**
     * Creates and starts a non-singleton {@link ServiceStack} with the given
     * archive segment duration. The caller owns the returned instance and
     * must call {@link #stop()} when done (typically from {@code @AfterAll}).
     * A JVM shutdown hook is also registered as a safety net.
     */
    public static ServiceStack create(int archiveSegmentSec) {
        ServiceStack s = new ServiceStack(archiveSegmentSec);
        Runtime.getRuntime().addShutdownHook(new Thread(s::stop));
        return s;
    }

    // ── Infrastructure handles ────────────────────────────────────────────────
    private final Process              xvfbProcess;
    private final GenericContainer<?>  service;
    private final Path                 archiveDir;
    private final List<Process>        colorWindows = new CopyOnWriteArrayList<>();
    private final AtomicBoolean        stopped = new AtomicBoolean(false);

    // ── Construction / start ─────────────────────────────────────────────────
    @SuppressWarnings("resource")
    private ServiceStack(int archiveSegmentSec) {
        try {
            xvfbProcess = startXvfb();

            archiveDir = Files.createTempDirectory("service-archive-");
            Files.setPosixFilePermissions(archiveDir,
                    PosixFilePermissions.fromString("rwxrwxrwx"));

            service = buildService(archiveSegmentSec);
            service.start();

            awaitHttpReady();

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

    // ── Service container ────────────────────────────────────────────────────
    @SuppressWarnings("resource")
    private GenericContainer<?> buildService(int archiveSegmentSec) {
        return new GenericContainer<>(SERVICE_IMAGE)
                .withNetworkMode("host")
                .withEnv("DISPLAY", ":99")
                .withEnv("STREAM_WIDTH", "1280")
                .withEnv("STREAM_HEIGHT", "720")
                .withEnv("STREAM_FRAMERATE", "30")
                .withEnv("DESKTOP_SPLITS", DESKTOP_SPLITS)
                .withEnv("ARCHIVE_SEGMENT_SEC", String.valueOf(archiveSegmentSec))
                .withEnv("ARCHIVE_DIR", "/archive")
                .withEnv("WEB_PORT", String.valueOf(HTTP_PORT))
                // Host networking: advertise loopback in ICE candidates so
                // the CI browser can connect over 127.0.0.1.
                .withEnv("WEBRTC_ADDITIONAL_HOSTS", "127.0.0.1")
                .withFileSystemBind("/tmp/.X11-unix", "/tmp/.X11-unix")
                .withCreateContainerCmdModifier(cmd ->
                        cmd.getHostConfig().withIpcMode("host"))
                .withFileSystemBind(archiveDir.toString(), "/archive")
                .waitingFor(Wait.forLogMessage(".*web server on port.*", 1)
                        .withStartupTimeout(Duration.ofSeconds(120)));
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
    public void stop() {
        if (!stopped.compareAndSet(false, true)) return;

        stopColorWindow();

        // Empty /archive from inside the container while it is still
        // running: the service stages /archive//video downloads in
        // root-owned .archive_stage_* dirs on this bind mount, and one
        // left behind by an interrupted request cannot be read or deleted
        // by the host-side walk below.  (-f keeps rm quiet when a glob
        // matches nothing.)
        try {
            service.execInContainer("sh", "-c",
                    "rm -rf /archive/* /archive/.[!.]*");
        } catch (Exception ignored) {}

        try { service.stop(); } catch (Exception ignored) {}

        xvfbProcess.destroy();
        try {
            if (!xvfbProcess.waitFor(5, TimeUnit.SECONDS)) xvfbProcess.destroyForcibly();
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }

        // Remove stale socket so the next ServiceStack can start Xvfb on :99.
        try { Files.deleteIfExists(Path.of("/tmp/.X11-unix/X99")); } catch (IOException ignored) {}

        if (archiveDir != null && Files.exists(archiveDir)) {
            // Files.walk surfaces unreadable subdirectories as
            // UncheckedIOException mid-iteration; anything the host user
            // cannot remove is abandoned in the temp dir rather than
            // failing the teardown (the in-container rm above should have
            // removed such dirs already).
            try {
                Files.walk(archiveDir)
                     .sorted(Comparator.reverseOrder())
                     .forEach(p -> { try { Files.delete(p); } catch (IOException ignored) {} });
            } catch (IOException | UncheckedIOException ignored) {}
        }
    }

    // ── Public API ────────────────────────────────────────────────────────────
    public String baseUrl()     { return "http://localhost:" + HTTP_PORT; }
    public String serviceLogs() { return service.getLogs(); }
    public int    webrtcPort()  { return WHEP_PORT; }
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
        colorWindows.add(launchColorWindow("1280x720+0+0", hexColor));
    }

    /**
     * Paints the top and bottom halves of the Xvfb display in two different
     * colors — one full-width window per half.  Used by the split-crop tests
     * to prove /top and /bottom really serve their own half of the frame
     * (a missing or wrong crop y-offset would deliver the other color).
     */
    public void setDesktopTwoTone(String topHex, String bottomHex) throws IOException {
        stopColorWindow();
        colorWindows.add(launchColorWindow("1280x360+0+0",   topHex));
        colorWindows.add(launchColorWindow("1280x360+0+360", bottomHex));
    }

    private static Process launchColorWindow(String geometry, String hexColor)
            throws IOException {
        return new ProcessBuilder(
                "xlogo",
                "-display", ":99",
                "-geometry", geometry,
                "-bg", hexColor,
                "-fg", hexColor,
                "-bw", "0")
                .redirectOutput(ProcessBuilder.Redirect.DISCARD)
                .redirectError(ProcessBuilder.Redirect.DISCARD)
                .start();
    }

    /** Terminates any color windows launched by {@link #setDesktopColor} /
     *  {@link #setDesktopTwoTone}. */
    public void stopColorWindow() {
        List<Process> procs = List.copyOf(colorWindows);
        colorWindows.clear();
        for (Process p : procs) {
            p.destroy();
            try {
                if (!p.waitFor(2, TimeUnit.SECONDS)) p.destroyForcibly();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
    }

    /**
     * Blocks until a completed {@code *_to_*.mp4} segment appears in the archive
     * directory.  Completed segments are produced by the pipeline's rename/move
     * step on the first fragment rotation — the live container is already a
     * fragmented MP4, so no remux is involved.
     * Polls every 1 s; throws {@link AssertionError} after 90 s.
     */
    public void awaitFirstSegment() throws InterruptedException, IOException {
        long deadline = System.currentTimeMillis() + 90_000;
        while (System.currentTimeMillis() < deadline) {
            try (DirectoryStream<Path> ds =
                         Files.newDirectoryStream(archiveDir, "*_to_*.mp4")) {
                if (ds.iterator().hasNext()) return;
            }
            Thread.sleep(1_000);
        }
        throw new AssertionError(
                "No completed segment (*_to_*.mp4) appeared in " + archiveDir + " within 90 s.\n" +
                "Service logs:\n" + service.getLogs());
    }
}
