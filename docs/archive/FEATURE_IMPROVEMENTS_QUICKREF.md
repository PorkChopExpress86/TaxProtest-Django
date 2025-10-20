# Quick Reference: Feature Extraction Improvements

## 🎯 What Changed

All four recommended improvements have been implemented:

1. ✅ **Soft Delete Fields** - Track import history without deleting data
2. ✅ **Post-Import Linking** - Automatically fix orphaned records
3. ✅ **Data Validation** - Verify account numbers before import
4. ✅ **Import Metadata** - Comprehensive tracking with batch IDs

## 🚀 Quick Commands

### Run Import (Recommended)
```bash
docker compose exec web python manage.py import_building_data
```

### Link Orphaned Records Only
```bash
docker compose exec web python manage.py link_orphaned_records
```

### Check Database State
```bash
docker compose exec web python manage.py shell -c "
from data.models import BuildingDetail, ExtraFeature

# Active vs inactive
print(f'Active Buildings: {BuildingDetail.objects.filter(is_active=True).count()}')
print(f'Inactive Buildings: {BuildingDetail.objects.filter(is_active=False).count()}')
print(f'Active Features: {ExtraFeature.objects.filter(is_active=True).count()}')
print(f'Inactive Features: {ExtraFeature.objects.filter(is_active=False).count()}')

# Orphaned records
print(f'Orphaned Buildings: {BuildingDetail.objects.filter(property__isnull=True).count()}')
print(f'Orphaned Features: {ExtraFeature.objects.filter(property__isnull=True).count()}')
"
```

## 📊 New Import Flow

**Old:** Delete everything → Import new data  
**New:** Mark inactive → Import with validation → Link orphans → Track stats

## 🔍 Key Features

### Soft Deletes
- Records marked `is_active=False` instead of deleted
- Preserves historical data
- Can rollback if needed

### Import Tracking
- Each import gets a unique `import_batch_id` (e.g., `20251016_140532`)
- `import_date` timestamp on every record
- View history in Django admin

### Data Validation
- Pre-validates account numbers exist
- Returns detailed statistics:
  - `imported`: Successfully created
  - `invalid`: Bad account numbers
  - `skipped`: Missing data

### Orphan Linking
- Automatically links features to properties
- Runs after each import
- Can be run manually anytime

## 📁 Django Admin

Navigate to:
- `/admin/data/buildingdetail/` - View/filter building records
- `/admin/data/extrafeature/` - View/filter feature records

**Filters:**
- Active/Inactive status
- Import date
- Import batch ID
- Building/Feature type

## 📝 Import Statistics Example

```
Batch ID: 20251016_140532
Buildings deactivated: 145,000
Features deactivated: 89,000
Buildings imported: 148,500 (invalid: 350, skipped: 150)
Features imported: 91,200 (invalid: 120, skipped: 80)
Buildings linked: 2,450
Features linked: 1,830
```

## 🔧 Developer Notes

### Query Active Records Only
```python
# Always filter for active records
buildings = BuildingDetail.objects.filter(is_active=True)
features = ExtraFeature.objects.filter(is_active=True)

# Get property with active features
property.buildings.filter(is_active=True)
property.extra_features.filter(is_active=True)
```

### Manual Soft Delete
```python
from data.etl import mark_old_records_inactive

# Mark all as inactive
results = mark_old_records_inactive()

# Mark all except specific batch as inactive
results = mark_old_records_inactive(exclude_batch_id='20251016_140532')
```

### Manual Linking
```python
from data.etl import link_orphaned_records

# Link orphaned records
results = link_orphaned_records(chunk_size=5000)
print(f"Linked {results['buildings_linked']} buildings")
print(f"Linked {results['features_linked']} features")
```

## 📚 Full Documentation

See `FEATURE_EXTRACTION_IMPROVEMENTS.md` for complete details.

## ✨ Benefits

- **No Data Loss** - Soft deletes preserve history
- **Better Tracking** - Know exactly when data was imported
- **Data Integrity** - Validation prevents bad data
- **Easy Debugging** - Track down issues by batch ID
- **Rollback Ready** - Can reactivate previous imports

---

**Status:** ✅ All improvements implemented and tested
