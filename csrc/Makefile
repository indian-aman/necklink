CC      ?= gcc
CFLAGS  ?= -O2 -std=c99 -Wall -Wextra -pedantic -Werror -I.
BUILD   ?= build

CORE_SRCS := necksight.c

.PHONY: all test clean

all: $(BUILD)/necksight_selftest

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/necksight_selftest: tools/necksight_selftest.c $(CORE_SRCS) necksight.h | $(BUILD)
	$(CC) $(CFLAGS) -o $@ tools/necksight_selftest.c $(CORE_SRCS)

test: $(BUILD)/necksight_selftest
	./$(BUILD)/necksight_selftest

clean:
	rm -rf $(BUILD)
