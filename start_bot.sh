#!/bin/bash
# Launch script for the bot — fully detaches so it survives the parent shell.
# Usage: bash start_bot.sh
# Stop:  kill $(cat /tmp/bot.pid)

set -e
cd "$(dirname "$0")"
mkdir -p logs

# Stop any existing instance
if [ -f /tmp/bot.pid ] && kill -0 "$(cat /tmp/bot.pid)" 2>/dev/null; then
    echo "Stopping existing bot instance..."
    kill "$(cat /tmp/bot.pid)" 2>/dev/null || true
    sleep 2
fi

# Launch fully detached — setsid creates new session, & backgrounds, disown removes from job table
# Redirections must come BEFORE setsid so they're inherited.
setsid env BOT_TOKEN="8782707772:AAE_BGWdVRwr6luC82TIQUvDbOZ-YXbehKM" \
    python3 -u bot.py \
    > logs/bot_stdout.log 2>&1 < /dev/null &

NEW_PID=$!
echo "$NEW_PID" > /tmp/bot.pid
disown $NEW_PID 2>/dev/null || true

echo "✅ Bot launched with PID: $NEW_PID"
sleep 5
if kill -0 $NEW_PID 2>/dev/null; then
    echo "✅ Bot is running and polling Telegram"
    echo ""
    echo "📋 To follow live issues:"
    echo "   tail -f logs/live_issues.log"
    echo ""
    echo "📋 To stop the bot:"
    echo "   kill \$(cat /tmp/bot.pid)"
else
    echo "❌ Bot died. Last 20 log lines:"
    tail -20 logs/bot_stdout.log
fi
