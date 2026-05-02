package functests;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Order;

/**
 * Runs the live-feed color suite with 8 s archive segments.
 * Ten flips at ~2.5 s each span roughly 25 s, producing at least 3 completed
 * segments plus one active segment within the flip window.
 */
@Order(6)
@DisplayName("Live feed — 3+ completed segments (10 flips, 8 s segments)")
class MultiSegmentLiveFeedColorTest extends AbstractLiveFeedColorTest {
    @Override protected int  numFlips()         { return 10; }
    @Override protected long flipHoldMs()       { return 1_500; }
    @Override protected int  archiveSegmentSec(){ return 8; }
}
