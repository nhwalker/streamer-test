package functests;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Order;

/**
 * Runs the live-feed color suite with a 60 s archive segment duration.
 * The first segment completes at ~60 s, which is within the 90 s
 * awaitFirstSegment() timeout, so stage_segments can include the active
 * segment.  4 flips (~6 s) all land in that single active segment.
 */
@Order(5)
@DisplayName("Live feed — single active segment (4 flips, 60 s segments)")
class SingleSegmentLiveFeedColorTest extends AbstractLiveFeedColorTest {
    @Override protected int  numFlips()         { return 4; }
    @Override protected long flipHoldMs()       { return 1_500; }
    @Override protected int  archiveSegmentSec(){ return 60; }
}
