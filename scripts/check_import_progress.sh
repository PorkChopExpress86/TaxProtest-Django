#!/bin/bash
# Script to monitor the building data import progress

echo "=== Import Progress Monitor ==="
echo ""

# Check if import is running
if pgrep -f "import_building_data" > /dev/null; then
    echo "✓ Import process is running"
else
    echo "✗ Import process is NOT running"
fi

echo ""
echo "=== Latest Log Output ==="
tail -20 /tmp/building_import.log 2>/dev/null || echo "No log file found"

echo ""
echo "=== Database Record Counts ==="
docker compose exec -T web python manage.py shell -c "
from data.models import PropertyRecord, BuildingDetail, ExtraFeature

props = PropertyRecord.objects.count()
buildings = BuildingDetail.objects.count()
buildings_active = BuildingDetail.objects.filter(is_active=True).count()
features = ExtraFeature.objects.count()
features_active = ExtraFeature.objects.filter(is_active=True).count()

print(f'Properties:       {props:>10,}')
print(f'Buildings:        {buildings:>10,} (active: {buildings_active:,})')
print(f'Features:         {features:>10,} (active: {features_active:,})')
" 2>/dev/null

echo ""
echo "=== To watch live updates, run: ==="
echo "tail -f /tmp/building_import.log"
