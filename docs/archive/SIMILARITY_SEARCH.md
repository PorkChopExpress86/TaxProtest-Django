# Property Similarity Search Feature

## Overview
The similarity search feature allows users to find comparable properties based on location, size, age, and features. This is useful for property valuation, market analysis, and finding comparable sales.

## Implementation Details

### Components Created

1. **data/similarity.py** - Core similarity algorithm
   - `haversine_distance()`: Calculates great circle distance in miles between two lat/long coordinates
   - `calculate_similarity_score()`: Computes weighted similarity score (0-100 scale)
   - `find_similar_properties()`: Main search function with filtering and ranking
   - `format_feature_list()`: Converts feature codes to readable names

2. **Similarity Scoring Algorithm**
   The algorithm uses weighted scoring across multiple dimensions:
   - **Distance Score (30 points)**: Based on proximity (within 5 miles)
   - **Size Score (25 points)**: Building area similarity (within 30% difference)
   - **Age Score (15 points)**: Year built similarity (within 10 years)
   - **Feature Score (20 points)**: Matching extra features (pools, garages, etc.)
   - **Room Score (10 points)**: Bedroom and bathroom counts

3. **Views and URLs**
   - `taxprotest/views.py`: Added `similar_properties()` view
   - `taxprotest/urls.py`: Added route `/similar/<account_number>/`
   - Template: `templates/similar_properties.html`

4. **UI Integration**
   - Added "Find Similar" button to each row in search results table
   - Button links to `/similar/<account_number>/`
   - Similar properties page shows target property details and ranked results

### Search Parameters

Default parameters (can be customized via query string):
- `max_distance`: 5 miles (search radius)
- `max_results`: 25 properties
- `min_score`: 30 (minimum similarity score 0-100)

Example URL with custom parameters:
```
/similar/0123456789012345/?max_distance=10&max_results=50&min_score=20
```

### Database Requirements

The similarity search requires the following data to be imported:

1. **GIS Data** (Parcels.zip)
   - Latitude and longitude for distance calculations
   - Import command: `docker compose run --rm web python manage.py load_gis_data`

2. **Building Details** (building_res.txt)
   - Living area, year built, bedrooms, bathrooms
   - Import command: `docker compose run --rm web python manage.py load_building_features`
   - Status: Partially complete (130K+ records imported, needs completion)

3. **Extra Features** (extra_features.txt)
   - Pools, garages, patios, etc.
   - Import command: Same as building details (part of load_building_features)
   - Status: Not yet started

### Algorithm Flow

1. **Input Validation**
   - Verify property exists and has GIS coordinates
   - Return error if location data missing

2. **Bounding Box Filter**
   - Calculate lat/long bounding box based on max_distance
   - Filter candidate properties within the box (efficient pre-filter)

3. **Distance Calculation**
   - Use haversine formula for precise distance
   - Filter results to properties within max_distance

4. **Similarity Scoring**
   - Calculate weighted score across all dimensions
   - Properties without building data still get distance/age scores

5. **Ranking and Results**
   - Sort by similarity score (highest first)
   - Return top N results with formatted data

### Feature Details

#### Distance Scoring
- Uses haversine formula for accurate spherical distance
- Maximum score at 0 miles, decreases linearly to 0 at max_distance
- Formula: `max(0, 30 * (1 - distance / max_distance))`

#### Size Scoring  
- Compares living area (heat_area from building_res.txt)
- Perfect match = 25 points
- Score decreases as percentage difference increases
- Uses formula: `max(0, 25 * (1 - abs(size_diff) / max(target, candidate)))`

#### Age Scoring
- Compares year_built
- Perfect match (same year) = 15 points
- Score decreases as year difference increases
- Maximum difference tracked: 50 years

#### Feature Scoring
- Compares extra features (pools, garages, etc.)
- Calculates Jaccard similarity: `intersection / union`
- Maximum 20 points for identical feature sets

#### Room Scoring
- Compares bedroom and bathroom counts
- 5 points each for exact matches
- Partial credit for being within 1 room

### UI Components

#### Search Results Table
- Added "Actions" column with "Find Similar" button
- Button styled with indigo color scheme
- Icon + text for clear action

#### Similar Properties Page
- **Header Section**: Shows target property details and specs
- **Search Parameters**: Displays current search settings with "Expand Search" link
- **Results Table**: Shows ranked similar properties with:
  - Similarity score (color-coded: green ≥70%, yellow ≥50%, blue <50%)
  - Distance in miles
  - Address and owner
  - Building specs (area, year, bed/bath)
  - Assessed value
  - Features list (truncated)
- **No Results**: Helpful message with link to expand search

### Testing the Feature

1. **Start the application**:
   ```bash
   docker compose up
   ```

2. **Search for a property**:
   - Go to http://localhost:8000/
   - Enter search criteria (e.g., last name "Smith")
   - Click "Search Properties"

3. **Find similar properties**:
   - In the results table, click the "Similar" button for any property
   - View the similar properties ranked by score

4. **Adjust search parameters**:
   - Click "Expand Search" to widen the radius
   - Or manually adjust URL parameters:
     - `?max_distance=10` - Search within 10 miles
     - `?max_results=50` - Show up to 50 results
     - `?min_score=20` - Lower minimum score threshold

### Known Limitations

1. **Data Dependencies**:
   - Similarity search requires GIS coordinates (latitude/longitude)
   - Properties without coordinates will show error message
   - Building details and features enhance scoring but are optional

2. **Import Status**:
   - Building details import is partially complete (~130K records)
   - Extra features not yet imported
   - Complete imports before full production use

3. **Performance**:
   - Bounding box pre-filter provides good performance
   - Large search radii (>10 miles) may be slower
   - Consider adding database indexes if queries are slow

### Future Enhancements

1. **Map View**:
   - Display similar properties on an interactive map
   - Show distance and similarity visually

2. **Advanced Filters**:
   - Filter by property type (single family, townhome, etc.)
   - Filter by value range
   - Filter by specific features (must have pool, garage, etc.)

3. **Saved Searches**:
   - Allow users to save similarity search criteria
   - Email notifications for new similar listings

4. **Comparison View**:
   - Side-by-side comparison of target property vs similar properties
   - Highlight differences in specs and features

5. **Statistical Analysis**:
   - Show median/average values for similar properties
   - Provide value estimate based on comparables

## Files Modified/Created

### New Files
- `data/similarity.py` - Core similarity algorithm (300+ lines)
- `templates/similar_properties.html` - Results page template
- `SIMILARITY_SEARCH.md` - This documentation

### Modified Files
- `taxprotest/views.py` - Added `similar_properties()` view
- `taxprotest/urls.py` - Added route for similarity search
- `templates/index.html` - Added "Actions" column and "Find Similar" button

## Conclusion

The similarity search feature is now fully implemented on the backend with a complete scoring algorithm, view, and UI integration. To use it in production:

1. Complete the building details import (resume interrupted load)
2. Import extra features data
3. Test with various properties to validate scoring
4. Consider adding the future enhancements listed above

The feature provides a powerful tool for property analysis and valuation, using multiple dimensions of comparison to find truly similar properties in the area.
