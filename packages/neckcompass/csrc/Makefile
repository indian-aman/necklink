CC       ?= gcc
CFLAGS   ?= -O2 -std=c99 -Wall -Wextra -pedantic -Werror -I.
SANFLAGS ?= -O1 -g -std=c99 -Wall -Wextra -pedantic -Werror -fsanitize=address,undefined -fno-sanitize-recover=undefined -I.
BUILD    ?= build

CORE_SRCS := neckcompass.c
CORE_HDRS := neckcompass.h neckcompass_banc_weights.h

.PHONY: all test sanitize clean

all: $(BUILD)/neckcompass_selftest

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/neckcompass_selftest: tools/neckcompass_selftest.c $(CORE_SRCS) $(CORE_HDRS) | $(BUILD)
	$(CC) $(CFLAGS) -o $@ tools/neckcompass_selftest.c $(CORE_SRCS)

$(BUILD)/neckcompass_selftest_san: tools/neckcompass_selftest.c $(CORE_SRCS) $(CORE_HDRS) | $(BUILD)
	$(CC) $(SANFLAGS) -o $@ tools/neckcompass_selftest.c $(CORE_SRCS)

test: $(BUILD)/neckcompass_selftest
	./$(BUILD)/neckcompass_selftest

sanitize: $(BUILD)/neckcompass_selftest_san
	./$(BUILD)/neckcompass_selftest_san

clean:
	rm -rf $(BUILD)
