import org.gradle.api.tasks.testing.logging.TestExceptionFormat

// To run locally: install Gradle 8.x, then from the tests/ directory:
//   gradle test
// Or generate a wrapper first:
//   gradle wrapper --gradle-version 8.11.1
//   ./gradlew test

plugins {
    java
}

java {
    toolchain {
        languageVersion.set(JavaLanguageVersion.of(17))
    }
}

repositories {
    mavenCentral()
}

val testcontainersVersion = "1.20.4"
val seleniumVersion       = "4.27.0"
val junitVersion          = "5.11.4"

dependencies {
    testImplementation("org.junit.jupiter:junit-jupiter:$junitVersion")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")

    // Testcontainers core + Selenium module
    testImplementation("org.testcontainers:testcontainers:$testcontainersVersion")
    testImplementation("org.testcontainers:selenium:$testcontainersVersion")

    // Selenium Java (includes RemoteWebDriver, ChromeOptions, WebDriverWait, etc.)
    testImplementation("org.seleniumhq.selenium:selenium-java:$seleniumVersion")

    // SLF4J backend — silences testcontainers "no SLF4J provider" warnings
    testRuntimeOnly("org.slf4j:slf4j-simple:2.0.16")
}

tasks.test {
    useJUnitPlatform()

    // Integration tests can take up to 15 minutes on a cold cache (Rust build).
    timeout.set(java.time.Duration.ofMinutes(15))

    testLogging {
        events("passed", "failed", "skipped")
        showStandardStreams = true
        exceptionFormat = TestExceptionFormat.FULL
    }
}
