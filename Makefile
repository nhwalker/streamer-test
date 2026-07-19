# Two-phase build:
#
#   make setup            # ONLINE phase — base images with all third-party
#                         # FOSS (Containerfile.base) + Gradle dependency cache
#   make service hub      # OFFLINE-capable app phase — COPYs the in-repo
#                         # application onto the prebuilt bases
#   make functional       # OFFLINE-capable — runs the Java suite from the
#                         # warmed Gradle cache
#
# OFFLINE=1 enforces the air gap: podman builds run with --pull=never (the
# base images must already be in local storage — built by `make setup`, or
# podman load'ed from a tarball) and gradle runs with --offline (every
# dependency must already be in the cache from `make setup`).
.PHONY: all setup service-base hub-base gradle-deps service hub clean lint test functional

SERVICE_TAG      ?= desktop-stream-service:ci
HUB_TAG          ?= desktop-stream-hub:ci
SERVICE_BASE_TAG ?= desktop-stream-service-base:ci
HUB_BASE_TAG     ?= desktop-stream-hub-base:ci
BUILD            ?= podman build

OFFLINE ?= 0
ifeq ($(OFFLINE),1)
BUILD_FLAGS      += --pull=never
GRADLE_FLAGS     += --offline
# Offline: never rebuild the bases (they may exist only as loaded images,
# with no build cache) — just require them to be present.
SERVICE_BASE_DEP :=
HUB_BASE_DEP     :=
else
SERVICE_BASE_DEP := service-base
HUB_BASE_DEP     := hub-base
endif

all: service hub

# ── Online setup phase ───────────────────────────────────────────────────────

setup: service-base hub-base gradle-deps

service-base:
	$(BUILD) -t $(SERVICE_BASE_TAG) -f service/Containerfile.base service/

hub-base:
	$(BUILD) -t $(HUB_BASE_TAG) -f hub/Containerfile.base hub/

# Warms the Gradle cache (wrapper distribution + every dependency) so the
# functional suite can later run with --offline.
gradle-deps:
	cd functional-tests && ./gradlew resolveDependencies

# ── Offline-capable app phase ────────────────────────────────────────────────

service: $(SERVICE_BASE_DEP)
	$(BUILD) $(BUILD_FLAGS) --build-arg BASE_IMAGE=$(SERVICE_BASE_TAG) \
		-t $(SERVICE_TAG) service/

hub: $(HUB_BASE_DEP)
	$(BUILD) $(BUILD_FLAGS) --build-arg BASE_IMAGE=$(HUB_BASE_TAG) \
		-t $(HUB_TAG) hub/

clean:
	podman rmi -f $(SERVICE_TAG) $(HUB_TAG) \
		$(SERVICE_BASE_TAG) $(HUB_BASE_TAG) 2>/dev/null || true

lint:
	ruff check service/ tests/ hub/
	mypy --ignore-missing-imports service/

# Unit tests (pure Python, no containers or browser).
test:
	python3 -m pytest tests/

# Container/browser integration tests (Java). Needs a docker daemon, Xvfb,
# x11-apps, and Chrome on the host; builds the images first.
functional: service hub
	cd functional-tests && SERVICE_IMAGE=$(SERVICE_TAG) HUB_IMAGE=$(HUB_TAG) ./gradlew $(GRADLE_FLAGS) test
