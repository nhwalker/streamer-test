package functests;

import functests.support.ServiceStack;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Order;

import java.time.Duration;

@Order(8)
@DisplayName("Archive endpoint (GET /archive)")
class ArchiveEndpointTest extends AbstractArchiveEndpointTest {

    @BeforeAll
    static void setup() throws Exception {
        stack = ServiceStack.getInstance();
        stack.awaitFirstSegment();
        client = java.net.http.HttpClient.newBuilder()
                                         .connectTimeout(Duration.ofSeconds(10))
                                         .build();
    }
}
