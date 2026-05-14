package functests;

import functests.support.ServiceStack;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Order;

import java.time.Duration;
import java.time.Instant;

@Order(9)
@DisplayName("Video endpoint (GET /video)")
class VideoEndpointTest extends AbstractVideoEndpointTest {

    @BeforeAll
    static void setup() throws Exception {
        stack      = ServiceStack.getInstance();
        stack.awaitFirstSegment();
        setupEpoch = Instant.now().getEpochSecond();
        client     = java.net.http.HttpClient.newBuilder()
                                             .connectTimeout(Duration.ofSeconds(10))
                                             .build();
    }
}
