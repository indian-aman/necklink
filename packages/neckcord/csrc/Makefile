CC       ?= gcc
CFLAGS   ?= -O2 -std=c99 -Wall -Wextra -pedantic -Werror
SANFLAGS ?= -O1 -g -std=c99 -Wall -Wextra -pedantic -Werror -fsanitize=address,undefined -fno-sanitize-recover=undefined
BUILD    := build
TARGET   := $(BUILD)/neckcord_selftest
SANTGT   := $(BUILD)/neckcord_selftest_san

.PHONY: all clean test test-sanitize

all: $(TARGET)

$(BUILD):
	mkdir -p $(BUILD)

$(TARGET): neckcord.c neckcord.h tools/neckcord_selftest.c | $(BUILD)
	$(CC) $(CFLAGS) -I. neckcord.c tools/neckcord_selftest.c -o $(TARGET)

$(SANTGT): neckcord.c neckcord.h tools/neckcord_selftest.c | $(BUILD)
	$(CC) $(SANFLAGS) -I. neckcord.c tools/neckcord_selftest.c -o $(SANTGT)

test: $(TARGET)
	./$(TARGET)

test-sanitize: $(SANTGT)
	./$(SANTGT)

clean:
	rm -rf $(BUILD)
