package functests;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Order;

@Order(4)
@DisplayName("Live feed — default params (10 flips, 20 s segments)")
class DefaultLiveFeedColorTest extends AbstractLiveFeedColorTest {
    @Override protected int  numFlips()         { return 10; }
    @Override protected long flipHoldMs()       { return 1_500; }
    @Override protected int  archiveSegmentSec(){ return 20; }
}
