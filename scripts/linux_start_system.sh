#!/bin/bash
# Script for launching the Email-Telegram bot system via an interactive shell

echo "Launching the Email-Telegram bot management system..."

# Change to the project root directory
cd "$(dirname "$0")/.."

# Try to activate virtual environment (check both Linux and Windows paths)
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
elif [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -f ".venv/Scripts/activate" ]; then
    source .venv/Scripts/activate
elif [ -f "venv/Scripts/activate" ]; then
    source venv/Scripts/activate
else
    echo "Error: Cannot find virtual environment activation script"
    read -p "Press Enter to exit..."
    exit 1
fi

# Set PYTHONPATH to include the project root
export PYTHONPATH="$PWD"

# Run the management tool
python -m src.cli.tools system shell
EXIT_STATUS=$?

# Try to deactivate if we're in a virtual environment
if [ -n "$VIRTUAL_ENV" ]; then
    deactivate
fi

# If the script fails, do not close immediately
if [ $EXIT_STATUS -ne 0 ]; then
    echo
    echo "An error occurred while starting. Press Enter to exit..."
    read
fi