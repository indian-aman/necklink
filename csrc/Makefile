CC      ?= gcc
CFLAGS  ?= -O2 -std=c99 -Wall -Wextra -pedantic -Werror -I.
BUILD   ?= build

CORE_SRCS := necksense.c

.PHONY: all test clean

all: $(BUILD)/necksense_selftest

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/necksense_selftest: tools/necksense_selftest.c $(CORE_SRCS) necksense.h | $(BUILD)
	$(CC) $(CFLAGS) -o $@ tools/necksense_selftest.c $(CORE_SRCS)

test: $(BUILD)/necksense_selftest
	./$(BUILD)/necksense_selftest

clean:
	rm -rf $(BUILD)
