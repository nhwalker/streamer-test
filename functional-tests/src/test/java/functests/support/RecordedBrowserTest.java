package functests.support;

/**
 * Implemented by test classes that drive a Selenium {@link org.openqa.selenium.WebDriver}
 * and want each test method to be recorded by {@link BrowserRecorderExtension}.
 *
 * <p>The extension fetches the active recorder from the test instance via
 * {@link #browserRecorder()} so it can {@code start()} it before the test
 * runs and {@code stopAndAttach(...)} it afterwards.  Returning {@code null}
 * means recording is not yet initialized; the extension treats that as a
 * no-op so tests can be safely annotated even before their {@code @BeforeAll}
 * has constructed a recorder.
 */
public interface RecordedBrowserTest {
    /** The recorder for the currently-driven browser, or {@code null} if not yet built. */
    BrowserRecorder browserRecorder();
}
