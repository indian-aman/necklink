CC      ?= gcc
CFLAGS  ?= -O2 -std=c99 -Wall -Wextra -pedantic -Werror -I.
BUILD   ?= build

CORE_SRCS := necksight.c

.PHONY: all test sanitize clean

all: $(BUILD)/necksight_selftest

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/necksight_selftest: tools/necksight_selftest.c $(CORE_SRCS) necksight.h necksight_banc_weights.h | $(BUILD)
	$(CC) $(CFLAGS) -o $@ tools/necksight_selftest.c $(CORE_SRCS)

$(BUILD)/necksight_selftest_san: tools/necksight_selftest.c $(CORE_SRCS) necksight.h necksight_banc_weights.h | $(BUILD)
	$(CC) -O2 -g -std=c99 -Wall -Wextra -pedantic -Werror -fsanitize=address,undefined -fno-sanitize-recover=undefined -I. -o $@ tools/necksight_selftest.c $(CORE_SRCS)

test: $(BUILD)/necksight_selftest
	./$(BUILD)/necksight_selftest

sanitize: $(BUILD)/necksight_selftest_san
	./$(BUILD)/necksight_selftest_san

clean:
	rm -rf $(BUILD)
