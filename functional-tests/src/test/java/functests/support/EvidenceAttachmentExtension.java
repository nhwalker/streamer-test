package functests.support;

import io.qameta.allure.Allure;
import org.junit.jupiter.api.extension.AfterTestExecutionCallback;
import org.junit.jupiter.api.extension.BeforeTestExecutionCallback;
import org.junit.jupiter.api.extension.ExtensionContext;

import java.io.ByteArrayInputStream;
import java.util.ArrayList;
import java.util.List;

/**
 * Captures test evidence and attaches it to the Allure report when the test
 * fails or when {@code KEEP_EVIDENCE=true} is set in the environment.
 *
 * <p>Tests register evidence during execution by calling
 * {@link #add(String, String, String, byte[])}.  After each test completes,
 * this extension inspects the test outcome:
 *
 * <ul>
 *   <li>If the test failed, all pending evidence is attached to the Allure
 *       report so the failure can be diagnosed from the captured artifacts.</li>
 *   <li>If {@code KEEP_EVIDENCE=true} is set in the environment, evidence is
 *       attached even on success — useful for debugging flaky tests or for
 *       collecting baseline artifacts.</li>
 *   <li>Otherwise, evidence is discarded.</li>
 * </ul>
 */
public final class EvidenceAttachmentExtension
        implements BeforeTestExecutionCallback, AfterTestExecutionCallback {

    private static final boolean KEEP_EVIDENCE =
            "true".equalsIgnoreCase(System.getenv("KEEP_EVIDENCE"));

    private static final ExtensionContext.Namespace NAMESPACE =
            ExtensionContext.Namespace.create(EvidenceAttachmentExtension.class);

    private static final ThreadLocal<List<Evidence>> CURRENT = new ThreadLocal<>();

    public record Evidence(String name, String mimeType, String extension, byte[] content) {}

    /**
     * Registers an evidence artifact for the currently running test.  The
     * artifact is buffered in memory and only attached to the Allure report
     * if the test fails or {@code KEEP_EVIDENCE=true}.  Safe to call when no
     * test is running (the call becomes a no-op).
     *
     * @param name      attachment display name in the Allure report
     * @param mimeType  MIME type, e.g. {@code "video/mp4"}
     * @param extension file extension including the leading dot, e.g. {@code ".mp4"}
     * @param content   raw bytes
     */
    public static void add(String name, String mimeType, String extension, byte[] content) {
        List<Evidence> list = CURRENT.get();
        if (list == null) return;
        list.add(new Evidence(name, mimeType, extension, content));
    }

    @Override
    public void beforeTestExecution(ExtensionContext ctx) {
        List<Evidence> list = new ArrayList<>();
        ctx.getStore(NAMESPACE).put("evidence", list);
        CURRENT.set(list);
    }

    @Override
    public void afterTestExecution(ExtensionContext ctx) {
        boolean failed = ctx.getExecutionException().isPresent();
        @SuppressWarnings("unchecked")
        List<Evidence> list = (List<Evidence>) ctx.getStore(NAMESPACE).remove("evidence");
        CURRENT.remove();
        if (list == null) return;
        if (!failed && !KEEP_EVIDENCE) return;
        for (Evidence e : list) {
            Allure.addAttachment(
                    e.name(), e.mimeType(),
                    new ByteArrayInputStream(e.content()), e.extension());
        }
    }
}
