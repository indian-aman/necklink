CC       ?= gcc
CFLAGS   ?= -O2 -std=c99 -Wall -Wextra -pedantic -Werror -DNL_CNS_TINY=1 -DNS_TINY=1
INCLUDES := -I. -Iorgans

SRCS := \
	necklink_cns.c \
	nl_mavlink.c \
	organs/necksight.c \
	organs/neckcompass.c \
	organs/neckcord.c \
	organs/necksense.c

SELFTEST_BIN := necklink_cns_selftest
ASAN_BIN     := necklink_cns_selftest_asan

.PHONY: all test sanitize clean

all: $(SELFTEST_BIN)

$(SELFTEST_BIN): $(SRCS) tools/necklink_cns_selftest.c necklink_cns.h nl_mavlink.h
	$(CC) $(CFLAGS) $(INCLUDES) $(SRCS) tools/necklink_cns_selftest.c -o $(SELFTEST_BIN)

test: $(SELFTEST_BIN)
	./$(SELFTEST_BIN)

sanitize: $(SRCS) tools/necklink_cns_selftest.c
	$(CC) -O1 -g -std=c99 -Wall -Wextra -pedantic -Werror -DNL_CNS_TINY=1 -DNS_TINY=1 \
		-fsanitize=address,undefined -fno-sanitize-recover=undefined \
		$(INCLUDES) $(SRCS) tools/necklink_cns_selftest.c -o $(ASAN_BIN)
	./$(ASAN_BIN)

clean:
	rm -f $(SELFTEST_BIN) $(ASAN_BIN) *.o *.so *.dll
