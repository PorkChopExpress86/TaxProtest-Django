"""
Fixtures aggregator for bedroom and bathroom counts.

This module processes the HCAD fixtures.txt file to extract bedroom and bathroom
counts that are stored as separate fixture records.
"""
import logging
from pathlib import Path
from typing import Dict, Tuple
from collections import defaultdict


logger = logging.getLogger(__name__)


class FixturesAggregator:
    """Aggregates fixture data for buildings."""
    
    # Fixture type codes for bedrooms and bathrooms
    BEDROOM_CODE = 'RMB'  # Room: Bedroom
    FULL_BATH_CODE = 'RMF'  # Room: Full Bath
    HALF_BATH_CODE = 'RMH'  # Room: Half Bath
    
    def __init__(self):
        """Initialize the fixtures aggregator."""
        self.fixtures_cache: Dict[Tuple[str, int], Dict[str, float]] = {}
    
    def load_fixtures_file(self, file_path: Path) -> None:
        """
        Load and aggregate fixtures from fixtures.txt file.
        
        Args:
            file_path: Path to the fixtures.txt file
            
        The fixtures file has format:
        acct    bld_num    type    type_dscr    units
        1234567890123    1    RMB    Room: Bedroom    4.00
        1234567890123    1    RMF    Room: Full Bath    2.00
        1234567890123    1    RMH    Room: Half Bath    1.00
        """
        logger.info(f"Loading fixtures from {file_path}")
        
        # Use defaultdict to accumulate fixture counts
        fixtures_data = defaultdict(lambda: {
            'bedrooms': 0.0,
            'full_baths': 0.0,
            'half_baths': 0.0
        })
        
        line_count = 0
        processed_count = 0
        
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                # Skip header
                next(f)
                
                for line in f:
                    line_count += 1
                    
                    # Progress logging every 100k lines
                    if line_count % 100000 == 0:
                        logger.info(f"Processed {line_count:,} fixture lines...")
                    
                    try:
                        parts = line.strip().split('\t')
                        if len(parts) < 5:
                            continue
                        
                        account_num = parts[0].strip()
                        building_num = int(parts[1].strip())
                        fixture_type = parts[2].strip()
                        units = float(parts[4].strip())
                        
                        # Only process bedroom/bathroom fixture types
                        if fixture_type not in (self.BEDROOM_CODE, self.FULL_BATH_CODE, self.HALF_BATH_CODE):
                            continue
                        
                        key = (account_num, building_num)
                        
                        if fixture_type == self.BEDROOM_CODE:
                            fixtures_data[key]['bedrooms'] = units
                        elif fixture_type == self.FULL_BATH_CODE:
                            fixtures_data[key]['full_baths'] = units
                        elif fixture_type == self.HALF_BATH_CODE:
                            fixtures_data[key]['half_baths'] = units
                        
                        processed_count += 1
                        
                    except (ValueError, IndexError) as e:
                        logger.debug(f"Error parsing fixture line {line_count}: {e}")
                        continue
        
        except FileNotFoundError:
            logger.warning(f"Fixtures file not found: {file_path}")
            return
        except Exception as e:
            logger.error(f"Error reading fixtures file: {e}")
            return
        
        # Convert defaultdict to regular dict and cache
        self.fixtures_cache = dict(fixtures_data)
        
        logger.info(
            f"Loaded fixtures for {len(self.fixtures_cache):,} buildings "
            f"from {line_count:,} lines ({processed_count:,} bedroom/bathroom records)"
        )
    
    def get_fixtures(self, account_number: str, building_number: int) -> Dict[str, float]:
        """
        Get aggregated fixtures for a specific building.
        
        Args:
            account_number: Property account number
            building_number: Building number
            
        Returns:
            Dictionary with bedrooms, full_baths, half_baths (0.0 if not found)
        """
        key = (account_number, building_number)
        return self.fixtures_cache.get(key, {
            'bedrooms': 0.0,
            'full_baths': 0.0,
            'half_baths': 0.0
        })
    
    def get_bedroom_count(self, account_number: str, building_number: int) -> int:
        """Get bedroom count for a building."""
        fixtures = self.get_fixtures(account_number, building_number)
        return int(fixtures['bedrooms'])
    
    def get_bathroom_count(self, account_number: str, building_number: int) -> float:
        """
        Get total bathroom count for a building.
        
        Total bathrooms = full_baths + (half_baths * 0.5)
        """
        fixtures = self.get_fixtures(account_number, building_number)
        full = fixtures['full_baths']
        half = fixtures['half_baths']
        return full + (half * 0.5)
    
    def clear_cache(self) -> None:
        """Clear the fixtures cache."""
        self.fixtures_cache.clear()
        logger.info("Fixtures cache cleared")
    
    def get_stats(self) -> Dict[str, int]:
        """Get statistics about loaded fixtures."""
        if not self.fixtures_cache:
            return {
                'total_buildings': 0,
                'with_bedrooms': 0,
                'with_bathrooms': 0,
                'with_both': 0
            }
        
        with_bedrooms = sum(1 for f in self.fixtures_cache.values() if f['bedrooms'] > 0)
        with_full_baths = sum(1 for f in self.fixtures_cache.values() if f['full_baths'] > 0)
        with_half_baths = sum(1 for f in self.fixtures_cache.values() if f['half_baths'] > 0)
        with_bathrooms = sum(
            1 for f in self.fixtures_cache.values()
            if f['full_baths'] > 0 or f['half_baths'] > 0
        )
        with_both = sum(
            1 for f in self.fixtures_cache.values()
            if f['bedrooms'] > 0 and (f['full_baths'] > 0 or f['half_baths'] > 0)
        )
        
        return {
            'total_buildings': len(self.fixtures_cache),
            'with_bedrooms': with_bedrooms,
            'with_full_baths': with_full_baths,
            'with_half_baths': with_half_baths,
            'with_bathrooms': with_bathrooms,
            'with_both': with_both
        }
