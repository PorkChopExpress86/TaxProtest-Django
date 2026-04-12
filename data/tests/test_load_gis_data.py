import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from data.management.commands.load_gis_data import find_preferred_shapefile


class FindPreferredShapefileTests(SimpleTestCase):
    def test_prefers_nested_parcelscity_shapefile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / 'Parcels.shp').touch()
            nested = root / 'Parcels' / 'Gis' / 'pdata' / 'ParcelsCity'
            nested.mkdir(parents=True)
            preferred = nested / 'ParcelsCity.shp'
            preferred.touch()

            self.assertEqual(find_preferred_shapefile(str(root)), str(preferred))

    def test_falls_back_to_available_shapefile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            only_file = root / 'Parcels.shp'
            only_file.touch()

            self.assertEqual(find_preferred_shapefile(str(root)), str(only_file))

    def test_returns_none_when_no_shapefile_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(find_preferred_shapefile(tmpdir))