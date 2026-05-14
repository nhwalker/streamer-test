# Build order: base must be built first; service depends on it.
# hub has no GStreamer dependency and can be built independently.
.PHONY: all base service hub clean

BASE_TAG    ?= streamer-base:latest
SERVICE_TAG ?= desktop-stream-service:ci
HUB_TAG     ?= desktop-stream-hub:ci
BUILD       ?= podman build

all: service hub

base:
	$(BUILD) -t $(BASE_TAG) base/

service: base
	$(BUILD) -t $(SERVICE_TAG) service/

hub:
	$(BUILD) -t $(HUB_TAG) hub/

clean:
	podman rmi -f $(SERVICE_TAG) $(HUB_TAG) $(BASE_TAG) 2>/dev/null || true
