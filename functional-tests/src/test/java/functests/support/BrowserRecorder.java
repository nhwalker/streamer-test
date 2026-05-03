package functests.support;

import org.openqa.selenium.WebDriver;
import org.openqa.selenium.devtools.DevTools;
import org.openqa.selenium.devtools.HasDevTools;
import org.openqa.selenium.devtools.v131.page.Page;
import org.openqa.selenium.devtools.v131.page.model.ScreencastFrame;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.Base64;
import java.util.Optional;
import java.util.concurrent.ConcurrentLinkedDeque;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * Records the rendered output of a Selenium-driven Chrome browser using the
 * Chrome DevTools Protocol {@code Page.startScreencast} API.  Frames are
 * buffered in memory until the test completes; on {@link #stopAndAttach},
 * they are encoded to MP4 with {@code ffmpeg} and registered with
 * {@link EvidenceAttachmentExtension}, which decides whether to actually
 * attach them to the Allure report (only on failure or when
 * {@code KEEP_EVIDENCE=true}).
 *
 * <p>Designed to be debug-only: any failure inside the recorder (DevTools
 * version mismatch, ffmpeg missing, encoder error) is logged and degrades
 * gracefully — it never propagates an exception that could affect the test
 * outcome.
 */
public final class BrowserRecorder implements AutoCloseable {

    private static final Logger LOG = Logger.getLogger(BrowserRecorder.class.getName());

    /** Cap to bound memory: 600 frames @ ~50 KB each ≈ 30 MB worst case. */
    private static final int MAX_BUFFERED_FRAMES = 600;

    private static final int FFMPEG_TIMEOUT_SEC = 30;

    private final DevTools devTools;
    private final ConcurrentLinkedDeque<byte[]> frames = new ConcurrentLinkedDeque<>();
    private final AtomicInteger droppedFrames = new AtomicInteger();
    private final AtomicBoolean running = new AtomicBoolean();
    private boolean disabled;
    private boolean listenerRegistered;

    private BrowserRecorder(DevTools devTools) {
        this.devTools = devTools;
    }

    /**
     * Creates a recorder bound to the given driver.  The driver must implement
     * {@link HasDevTools} (Chrome and Edge do).  Returns a disabled recorder
     * (subsequent calls are no-ops) if the driver does not expose DevTools or
     * if the bundled DevTools binding is incompatible with the running Chrome.
     */
    public static BrowserRecorder forDriver(WebDriver driver) {
        try {
            if (!(driver instanceof HasDevTools h)) {
                LOG.warning("Driver does not implement HasDevTools; recording disabled.");
                BrowserRecorder r = new BrowserRecorder(null);
                r.disabled = true;
                return r;
            }
            return new BrowserRecorder(h.getDevTools());
        } catch (Throwable t) {
            LOG.log(Level.WARNING, "Could not obtain DevTools session; recording disabled.", t);
            BrowserRecorder r = new BrowserRecorder(null);
            r.disabled = true;
            return r;
        }
    }

    /**
     * Begins receiving screencast frames.  Idempotent — calling start while
     * already running clears the frame buffer and restarts capture, so each
     * test method can record from a clean slate.  Never throws.
     */
    public void start() {
        if (disabled) return;
        try {
            if (running.get()) {
                stopScreencastQuiet();
            }
            frames.clear();
            droppedFrames.set(0);
            devTools.createSessionIfThereIsNotOne();

            if (!listenerRegistered) {
                devTools.addListener(Page.screencastFrame(), this::onFrame);
                listenerRegistered = true;
            }

            devTools.send(Page.startScreencast(
                    Optional.of(Page.StartScreencastFormat.JPEG),
                    Optional.of(60),    // quality 0–100
                    Optional.of(960),   // maxWidth
                    Optional.of(540),   // maxHeight
                    Optional.of(6)));   // everyNthFrame  (~5 fps from 30 fps source)
            running.set(true);
        } catch (Throwable t) {
            LOG.log(Level.WARNING, "Could not start screencast; recording disabled.", t);
            disabled = true;
        }
    }

    private void onFrame(ScreencastFrame frame) {
        Integer sessionId = frame.getSessionId();
        try {
            byte[] jpeg = Base64.getDecoder().decode(frame.getData());
            if (frames.size() >= MAX_BUFFERED_FRAMES) {
                frames.pollFirst();
                droppedFrames.incrementAndGet();
            }
            frames.add(jpeg);
        } catch (Throwable t) {
            LOG.log(Level.FINE, "Failed to buffer screencast frame", t);
        }
        try {
            if (sessionId != null) {
                devTools.send(Page.screencastFrameAck(sessionId));
            }
        } catch (Throwable t) {
            LOG.log(Level.FINE, "screencastFrameAck failed", t);
        }
    }

    /**
     * Stops the screencast and encodes the buffered frames to MP4.  Returns
     * an empty array if recording is disabled, no frames were captured, or
     * encoding failed for any reason.  Never throws.
     */
    public byte[] stopAndEncode() {
        if (disabled) return new byte[0];
        stopScreencastQuiet();
        running.set(false);

        if (frames.isEmpty()) return new byte[0];
        if (droppedFrames.get() > 0) {
            LOG.warning("BrowserRecorder dropped " + droppedFrames.get()
                    + " oldest frames (buffer cap " + MAX_BUFFERED_FRAMES + ")");
        }
        try {
            return encodeWithFfmpeg();
        } catch (Throwable t) {
            LOG.log(Level.WARNING, "ffmpeg encode failed; no recording attached.", t);
            return new byte[0];
        } finally {
            frames.clear();
        }
    }

    /**
     * Stops, encodes, and — if the result is non-empty — hands the MP4 to
     * {@link EvidenceAttachmentExtension}.  The extension flushes attachments
     * only on test failure or when {@code KEEP_EVIDENCE=true}, so the
     * recording disappears for clean passing runs.
     */
    public void stopAndAttach(String name) {
        try {
            byte[] mp4 = stopAndEncode();
            if (mp4.length == 0) return;
            EvidenceAttachmentExtension.add(name + ".mp4", "video/mp4", ".mp4", mp4);
        } catch (Throwable t) {
            LOG.log(Level.WARNING, "stopAndAttach failed for " + name, t);
        }
    }

    private void stopScreencastQuiet() {
        try {
            devTools.send(Page.stopScreencast());
        } catch (Throwable t) {
            LOG.log(Level.FINE, "Page.stopScreencast failed (likely already stopped)", t);
        }
    }

    private byte[] encodeWithFfmpeg() throws IOException, InterruptedException {
        ProcessBuilder pb = new ProcessBuilder(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "image2pipe", "-framerate", "5", "-i", "-",
                "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-crf", "28",
                "-pix_fmt", "yuv420p",
                "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-movflags", "+faststart",
                "-f", "mp4", "pipe:1");
        pb.redirectErrorStream(false);
        Process proc = pb.start();

        Thread stderrDrain = new Thread(() -> drainQuietly(proc.getErrorStream()),
                "ffmpeg-stderr-drain");
        stderrDrain.setDaemon(true);
        stderrDrain.start();

        Thread stdinFeeder = new Thread(() -> {
            try (OutputStream out = proc.getOutputStream()) {
                for (byte[] jpeg : frames) {
                    out.write(jpeg);
                }
                out.flush();
            } catch (IOException e) {
                LOG.log(Level.FINE, "ffmpeg stdin feeder closed early", e);
            }
        }, "ffmpeg-stdin-feeder");
        stdinFeeder.setDaemon(true);
        stdinFeeder.start();

        byte[] out;
        try (InputStream in = proc.getInputStream()) {
            out = in.readAllBytes();
        }

        if (!proc.waitFor(FFMPEG_TIMEOUT_SEC, TimeUnit.SECONDS)) {
            proc.destroyForcibly();
            throw new IOException("ffmpeg did not exit within " + FFMPEG_TIMEOUT_SEC + " s");
        }
        if (proc.exitValue() != 0) {
            throw new IOException("ffmpeg exited with code " + proc.exitValue());
        }
        return out;
    }

    private static void drainQuietly(InputStream stream) {
        try (InputStream s = stream) {
            byte[] buf = new byte[4096];
            while (s.read(buf) >= 0) { /* discard */ }
        } catch (IOException ignored) {
        }
    }

    /** Best-effort cleanup. Discards any pending frames without encoding. */
    @Override
    public void close() {
        if (disabled) return;
        try {
            stopScreencastQuiet();
        } finally {
            frames.clear();
            running.set(false);
        }
    }
}
