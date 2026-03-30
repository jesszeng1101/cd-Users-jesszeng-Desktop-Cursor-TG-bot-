#!/bin/bash
if [ -f bot.pid ]; then
  kill $(cat bot.pid)
  rm bot.pid
  echo "Bot stopped."
else
  echo "No bot.pid found."
fi
