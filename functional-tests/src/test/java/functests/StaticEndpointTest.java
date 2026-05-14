package functests;

import functests.support.ServiceStack;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Order;

import java.time.Duration;

@Order(7)
@DisplayName("Static endpoints (/, /top, /bottom)")
class StaticEndpointTest extends AbstractStaticEndpointTest {

    @BeforeAll
    static void setup() {
        stack  = ServiceStack.getInstance();
        client = java.net.http.HttpClient.newBuilder()
                                         .connectTimeout(Duration.ofSeconds(10))
                                         .build();
    }
}
