#!/bin/bash
cd "$(dirname "$0")"
while true; do
    python3 -u bot.py
    echo "[$(date)] Bot crashed, restarting in 5s..." >> /tmp/bot_restart.log
    sleep 5
done
