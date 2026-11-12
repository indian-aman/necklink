CC      ?= gcc
CFLAGS  ?= -O2 -std=c99 -Wall -Wextra -pedantic -Werror -I.
BUILD   ?= build

CORE_SRCS := nltp_core.c nltp_sha256.c
NET_SRCS  := nltp_net.c

.PHONY: all test clean espnow-sim sketch-runner

all: $(BUILD)/nltp_selftest $(BUILD)/nltp_txonly_selftest $(BUILD)/nltp_cli

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/nltp_selftest: tools/nltp_selftest.c $(CORE_SRCS) nltp_core.h nltp_sha256.h nltp_wire.h | $(BUILD)
	$(CC) $(CFLAGS) -o $@ tools/nltp_selftest.c $(CORE_SRCS)

$(BUILD)/nltp_txonly_selftest: tools/nltp_txonly_selftest.c $(CORE_SRCS) nltp_core.h nltp_sha256.h nltp_wire.h | $(BUILD)
	$(CC) $(CFLAGS) -DNLTP_TX_ONLY=1 -o $@ tools/nltp_txonly_selftest.c $(CORE_SRCS)

$(BUILD)/nltp_cli: tools/nltp_cli.c $(CORE_SRCS) $(NET_SRCS) nltp_core.h nltp_net.h nltp_sha256.h nltp_wire.h | $(BUILD)
	$(CC) $(CFLAGS) -o $@ tools/nltp_cli.c $(CORE_SRCS) $(NET_SRCS)

test: $(BUILD)/nltp_selftest $(BUILD)/nltp_txonly_selftest
	./$(BUILD)/nltp_selftest
	./$(BUILD)/nltp_txonly_selftest

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

# The example sketches ESP32_ESPNow_Sensor_Node + ESP32_ESPNow_Gateway as two simulated ESP32s; the
# gateway's Wi-Fi side is a real UDP socket. Driven by scripts/esp32_sketch_e2e_test.py.
ARDEX    := ../arduino/NLTP/examples
ARDFLAGS := -O1 -g -std=gnu++17 -Wall -Wextra -Wno-unused-parameter -Wno-unused-function -DESP_PLATFORM \
            -I$(SIMDIR)/arduino -I$(SIMDIR)/stubs -I../arduino/NLTP/src -include $(SIMDIR)/arduino/Arduino.h

$(BUILD)/sketch_runner: $(SIMDIR)/arduino/*.h $(SIMDIR)/arduino/sketch_runner.cpp $(SIMDIR)/radio.c \
                        $(ARDEX)/ESP32_ESPNow_Sensor_Node/*.ino $(ARDEX)/ESP32_ESPNow_Gateway/*.ino nltp_espnow.h | $(BUILD)
	$(CXX) $(ARDFLAGS) -Dsetup=sensor_setup -Dloop=sensor_loop -x c++ -c $(ARDEX)/ESP32_ESPNow_Sensor_Node/ESP32_ESPNow_Sensor_Node.ino -o $(BUILD)/sk_sensor.o
	$(CXX) $(ARDFLAGS) -Dsetup=gateway_setup -Dloop=gateway_loop -x c++ -c $(ARDEX)/ESP32_ESPNow_Gateway/ESP32_ESPNow_Gateway.ino -o $(BUILD)/sk_gateway.o
	$(CXX) $(ARDFLAGS) -c $(SIMDIR)/arduino/sketch_runner.cpp -o $(BUILD)/sk_runner.o
	$(CC) -O1 -g -std=c99 -DESP_PLATFORM -I$(SIMDIR)/stubs -I. -c $(SIMDIR)/radio.c -o $(BUILD)/sk_radio.o
	$(CC) -O1 -g -std=c99 -DESP_PLATFORM -I$(SIMDIR)/stubs -I. -c nltp_core.c -o $(BUILD)/sk_core.o
	$(CC) -O1 -g -std=c99 -DESP_PLATFORM -I. -c nltp_sha256.c -o $(BUILD)/sk_sha.o
	$(CC) -O1 -g -std=c99 -DESP_PLATFORM -I. -c ../arduino/NLTP/src/nltp_net.c -o $(BUILD)/sk_net.o
	$(CXX) -o $@ $(BUILD)/sk_sensor.o $(BUILD)/sk_gateway.o $(BUILD)/sk_runner.o $(BUILD)/sk_radio.o $(BUILD)/sk_core.o $(BUILD)/sk_sha.o $(BUILD)/sk_net.o

sketch-runner: $(BUILD)/sketch_runner

clean:
	rm -rf $(BUILD)
