package functests;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Order;

/**
 * Runs the live-feed color suite with a 300 s archive segment duration.
 * Browser setup takes at most ~90 s and 4 flips take ~10 s, so all activity
 * occurs within the single active (not-yet-completed) segment.  The archive
 * test therefore verifies color ordering through a single MKV entry.
 */
@Order(5)
@DisplayName("Live feed — single active segment (4 flips, 300 s segments)")
class SingleSegmentLiveFeedColorTest extends AbstractLiveFeedColorTest {
    @Override protected int  numFlips()         { return 4; }
    @Override protected long flipHoldMs()       { return 1_500; }
    @Override protected int  archiveSegmentSec(){ return 300; }
}
