#!/bin/sh
set -eu

export DISPLAY="${DISPLAY:-:99}"
Xvfb "$DISPLAY" -screen 0 1280x720x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
sleep 1
exec node index.js
