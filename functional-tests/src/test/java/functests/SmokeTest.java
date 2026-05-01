package functests;

import io.qameta.allure.Description;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertTrue;

class SmokeTest {

    @Test
    @Description("Verify the test harness is wired up correctly")
    void harnessBoots() {
        assertTrue(true);
    }
}
