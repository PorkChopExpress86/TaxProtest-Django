#!/bin/bash
# Monitor GIS import progress

echo "=== GIS Import Progress Monitor ==="
echo "Started at: $(date)"
echo ""

while true; do
    # Check database progress
    RESULT=$(docker compose exec -T web python manage.py shell 2>/dev/null <<EOF
from data.models import PropertyRecord
with_coords = PropertyRecord.objects.filter(latitude__isnull=False).count()
total = PropertyRecord.objects.count()
print(f'{with_coords},{total}')
EOF
)
    
    # Parse result
    WITH_COORDS=$(echo "$RESULT" | grep -o '[0-9]*,[0-9]*' | cut -d',' -f1)
    TOTAL=$(echo "$RESULT" | grep -o '[0-9]*,[0-9]*' | cut -d',' -f2)
    
    if [ -n "$WITH_COORDS" ] && [ -n "$TOTAL" ] && [ "$TOTAL" -gt 0 ]; then
        PERCENT=$(awk "BEGIN {printf \"%.2f\", ($WITH_COORDS/$TOTAL)*100}")
        echo "[$(date +%H:%M:%S)] Coordinates: $WITH_COORDS / $TOTAL ($PERCENT%)"
        
        # Check if complete
        if [ "$PERCENT" = "100.00" ] || [ "$WITH_COORDS" -eq "$TOTAL" ]; then
            echo ""
            echo "✓ GIS Import Complete!"
            break
        fi
    else
        echo "[$(date +%H:%M:%S)] Waiting for import to start..."
    fi
    
    # Check if process is still running
    PROCS=$(ps aux | grep "load_gis_data" | grep -v grep | wc -l)
    if [ "$PROCS" -eq 0 ]; then
        echo ""
        echo "✗ No GIS import processes found. Import may have failed."
        echo "Check logs: tail /tmp/gis_final.log"
        break
    fi
    
    sleep 60  # Check every minute
done

echo ""
echo "Final statistics:"
docker compose exec -T web python manage.py shell 2>/dev/null <<EOF
from data.models import PropertyRecord
total = PropertyRecord.objects.count()
with_coords = PropertyRecord.objects.filter(latitude__isnull=False).count()
print(f'Total properties: {total:,}')
print(f'With coordinates: {with_coords:,}')
print(f'Coverage: {with_coords/total*100:.1f}%')
EOF
