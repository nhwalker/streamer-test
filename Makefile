# service and hub build independently — no shared base image, no source
# compilation (the GStreamer/Rust build stage was removed in the ffmpeg +
# MediaMTX rewrite).
.PHONY: all service hub clean

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
