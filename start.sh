#!/bin/bash
# KERNOS Discord Bot Launcher
# Double-click this file (or run from terminal: ./start.sh)

cd "$(dirname "$0")"

# Activate the virtual environment
source .venv/bin/activate

# Start the bot
echo "Starting Kernos..."
echo "Press Ctrl+C to stop."
echo ""
python kernos/server.py
