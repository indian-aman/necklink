#!/usr/bin/env bash
dpkg -L ngtcp2-server | grep -E "bin/" ; dpkg -L libudt-dev | grep -E "\.so|\.a$" ; ls /usr/include/udt/ | tr '\n' ' '; echo
gtlsclient --help 2>&1 | head -3; srt-file-transmit -h 2>&1 | head -5
