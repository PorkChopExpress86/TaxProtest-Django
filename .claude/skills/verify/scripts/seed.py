# Verification seed data, run inside `manage.py shell` by compose.verify.yml.
# Reuses the similarity golden-test neighbourhoods so the instance has a Harris
# and a Brazos subject with ranked comparables. Seeds only an empty database.
from counties.common.tests.similarity_scenarios import (
    build_brazos_residential_scenario,
    build_harris_residential_scenario,
)
from counties.harris.models import PropertyRecord

if PropertyRecord.objects.exists():
    print("verify-seed: database already seeded")
else:
    harris = build_harris_residential_scenario()
    brazos = build_brazos_residential_scenario()
    print(f"verify-seed: harris subject {harris}, brazos subject {brazos}")
