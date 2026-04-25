# Build order: base must be built first; caster and service depend on it.
.PHONY: all base caster service clean

BASE_TAG    ?= streamer-base:latest
CASTER_TAG  ?= desktop-caster:ci
SERVICE_TAG ?= desktop-stream-service:ci
BUILD       ?= podman build

all: caster service

base:
	$(BUILD) -t $(BASE_TAG) base/

caster: base
	$(BUILD) -t $(CASTER_TAG) caster/

service: base
	$(BUILD) -t $(SERVICE_TAG) service/

clean:
	podman rmi -f $(CASTER_TAG) $(SERVICE_TAG) $(BASE_TAG) 2>/dev/null || true
