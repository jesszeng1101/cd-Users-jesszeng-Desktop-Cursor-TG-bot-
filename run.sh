#!/bin/bash
cd "$(dirname "$0")"
nohup python3 bot.py >> bot.log 2>&1 &
echo "Bot started. PID: $!"
echo $! > bot.pid
