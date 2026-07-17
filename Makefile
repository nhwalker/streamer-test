# service and hub build independently — no shared base image, no source
# compilation (the GStreamer/Rust build stage was removed in the ffmpeg +
# MediaMTX rewrite).
.PHONY: all service hub clean lint test functional

SERVICE_TAG ?= desktop-stream-service:ci
HUB_TAG     ?= desktop-stream-hub:ci
BUILD       ?= podman build

all: service hub

service:
	$(BUILD) -t $(SERVICE_TAG) service/

hub:
	$(BUILD) -t $(HUB_TAG) hub/

clean:
	podman rmi -f $(SERVICE_TAG) $(HUB_TAG) 2>/dev/null || true

lint:
	ruff check service/ tests/ hub/
	mypy --ignore-missing-imports service/

# Unit tests (pure Python, no containers or browser).
test:
	python3 -m pytest tests/

# Container/browser integration tests (Java). Needs a docker daemon, Xvfb,
# x11-apps, and Chrome on the host; builds the images first.
functional: service hub
	cd functional-tests && SERVICE_IMAGE=$(SERVICE_TAG) HUB_IMAGE=$(HUB_TAG) ./gradlew test
