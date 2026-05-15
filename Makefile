# Build order: base must be built first; service depends on it.
# hub and viewer have no GStreamer dependency and can be built independently.
.PHONY: all base service hub viewer clean

BASE_TAG    ?= streamer-base:latest
SERVICE_TAG ?= desktop-stream-service:ci
HUB_TAG     ?= desktop-stream-hub:ci
VIEWER_TAG  ?= desktop-stream-viewer:ci
BUILD       ?= podman build

all: service hub viewer

base:
	$(BUILD) -t $(BASE_TAG) base/

service: base
	$(BUILD) -t $(SERVICE_TAG) service/

hub:
	$(BUILD) -t $(HUB_TAG) hub/

viewer:
	$(BUILD) -t $(VIEWER_TAG) viewer/

clean:
	podman rmi -f $(SERVICE_TAG) $(HUB_TAG) $(VIEWER_TAG) $(BASE_TAG) 2>/dev/null || true
