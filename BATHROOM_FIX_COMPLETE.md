# Bathroom Display Fix - Completed

**Date:** October 17, 2025  
**Issue:** Bathrooms should display with one decimal place, and half bathrooms should be added to the total

## Problem

- Property at 16213 Wall St in 77040 has 2 full baths + 1 half bath
- Was displaying as "2.00" instead of "2.5"
- The `load_fixtures_room_counts()` function was storing full baths and half baths separately without calculating the total

## Solution

### 1. Fixed ETL Function (`data/etl.py`)

Updated `load_fixtures_room_counts()` to calculate total bathrooms:

```python
# Calculate total bathrooms: full baths + (0.5 * half baths)
full_baths = data['bathrooms'] if data['bathrooms'] is not None else Decimal('0')
half_baths_count = data['half_baths'] if data['half_baths'] is not None else 0

if data['bathrooms'] is not None or data['half_baths'] is not None:
    total_bathrooms = full_baths + (Decimal('0.5') * Decimal(half_baths_count))
    update_fields['bathrooms'] = total_bathrooms
```

**Result:** The `bathrooms` field now stores the total (e.g., 2.5 for 2 full + 1 half)

### 2. Updated Template Display (`templates/index.html`)

Added `floatformat:1` to always show one decimal place:

```html
<span class="text-gray-700">{{ r.bathrooms|floatformat:1|default:"-" }}</span>
```

**Result:** Bathrooms display as "2.0", "2.5", "3.0", etc.

### 3. Updated CSV Export (`taxprotest/views.py`)

Formatted bathroom value for CSV export:

```python
bathrooms = f'{float(building.bathrooms):.1f}' if building and building.bathrooms else ''
```

**Result:** CSV exports show bathrooms with one decimal place

## Verification

✅ **16213 Wall St, 77040:**
- Bedrooms: 4
- Full Baths: 2
- Half Baths: 1
- **Total Bathrooms: 2.5** ✓

✅ **Sample Wall Street Properties:**
```
16433 WALL: 4 bed / 2.0 bath
16429 WALL: 3 bed / 2.0 bath
16425 WALL: 3 bed / 3.0 bath
16421 WALL: 3 bed / 2.0 bath
16213 WALL: 4 bed / 2.5 bath  ← Correctly shows half bath
```

## Data Import Statistics

**Fixtures Import Results:**
- Total fixture records processed: 8,040,240
- Room records found (RMB/RMF/RMH): 3,146,378
- Buildings with room data: 1,303,303
- BuildingDetail records updated: 1,300,789
- Buildings not found: 2,514

## Testing

To verify the fix:

1. **Web Interface:**
   ```
   http://localhost:8000
   Search: Street=Wall, Zip=77040
   ```
   
2. **Database Query:**
   ```bash
   docker compose exec web python manage.py shell -c "
   from data.models import PropertyRecord, BuildingDetail
   prop = PropertyRecord.objects.filter(street_number='16213', street_name='WALL', zipcode='77040').first()
   bld = BuildingDetail.objects.filter(account_number=prop.account_number, is_active=True).first()
   print(f'Bedrooms: {bld.bedrooms}, Bathrooms: {bld.bathrooms}, Half Baths: {bld.half_baths}')
   "
   ```

3. **CSV Export:**
   - Search for Wall/77040
   - Click "Export to CSV"
   - Verify bathroom values have one decimal place

## Files Modified

1. `data/etl.py` - Fixed `load_fixtures_room_counts()` function
2. `templates/index.html` - Added `floatformat:1` filter
3. `taxprotest/views.py` - Formatted bathrooms in CSV export

---

**Status:** ✅ Complete and verified  
**Database:** 1,300,789 buildings updated with correct bathroom totals
