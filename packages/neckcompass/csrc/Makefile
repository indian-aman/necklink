CC      ?= gcc
CFLAGS  ?= -O2 -std=c99 -Wall -Wextra -pedantic -Werror -I.
BUILD   ?= build

CORE_SRCS := neckcompass.c

.PHONY: all test clean

all: $(BUILD)/neckcompass_selftest

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/neckcompass_selftest: tools/neckcompass_selftest.c $(CORE_SRCS) neckcompass.h | $(BUILD)
	$(CC) $(CFLAGS) -o $@ tools/neckcompass_selftest.c $(CORE_SRCS)

test: $(BUILD)/neckcompass_selftest
	./$(BUILD)/neckcompass_selftest

clean:
	rm -rf $(BUILD)
