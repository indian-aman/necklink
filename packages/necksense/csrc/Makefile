CC      ?= gcc
CFLAGS  ?= -O2 -std=c99 -Wall -Wextra -pedantic -Werror -I.
SANFLAGS ?= -O1 -g -std=c99 -Wall -Wextra -pedantic -Werror -fsanitize=address,undefined -fno-sanitize-recover=undefined -I.
BUILD   ?= build

CORE_SRCS := necksense.c

.PHONY: all test test-tiny sanitize clean

all: $(BUILD)/necksense_selftest $(BUILD)/necksense_selftest_tiny

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/necksense_selftest: tools/necksense_selftest.c $(CORE_SRCS) necksense.h | $(BUILD)
	$(CC) $(CFLAGS) -o $@ tools/necksense_selftest.c $(CORE_SRCS)

$(BUILD)/necksense_selftest_tiny: tools/necksense_selftest.c $(CORE_SRCS) necksense.h | $(BUILD)
	$(CC) $(CFLAGS) -DNS_TINY=1 -o $@ tools/necksense_selftest.c $(CORE_SRCS)

test: all
	./$(BUILD)/necksense_selftest
	./$(BUILD)/necksense_selftest_tiny

test-tiny: $(BUILD)/necksense_selftest_tiny
	./$(BUILD)/necksense_selftest_tiny

sanitize: tools/necksense_selftest.c $(CORE_SRCS) necksense.h | $(BUILD)
	$(CC) $(SANFLAGS) -o $(BUILD)/necksense_selftest_san tools/necksense_selftest.c $(CORE_SRCS)
	$(CC) $(SANFLAGS) -DNS_TINY=1 -o $(BUILD)/necksense_selftest_tiny_san tools/necksense_selftest.c $(CORE_SRCS)
	./$(BUILD)/necksense_selftest_san
	./$(BUILD)/necksense_selftest_tiny_san

clean:
	rm -rf $(BUILD)
