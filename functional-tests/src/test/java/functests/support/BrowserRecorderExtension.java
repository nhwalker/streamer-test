package functests.support;

import org.junit.jupiter.api.extension.AfterTestExecutionCallback;
import org.junit.jupiter.api.extension.BeforeTestExecutionCallback;
import org.junit.jupiter.api.extension.ExtensionContext;

import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * JUnit 5 extension that drives a {@link BrowserRecorder} for tests
 * implementing {@link RecordedBrowserTest}.  Starts the recorder before each
 * test method and stops + attaches the encoded video afterwards.
 *
 * <p>Must be registered on the test class <em>after</em>
 * {@link EvidenceAttachmentExtension} so that, by JUnit 5's reverse-of-
 * registration ordering for {@code AfterTestExecutionCallback}, this
 * extension's {@code afterTestExecution} runs <em>first</em> on the way out
 * — it adds the encoded MP4 to the evidence list, and then
 * {@link EvidenceAttachmentExtension} flushes (or discards) based on the
 * test outcome and {@code KEEP_EVIDENCE}.
 *
 * <pre>{@code
 * @ExtendWith({ EvidenceAttachmentExtension.class, BrowserRecorderExtension.class })
 * abstract class MyBrowserTest implements RecordedBrowserTest { ... }
 * }</pre>
 */
public final class BrowserRecorderExtension
        implements BeforeTestExecutionCallback, AfterTestExecutionCallback {

    private static final Logger LOG = Logger.getLogger(BrowserRecorderExtension.class.getName());

    @Override
    public void beforeTestExecution(ExtensionContext ctx) {
        BrowserRecorder recorder = lookup(ctx);
        if (recorder == null) return;
        try {
            recorder.start();
        } catch (Throwable t) {
            LOG.log(Level.WARNING, "BrowserRecorder.start() failed", t);
        }
    }

    @Override
    public void afterTestExecution(ExtensionContext ctx) {
        BrowserRecorder recorder = lookup(ctx);
        if (recorder == null) return;
        String name = "browser-" + sanitize(ctx.getDisplayName());
        try {
            recorder.stopAndAttach(name);
        } catch (Throwable t) {
            LOG.log(Level.WARNING, "BrowserRecorder.stopAndAttach() failed", t);
        }
    }

    private static BrowserRecorder lookup(ExtensionContext ctx) {
        Object instance = ctx.getTestInstance().orElse(null);
        if (instance instanceof RecordedBrowserTest t) {
            return t.browserRecorder();
        }
        return null;
    }

    private static String sanitize(String displayName) {
        String trimmed = displayName.replaceAll("\\W+", "-").replaceAll("(^-+)|(-+$)", "");
        return trimmed.isEmpty() ? "test" : trimmed;
    }
}
