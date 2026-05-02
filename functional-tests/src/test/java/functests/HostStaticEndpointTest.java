package functests;

import functests.support.ServiceStack;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Order;

import java.time.Duration;

@Order(7)
@DisplayName("Static endpoints — host mode (/, /top, /bottom)")
class HostStaticEndpointTest extends AbstractStaticEndpointTest {

    @BeforeAll
    static void setup() {
        stack  = ServiceStack.getHostInstance();
        client = java.net.http.HttpClient.newBuilder()
                                         .connectTimeout(Duration.ofSeconds(10))
                                         .build();
    }
}
