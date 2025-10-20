# Building Details & Extra Features Import - Summary

## ✅ Completed

### 1. Database Models Created
- **BuildingDetail** model with fields:
  - Building identification (type, style, class)
  - Quality and condition codes
  - Age (year built, remodeled, effective year)
  - Areas (living area, base area, gross area)
  - Stories, foundation, exterior, roof details
  - Room counts (bedrooms, bathrooms, fireplaces)
  - Indexed on account_number for fast lookups

- **ExtraFeature** model with fields:
  - Feature code and description (pool, garage, spa, etc.)
  - Quantity, area, dimensions
  - Quality and condition
  - Year built and value
  - Indexed on account_number and feature_code

### 2. ETL Functions
- `load_building_details()` - Imports from building_res.txt
- `load_extra_features()` - Imports from extra_features.txt
- Both use batch processing (5000 records/batch)
- Automatic field mapping with fallbacks
- Handles missing/malformed data gracefully

### 3. Management Command
- `python manage.py load_building_features`
- Downloads Real_building_land.zip if needed
- Loads both buildings and features
- Options to skip either import
- Progress tracking every 5000 records

### 4. Documentation
- `BUILDING_FEATURES_SETUP.md` - Complete import guide
- Includes verification steps, troubleshooting, feature codes reference

## 🚀 Current Status

**Import is running** for:
1. Building details from `building_res.txt` (~1-2M records)
2. Extra features from `extra_features.txt` (~3-5M records)

Expected completion time: 20-45 minutes total

## 📊 What This Enables

With building details and extra features loaded, we can now:

### 1. Rich Property Data
Every property now has associated:
- Detailed building characteristics
- List of amenities and features
- Structural information
- Room counts and sizes

### 2. Feature-Based Search
Query properties by:
```python
# Properties with pools
PropertyRecord.objects.filter(
    extra_features__feature_code__icontains='POOL'
)

# 3-4 bedroom homes
PropertyRecord.objects.filter(
    buildings__bedrooms__gte=3,
    buildings__bedrooms__lte=4
)

# Homes built after 2000
PropertyRecord.objects.filter(
    buildings__year_built__gte=2000
)
```

### 3. Similarity Matching (Next Step)
Can now match properties based on:
- **Size**: Living area within ±20%
- **Age**: Year built within ±5 years
- **Features**: Has pool? Has garage? Number of baths?
- **Location**: Within X miles (using lat/long)
- **Quality**: Similar quality/condition codes

## 📋 Next Steps: Implement "Find Similar" Feature

### Phase 1: Create Similarity Query Function
```python
def find_similar_properties(property_id, max_distance_miles=5):
    """Find properties similar to the given property."""
    prop = PropertyRecord.objects.get(id=property_id)
    building = prop.buildings.first()
    features = prop.extra_features.all()
    
    # Start with properties in the area
    nearby = PropertyRecord.objects.filter(
        latitude__isnull=False,
        longitude__isnull=False
    )
    
    # Filter by size (±20%)
    if building and building.heat_area:
        min_area = building.heat_area * 0.8
        max_area = building.heat_area * 1.2
        nearby = nearby.filter(
            buildings__heat_area__gte=min_area,
            buildings__heat_area__lte=max_area
        )
    
    # Filter by age (±5 years)
    if building and building.year_built:
        min_year = building.year_built - 5
        max_year = building.year_built + 5
        nearby = nearby.filter(
            buildings__year_built__gte=min_year,
            buildings__year_built__lte=max_year
        )
    
    # Match features (has pool, garage, etc.)
    feature_codes = [f.feature_code for f in features]
    for code in feature_codes:
        nearby = nearby.filter(
            extra_features__feature_code=code
        )
    
    # Calculate distance and sort
    # (Use Haversine formula or PostGIS)
    
    return nearby[:20]  # Top 20 matches
```

### Phase 2: Add UI Button
- Add "Find Similar" button to each row in search results
- Create new view: `similar_properties(request, account_number)`
- Template shows matching properties with:
  - Distance from original
  - Similarity score
  - Side-by-side comparison
  - Highlighted matching features

### Phase 3: Advanced Features
- Weighted similarity scoring
- Map view showing similar properties
- Export comparisons to CSV
- Save/bookmark similar property searches

## 🔍 Verification After Import

Once import completes, verify with:

```bash
docker compose exec web python manage.py shell
```

```python
from data.models import BuildingDetail, ExtraFeature, PropertyRecord

# Counts
print(f"Buildings: {BuildingDetail.objects.count()}")
print(f"Features: {ExtraFeature.objects.count()}")

# Properties with pools
pools = PropertyRecord.objects.filter(
    extra_features__feature_code__icontains='POOL'
).distinct().count()
print(f"Properties with pools: {pools}")

# Average living area
from django.db.models import Avg
avg_area = BuildingDetail.objects.aggregate(
    Avg('heat_area')
)['heat_area__avg']
print(f"Average living area: {avg_area:.0f} sqft")

# Sample property with all data
sample = PropertyRecord.objects.filter(
    buildings__isnull=False,
    extra_features__isnull=False,
    latitude__isnull=False
).first()

if sample:
    print(f"\nSample: {sample.address}")
    print(f"  Lat/Long: ({sample.latitude}, {sample.longitude})")
    b = sample.buildings.first()
    if b:
        print(f"  Built: {b.year_built}")
        print(f"  Living Area: {b.heat_area} sqft")
        print(f"  Bedrooms: {b.bedrooms}")
        print(f"  Bathrooms: {b.bathrooms}")
    print(f"  Features:")
    for f in sample.extra_features.all()[:5]:
        print(f"    - {f.feature_description}")
```

## 📚 Additional Resources

- **Feature Codes**: See `downloads/Code_description_real/desc_r_10_extra_features.txt`
- **Building Types**: See `downloads/Code_description_real/desc_r_02_building_type_code.txt`
- **Building Styles**: See `downloads/Code_description_real/desc_r_03_building_style.txt`
- **Quality Codes**: See `downloads/Code_description_real/desc_r_07_quality_code.txt`

## 🎯 Success Metrics

After successful import, you should have:
- ✅ ~1-2 million building records
- ✅ ~3-5 million extra feature records
- ✅ Properties linked to buildings via foreign keys
- ✅ Searchable by any building/feature attribute
- ✅ Ready for similarity matching algorithm

The foundation is now complete for implementing sophisticated property comparison and "Find Similar Properties" features!
