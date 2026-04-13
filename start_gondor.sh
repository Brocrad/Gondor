#!/bin/bash

# Gondor Bot Auto-Restart Manager (Linux/Mac)
echo "================================"
echo "   Gondor Bot Auto-Restart"
echo "================================"
echo ""
echo "Starting bot with automatic restart capability..."
echo "Press Ctrl+C to stop the bot completely"
echo ""

# Change to script directory
cd "$(dirname "$0")"

# Start the bot manager
python3 restart_bot.py

echo ""
echo "Bot manager has stopped."
