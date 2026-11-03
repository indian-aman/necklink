#!/usr/bin/env bash
echo "== gtlsclient"; gtlsclient --help 2>&1 | grep -E -- "--download|--exit-on|-q, |--cc=|--max-data|--max-stream-data|--no-quic-dump|--timeout|--handshake" | head -12
echo "== uftp unicast"; man uftp 2>/dev/null | grep -iE -A3 "unicast" | head -20
echo "== srt uri"; srt-file-transmit -h 2>&1 | grep -iE "srt://|mode=|transtype" | head -8
