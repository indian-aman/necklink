#!/usr/bin/env bash
echo "== srt-file-transmit"; srt-file-transmit -h 2>&1 | sed -n '1,40p' | grep -iE "file|listener|caller|-t |timeout|stats|-v" | head -15
echo "== uftp"; uftp -h 2>&1 | grep -iE " -H| -R| -C| -U| -s| -x| -Y| -P| -p | -M " | head -15
echo "== uftpd"; uftpd -h 2>&1 | grep -iE " -D| -d| -I| -p | -M| -U| -E" | head -10
echo "== gtlsserver"; gtlsserver --help 2>&1 | grep -iE "Usage|-d,|--htdocs|-q,|--quiet|cc=|--cc" | head -8
echo "== gtlsclient"; gtlsclient --help 2>&1 | grep -iE "--download|--exit-on|-q,|--cc|--max-data|--max-stream" | head -10
