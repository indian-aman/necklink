CC      ?= gcc
CFLAGS  ?= -O2 -std=c99 -Wall -Wextra -pedantic -Werror
BUILD   := build
TARGET  := $(BUILD)/neckcord_selftest

.PHONY: all clean test

all: $(TARGET)

$(BUILD):
	mkdir -p $(BUILD)

$(TARGET): neckcord.c neckcord.h tools/neckcord_selftest.c | $(BUILD)
	$(CC) $(CFLAGS) -I. neckcord.c tools/neckcord_selftest.c -o $(TARGET)

test: $(TARGET)
	./$(TARGET)

clean:
	rm -rf $(BUILD)
