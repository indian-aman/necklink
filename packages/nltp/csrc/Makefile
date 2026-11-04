CC      ?= gcc
CFLAGS  ?= -O2 -std=c99 -Wall -Wextra -pedantic -Werror -I.
BUILD   ?= build

CORE_SRCS := nltp_core.c nltp_sha256.c
NET_SRCS  := nltp_net.c

.PHONY: all test clean espnow-sim

all: $(BUILD)/nltp_selftest $(BUILD)/nltp_cli

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/nltp_selftest: tools/nltp_selftest.c $(CORE_SRCS) nltp_core.h nltp_sha256.h nltp_wire.h | $(BUILD)
	$(CC) $(CFLAGS) -o $@ tools/nltp_selftest.c $(CORE_SRCS)

$(BUILD)/nltp_cli: tools/nltp_cli.c $(CORE_SRCS) $(NET_SRCS) nltp_core.h nltp_net.h nltp_sha256.h nltp_wire.h | $(BUILD)
	$(CC) $(CFLAGS) -o $@ tools/nltp_cli.c $(CORE_SRCS) $(NET_SRCS)

test: $(BUILD)/nltp_selftest
	./$(BUILD)/nltp_selftest

# Host simulation of the ESP-NOW transport (nltp_espnow.h) on a fake radio; no ESP32 needed.
SIMDIR   := tools/espnow_sim
SIMFLAGS := -O1 -g -std=c99 -Wall -Wextra -Wno-unused-function -Wno-unused-parameter -DESP_PLATFORM \
            -DNLTP_RX_SLOTS=2 -DNLTP_RX_MAX_GENS=64 -DNLTP_ESPNOW_MAX_PEERS=2 -I$(SIMDIR)/stubs -I.
SIMSRC   := nltp_core.c nltp_sha256.c $(SIMDIR)/radio.c $(SIMDIR)/test_hub.c

$(BUILD)/espnow_sim: $(SIMSRC) $(SIMDIR)/devices.c nltp_espnow.h nltp_core.h nltp_wire.h | $(BUILD)
	$(CC) $(SIMFLAGS) -DROLE_HUB -c $(SIMDIR)/devices.c -o $(BUILD)/sim_hub.o
	$(CC) $(SIMFLAGS) -DNODE_ID=1 -c $(SIMDIR)/devices.c -o $(BUILD)/sim_n1.o
	$(CC) $(SIMFLAGS) -DNODE_ID=2 -c $(SIMDIR)/devices.c -o $(BUILD)/sim_n2.o
	$(CC) $(SIMFLAGS) -DNODE_ID=3 -c $(SIMDIR)/devices.c -o $(BUILD)/sim_n3.o
	$(CC) $(SIMFLAGS) -o $@ $(SIMSRC) $(BUILD)/sim_hub.o $(BUILD)/sim_n1.o $(BUILD)/sim_n2.o $(BUILD)/sim_n3.o

espnow-sim: $(BUILD)/espnow_sim
	./$(BUILD)/espnow_sim e
	./$(BUILD)/espnow_sim p

clean:
	rm -rf $(BUILD)
