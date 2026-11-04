CC      ?= gcc
CFLAGS  ?= -O2 -std=c99 -Wall -Wextra -pedantic -Werror -I.
BUILD   ?= build

CORE_SRCS := nltp_core.c nltp_sha256.c
NET_SRCS  := nltp_net.c

.PHONY: all test clean

all: $(BUILD)/nltp_selftest $(BUILD)/nltp_cli

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/nltp_selftest: tools/nltp_selftest.c $(CORE_SRCS) nltp_core.h nltp_sha256.h nltp_wire.h | $(BUILD)
	$(CC) $(CFLAGS) -o $@ tools/nltp_selftest.c $(CORE_SRCS)

$(BUILD)/nltp_cli: tools/nltp_cli.c $(CORE_SRCS) $(NET_SRCS) nltp_core.h nltp_net.h nltp_sha256.h nltp_wire.h | $(BUILD)
	$(CC) $(CFLAGS) -o $@ tools/nltp_cli.c $(CORE_SRCS) $(NET_SRCS)

test: $(BUILD)/nltp_selftest
	./$(BUILD)/nltp_selftest

clean:
	rm -rf $(BUILD)
