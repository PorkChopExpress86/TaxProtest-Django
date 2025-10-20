#!/bin/bash
# Run GIS import monitor in background with logging
# Usage: ./run_gis_monitor.sh

LOG_FILE="/tmp/gis_monitor_$(date +%Y%m%d_%H%M%S).log"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Starting GIS import monitor in background..."
echo "Log file: $LOG_FILE"
echo ""
echo "To view progress:"
echo "  tail -f $LOG_FILE"
echo ""
echo "To check current status:"
echo "  tail -20 $LOG_FILE"
echo ""

# Run monitor in background
nohup "$SCRIPT_DIR/monitor_gis_import.sh" > "$LOG_FILE" 2>&1 &
MONITOR_PID=$!

echo "Monitor running with PID: $MONITOR_PID"
echo "Log: $LOG_FILE"
echo ""
echo "To stop the monitor:"
echo "  kill $MONITOR_PID"
echo ""

# Show initial output
sleep 2
tail -20 "$LOG_FILE"
