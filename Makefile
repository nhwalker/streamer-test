# Build order: base must be built first; caster and service depend on it.
# hub has no GStreamer dependency and can be built independently.
.PHONY: all base caster service hub clean

BASE_TAG    ?= streamer-base:latest
CASTER_TAG  ?= desktop-caster:ci
SERVICE_TAG ?= desktop-stream-service:ci
HUB_TAG     ?= desktop-stream-hub:ci
BUILD       ?= podman build

all: caster service hub

base:
	$(BUILD) -t $(BASE_TAG) base/

caster: base
	$(BUILD) -t $(CASTER_TAG) caster/

service: base
	$(BUILD) -t $(SERVICE_TAG) service/

hub:
	$(BUILD) -t $(HUB_TAG) hub/

clean:
	podman rmi -f $(CASTER_TAG) $(SERVICE_TAG) $(HUB_TAG) $(BASE_TAG) 2>/dev/null || true
