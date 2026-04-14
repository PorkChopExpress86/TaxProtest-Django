# Protest Analysis Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated `/protest/<account_number>/` page that shows a print-optimized equity protest analysis (comparable properties with assessed $/sqft comparison and estimated savings) plus a CSV export endpoint.

**Architecture:** Two new views (`protest_analysis`, `protest_analysis_export`) added to `taxprotest/views.py`, registered in `taxprotest/urls.py`, rendered by a new Tailwind-based template. The views reuse the existing `find_similar_properties()` function unchanged and add per-comp $/sqft delta and equity summary calculations on top. The existing `similar_properties.html` gets a link button to the new page.

**Tech Stack:** Django 5.x, Python `statistics.median` (stdlib), Tailwind CSS (CDN, already in `base.html`), `django.contrib.humanize` (`intcomma`), `unittest.mock.patch` for tests.

---

## File Map

| File | Action | What changes |
|---|---|---|
| `taxprotest/tests/test_views.py` | Modify | Add `ProtestAnalysisViewTests` and `ProtestAnalysisExportTests` classes |
| `taxprotest/views.py` | Modify | Add `protest_analysis` and `protest_analysis_export` views; add `from statistics import median as statistics_median` import |
| `taxprotest/urls.py` | Modify | Import and register 2 new URL patterns; update import list |
| `templates/protest_analysis.html` | Create | New Tailwind template with subject card, equity banner, how-to accordion, score slider, comps table, print CSS |
| `templates/similar_properties.html` | Modify | Add "Protest Analysis" button in the target property card header area |

---

## Task 1: Write failing tests for `protest_analysis` view

**Files:**
- Modify: `taxprotest/tests/test_views.py`

- [ ] **Step 1: Append `ProtestAnalysisViewTests` class to the end of `taxprotest/tests/test_views.py`**

```python
class ProtestAnalysisViewTests(TestCase):
    def setUp(self):
        self.target = PropertyRecord.objects.create(
            address="16213 Wall St",
            city="Houston",
            zipcode="77040",
            owner_name="Target Owner",
            account_number="PROTEST_TGT",
            street_number="16213",
            street_name="Wall St",
            assessed_value=370000,
            building_area=2000,
            latitude=29.8,
            longitude=-95.5,
        )
        self.target_building = BuildingDetail.objects.create(
            property=self.target,
            account_number=self.target.account_number,
            building_number=1,
            heat_area=2000,
            bedrooms=4,
            bathrooms=2,
            quality_code="B",
            year_built=2005,
            is_active=True,
        )
        self.comp = PropertyRecord.objects.create(
            address="100 Similar Ln",
            city="Houston",
            zipcode="77040",
            owner_name="Comp Owner",
            account_number="PROTEST_CMP",
            street_number="100",
            street_name="Similar Ln",
            assessed_value=320000,
            building_area=2000,
            latitude=29.81,
            longitude=-95.5,
        )
        self.comp_building = BuildingDetail.objects.create(
            property=self.comp,
            account_number=self.comp.account_number,
            building_number=1,
            heat_area=2000,
            bedrooms=4,
            bathrooms=2,
            quality_code="B",
            year_built=2004,
            is_active=True,
        )

    def _similar_result(self, prop, building, score=75.0, distance=0.5):
        return {
            "property": prop,
            "building": building,
            "features": [],
            "distance": distance,
            "similarity_score": score,
        }

    def test_404_for_unknown_account(self):
        response = self.client.get(
            reverse("protest_analysis", args=["DOESNOTEXIST"])
        )
        self.assertEqual(response.status_code, 404)

    @patch("taxprotest.views.find_similar_properties")
    def test_200_and_required_context_keys_present(self, mock_find):
        mock_find.return_value = [self._similar_result(self.comp, self.comp_building)]
        response = self.client.get(
            reverse("protest_analysis", args=[self.target.account_number])
        )
        self.assertEqual(response.status_code, 200)
        ctx = response.context
        for key in [
            "target_property",
            "target_building",
            "subject_value_per_sqft",
            "comps",
            "median_comp_value_per_sqft",
            "equity_gap_per_sqft",
            "estimated_savings",
            "comps_below_subject",
            "qualifying_comp_count",
            "min_score",
        ]:
            self.assertIn(key, ctx, f"Missing context key: {key}")

    @patch("taxprotest.views.find_similar_properties")
    def test_equity_gap_and_savings_computed_correctly(self, mock_find):
        # target: $370,000 / 2,000 sqft = $185/sqft
        # comp:   $320,000 / 2,000 sqft = $160/sqft
        # gap: 185 - 160 = $25/sqft  |  savings: 25 * 2000 = $50,000
        mock_find.return_value = [self._similar_result(self.comp, self.comp_building)]
        response = self.client.get(
            reverse("protest_analysis", args=[self.target.account_number])
        )
        ctx = response.context
        self.assertAlmostEqual(ctx["subject_value_per_sqft"], 185.0, places=1)
        self.assertAlmostEqual(ctx["median_comp_value_per_sqft"], 160.0, places=1)
        self.assertAlmostEqual(ctx["equity_gap_per_sqft"], 25.0, places=1)
        self.assertAlmostEqual(ctx["estimated_savings"], 50000.0, places=0)

    @patch("taxprotest.views.find_similar_properties")
    def test_min_score_clamped_to_52_when_below(self, mock_find):
        mock_find.return_value = []
        response = self.client.get(
            reverse("protest_analysis", args=[self.target.account_number]),
            {"min_score": "10"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["min_score"], 52.0)
        mock_find.assert_called_with(
            account_number=self.target.account_number,
            max_distance_miles=10.0,
            max_results=50,
            min_score=52.0,
        )

    @patch("taxprotest.views.find_similar_properties")
    def test_min_score_defaults_to_70_when_not_provided(self, mock_find):
        mock_find.return_value = []
        self.client.get(
            reverse("protest_analysis", args=[self.target.account_number])
        )
        mock_find.assert_called_with(
            account_number=self.target.account_number,
            max_distance_miles=10.0,
            max_results=50,
            min_score=70.0,
        )

    @patch("taxprotest.views.find_similar_properties")
    def test_no_equity_summary_when_subject_missing_assessed_value(self, mock_find):
        self.target.assessed_value = None
        self.target.save()
        mock_find.return_value = []
        response = self.client.get(
            reverse("protest_analysis", args=[self.target.account_number])
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["subject_value_per_sqft"])
        self.assertIsNone(response.context["equity_gap_per_sqft"])
        self.assertIsNone(response.context["estimated_savings"])

    @patch("taxprotest.views.find_similar_properties")
    def test_comp_delta_is_negative_when_comp_cheaper_than_subject(self, mock_find):
        # subject: $185/sqft, comp: $160/sqft → delta = -25 (comp is cheaper)
        mock_find.return_value = [self._similar_result(self.comp, self.comp_building)]
        response = self.client.get(
            reverse("protest_analysis", args=[self.target.account_number])
        )
        comps = response.context["comps"]
        self.assertEqual(len(comps), 1)
        self.assertIn("comp_delta", comps[0])
        self.assertLess(comps[0]["comp_delta"], 0)

    @patch("taxprotest.views.find_similar_properties")
    def test_comps_below_subject_counted_correctly(self, mock_find):
        # 1 comp at $160/sqft < subject $185/sqft → count = 1
        mock_find.return_value = [self._similar_result(self.comp, self.comp_building)]
        response = self.client.get(
            reverse("protest_analysis", args=[self.target.account_number])
        )
        self.assertEqual(response.context["comps_below_subject"], 1)
```

- [ ] **Step 2: Run tests to verify they fail (view not yet implemented)**

```bash
docker compose exec web python manage.py test taxprotest.tests.test_views.ProtestAnalysisViewTests --verbosity=2
```

Expected: `AttributeError: module 'taxprotest.views' has no attribute 'protest_analysis'` or `NoReverseMatch` for `protest_analysis`. All 8 tests fail.

- [ ] **Step 3: Commit failing tests**

```bash
git add taxprotest/tests/test_views.py
git commit -m "test: add failing tests for protest_analysis view"
```

---

## Task 2: Implement `protest_analysis` view and register URL

**Files:**
- Modify: `taxprotest/views.py`
- Modify: `taxprotest/urls.py`

- [ ] **Step 1: Add `from statistics import median as statistics_median` to the imports at the top of `taxprotest/views.py`**

The existing import block at the top of `taxprotest/views.py` starts with `import csv`. Add the statistics import directly after:

```python
import csv
import statistics
```

*(Add `import statistics` on the line after `import csv`)*

- [ ] **Step 2: Append `protest_analysis` view to `taxprotest/views.py` (before the `about` view)**

Add the following function immediately before the `about` view (after the `## Removed mock results function` comment at line 483):

```python
def protest_analysis(request, account_number):
    """Protest analysis page: equity comparison for ARB hearing preparation."""
    target_property = PropertyRecord.objects.filter(account_number=account_number).first()
    if not target_property:
        from django.http import Http404
        raise Http404("Property not found")

    target_building = target_property.buildings.filter(is_active=True).first()
    target_features = list(target_property.extra_features.filter(is_active=True))

    # Parse and clamp min_score to [52.0, 100.0]; default 70.0
    try:
        min_score = float(request.GET.get("min_score", 70.0))
    except (ValueError, TypeError):
        min_score = 70.0
    min_score = max(52.0, min(100.0, min_score))

    # Compute subject $/sqft
    subject_heat_area = float(target_building.heat_area) if target_building and target_building.heat_area else None
    subject_assessed = target_property.assessed_value
    subject_value_per_sqft = None
    if subject_assessed and subject_heat_area and subject_heat_area > 0:
        try:
            subject_value_per_sqft = float(subject_assessed) / subject_heat_area
        except Exception:
            subject_value_per_sqft = None

    # Find similar properties
    similar = find_similar_properties(
        account_number=account_number,
        max_distance_miles=10.0,
        max_results=50,
        min_score=min_score,
    )

    # Build enriched comp list
    comps = []
    for result in similar:
        prop = result["property"]
        building = result["building"]
        features = result["features"]

        comp_assessed = prop.assessed_value
        comp_heat_area = float(building.heat_area) if building and building.heat_area else None

        comp_value_per_sqft = None
        comp_delta = None
        if comp_assessed and comp_heat_area and comp_heat_area > 0:
            try:
                comp_value_per_sqft = float(comp_assessed) / comp_heat_area
                if subject_value_per_sqft is not None:
                    comp_delta = comp_value_per_sqft - subject_value_per_sqft
            except Exception:
                pass

        comps.append({
            "account_number": prop.account_number,
            "address": prop.street_number,
            "street_name": prop.street_name,
            "zip_code": prop.zipcode,
            "assessed_value": comp_assessed,
            "heat_area": comp_heat_area,
            "comp_value_per_sqft": comp_value_per_sqft,
            "comp_delta": comp_delta,
            "distance": result["distance"],
            "similarity_score": result["similarity_score"],
            "match_label": get_similarity_label(result["similarity_score"]),
            "year_built": building.year_built if building else None,
            "bedrooms": building.bedrooms if building else None,
            "bathrooms": building.bathrooms if building else None,
            "quality_code": building.quality_code if building else None,
            "condition_code": building.condition_code if building else None,
            "features": format_feature_list(features, max_features=5),
        })

    # Compute equity summary
    median_comp_value_per_sqft = None
    equity_gap_per_sqft = None
    estimated_savings = None
    comps_below_subject = 0

    qualifying_ppsf = [c["comp_value_per_sqft"] for c in comps if c["comp_value_per_sqft"] is not None]
    if subject_value_per_sqft is not None and qualifying_ppsf:
        median_comp_value_per_sqft = statistics.median(qualifying_ppsf)
        equity_gap_per_sqft = subject_value_per_sqft - median_comp_value_per_sqft
        if subject_heat_area:
            estimated_savings = max(0.0, equity_gap_per_sqft * subject_heat_area)
        comps_below_subject = sum(1 for p in qualifying_ppsf if p < subject_value_per_sqft)

    context = {
        "target_property": target_property,
        "target_building": target_building,
        "target_features": format_feature_list(target_features),
        "subject_heat_area": subject_heat_area,
        "subject_value_per_sqft": subject_value_per_sqft,
        "comps": comps,
        "median_comp_value_per_sqft": median_comp_value_per_sqft,
        "equity_gap_per_sqft": equity_gap_per_sqft,
        "estimated_savings": estimated_savings,
        "comps_below_subject": comps_below_subject,
        "qualifying_comp_count": len(qualifying_ppsf),
        "min_score": min_score,
    }

    return render(request, "protest_analysis.html", context)
```

- [ ] **Step 3: Register the URL in `taxprotest/urls.py`**

Replace the existing import block (lines 20–27) with:

```python
from .views import (
    about,
    export_csv,
    healthz,
    index,
    protest_analysis,
    protest_analysis_export,
    readiness,
    similar_properties,
)
```

Add two new URL patterns to `urlpatterns` after the `similar_properties` line:

```python
path("protest/<str:account_number>/", protest_analysis, name="protest_analysis"),
path("protest/<str:account_number>/export/", protest_analysis_export, name="protest_analysis_export"),
```

*(Note: `protest_analysis_export` will be implemented in Task 4. Adding it here will cause an import error until Task 4 is complete — add a stub `def protest_analysis_export(request, account_number): pass` to `views.py` temporarily if needed, OR implement both views before updating `urls.py`.)*

**Simpler approach**: Add both views in Step 2 before touching `urls.py`. Move to Task 4 Step 2 now, then come back to register both URLs together.

- [ ] **Step 4: Run the Task 1 tests to verify they now pass**

```bash
docker compose exec web python manage.py test taxprotest.tests.test_views.ProtestAnalysisViewTests --verbosity=2
```

Expected: All 8 tests **PASS**.

- [ ] **Step 5: Commit**

```bash
git add taxprotest/views.py taxprotest/urls.py
git commit -m "feat: add protest_analysis view and URL"
```

---

## Task 3: Write failing tests for `protest_analysis_export` view

**Files:**
- Modify: `taxprotest/tests/test_views.py`

- [ ] **Step 1: Append `ProtestAnalysisExportTests` class to `taxprotest/tests/test_views.py`**

```python
class ProtestAnalysisExportTests(TestCase):
    def setUp(self):
        self.target = PropertyRecord.objects.create(
            address="200 Export Ave",
            city="Houston",
            zipcode="77040",
            owner_name="Export Owner",
            account_number="EXPORT_TGT",
            street_number="200",
            street_name="Export Ave",
            assessed_value=350000,
            building_area=2000,
            latitude=29.8,
            longitude=-95.5,
        )
        self.target_building = BuildingDetail.objects.create(
            property=self.target,
            account_number=self.target.account_number,
            building_number=1,
            heat_area=2000,
            bedrooms=3,
            bathrooms=2,
            quality_code="C",
            year_built=2000,
            is_active=True,
        )
        self.comp = PropertyRecord.objects.create(
            address="201 Export Ave",
            city="Houston",
            zipcode="77040",
            owner_name="Comp Owner",
            account_number="EXPORT_CMP",
            street_number="201",
            street_name="Export Ave",
            assessed_value=300000,
            building_area=2000,
            latitude=29.81,
            longitude=-95.5,
        )
        self.comp_building = BuildingDetail.objects.create(
            property=self.comp,
            account_number=self.comp.account_number,
            building_number=1,
            heat_area=2000,
            bedrooms=3,
            bathrooms=2,
            quality_code="C",
            year_built=1999,
            is_active=True,
        )

    def test_404_for_unknown_account(self):
        response = self.client.get(
            reverse("protest_analysis_export", args=["DOESNOTEXIST"])
        )
        self.assertEqual(response.status_code, 404)

    @patch("taxprotest.views.find_similar_properties")
    def test_returns_csv_content_type(self, mock_find):
        mock_find.return_value = []
        response = self.client.get(
            reverse("protest_analysis_export", args=[self.target.account_number])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")

    @patch("taxprotest.views.find_similar_properties")
    def test_csv_filename_contains_account_number(self, mock_find):
        mock_find.return_value = []
        response = self.client.get(
            reverse("protest_analysis_export", args=[self.target.account_number])
        )
        self.assertIn(self.target.account_number, response["Content-Disposition"])

    @patch("taxprotest.views.find_similar_properties")
    def test_csv_header_row_has_required_columns(self, mock_find):
        mock_find.return_value = []
        response = self.client.get(
            reverse("protest_analysis_export", args=[self.target.account_number])
        )
        content = response.content.decode()
        header = content.splitlines()[0]
        for col in [
            "address", "similarity_score", "similarity_label",
            "living_area_sqft", "bedrooms", "bathrooms", "year_built",
            "quality_code", "condition_code", "assessed_value",
            "value_per_sqft", "delta_vs_subject_per_sqft",
        ]:
            self.assertIn(col, header, f"Missing CSV column: {col}")

    @patch("taxprotest.views.find_similar_properties")
    def test_csv_data_row_contains_comp_values(self, mock_find):
        mock_find.return_value = [
            {
                "property": self.comp,
                "building": self.comp_building,
                "features": [],
                "distance": 0.5,
                "similarity_score": 76.0,
            }
        ]
        response = self.client.get(
            reverse("protest_analysis_export", args=[self.target.account_number])
        )
        content = response.content.decode()
        lines = content.splitlines()
        self.assertEqual(len(lines), 2)  # header + 1 data row
        # data row should contain the comp's address fragment
        self.assertIn("201", lines[1])
        # delta should be negative (comp cheaper): (300000/2000) - (350000/2000) = -25
        self.assertIn("-25.00", lines[1])
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec web python manage.py test taxprotest.tests.test_views.ProtestAnalysisExportTests --verbosity=2
```

Expected: All 5 tests fail with `NoReverseMatch` for `protest_analysis_export`.

- [ ] **Step 3: Commit failing tests**

```bash
git add taxprotest/tests/test_views.py
git commit -m "test: add failing tests for protest_analysis_export view"
```

---

## Task 4: Implement `protest_analysis_export` view

**Files:**
- Modify: `taxprotest/views.py`

- [ ] **Step 1: Append `protest_analysis_export` to `taxprotest/views.py` immediately after `protest_analysis`**

```python
def protest_analysis_export(request, account_number):
    """CSV export of protest analysis comparable properties."""
    target_property = PropertyRecord.objects.filter(account_number=account_number).first()
    if not target_property:
        from django.http import Http404
        raise Http404("Property not found")

    target_building = target_property.buildings.filter(is_active=True).first()

    try:
        min_score = float(request.GET.get("min_score", 70.0))
    except (ValueError, TypeError):
        min_score = 70.0
    min_score = max(52.0, min(100.0, min_score))

    subject_heat_area = float(target_building.heat_area) if target_building and target_building.heat_area else None
    subject_assessed = target_property.assessed_value
    subject_value_per_sqft = None
    if subject_assessed and subject_heat_area and subject_heat_area > 0:
        try:
            subject_value_per_sqft = float(subject_assessed) / subject_heat_area
        except Exception:
            pass

    similar = find_similar_properties(
        account_number=account_number,
        max_distance_miles=10.0,
        max_results=50,
        min_score=min_score,
    )

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="protest_analysis_{account_number}.csv"'
    )

    writer = csv.writer(response)
    writer.writerow([
        "address",
        "similarity_score",
        "similarity_label",
        "living_area_sqft",
        "bedrooms",
        "bathrooms",
        "year_built",
        "quality_code",
        "condition_code",
        "assessed_value",
        "value_per_sqft",
        "delta_vs_subject_per_sqft",
    ])

    for result in similar:
        prop = result["property"]
        building = result["building"]

        comp_assessed = prop.assessed_value
        comp_heat_area = float(building.heat_area) if building and building.heat_area else None

        comp_value_per_sqft = None
        comp_delta = None
        if comp_assessed and comp_heat_area and comp_heat_area > 0:
            try:
                comp_value_per_sqft = float(comp_assessed) / comp_heat_area
                if subject_value_per_sqft is not None:
                    comp_delta = comp_value_per_sqft - subject_value_per_sqft
            except Exception:
                pass

        full_address = f"{prop.street_number} {prop.street_name}".strip()

        writer.writerow([
            full_address,
            f"{result['similarity_score']:.1f}",
            get_similarity_label(result["similarity_score"]),
            f"{comp_heat_area:.0f}" if comp_heat_area else "",
            building.bedrooms if building else "",
            f"{float(building.bathrooms):.1f}" if building and building.bathrooms else "",
            building.year_built if building else "",
            building.quality_code if building else "",
            building.condition_code if building else "",
            f"{float(comp_assessed):.2f}" if comp_assessed else "",
            f"{comp_value_per_sqft:.2f}" if comp_value_per_sqft is not None else "",
            f"{comp_delta:.2f}" if comp_delta is not None else "",
        ])

    return response
```

- [ ] **Step 2: Verify `protest_analysis_export` is in the import list in `taxprotest/urls.py`**

The import block in `taxprotest/urls.py` should already include `protest_analysis_export` from Task 2 Step 3. If it does not, update it now to match:

```python
from .views import (
    about,
    export_csv,
    healthz,
    index,
    protest_analysis,
    protest_analysis_export,
    readiness,
    similar_properties,
)
```

And ensure both URL patterns are in `urlpatterns`:

```python
path("protest/<str:account_number>/", protest_analysis, name="protest_analysis"),
path("protest/<str:account_number>/export/", protest_analysis_export, name="protest_analysis_export"),
```

- [ ] **Step 3: Run all new view tests**

```bash
docker compose exec web python manage.py test taxprotest.tests.test_views.ProtestAnalysisViewTests taxprotest.tests.test_views.ProtestAnalysisExportTests --verbosity=2
```

Expected: All 13 tests **PASS**.

- [ ] **Step 4: Run the full test suite to check for regressions**

```bash
docker compose exec web python manage.py test --verbosity=1
```

Expected: All tests pass. No regressions.

- [ ] **Step 5: Commit**

```bash
git add taxprotest/views.py taxprotest/urls.py
git commit -m "feat: add protest_analysis_export view and register both protest URLs"
```

---

## Task 5: Create `templates/protest_analysis.html`

**Files:**
- Create: `templates/protest_analysis.html`

- [ ] **Step 1: Create the template file**

Create `templates/protest_analysis.html` with the following content:

```html
{% extends "base.html" %}
{% load humanize %}

{% block title %}Protest Analysis – {{ target_property.street_number }} {{ target_property.street_name }}{% endblock %}

{% block content %}
<style>
  @media print {
    nav, footer, .no-print { display: none !important; }
    .print-header { display: block !important; }
    body { font-family: Georgia, serif; font-size: 11pt; }
    .shadow-xl, .shadow-md, .shadow-sm { box-shadow: none !important; }
    a { color: inherit; text-decoration: none; }
  }
  .print-header { display: none; }
</style>

<div class="min-h-screen py-8 lg:py-12">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">

    <!-- Print-only header -->
    <div class="print-header mb-4 border-b border-gray-400 pb-2">
      <p class="text-sm font-semibold text-gray-700">
        Harris County Property Tax Protest Analysis &mdash;
        {{ target_property.street_number }} {{ target_property.street_name }},
        {{ target_property.zipcode }}
      </p>
      <p class="text-xs text-gray-500">Prepared from HCAD appraisal records &bull; For informational use only</p>
    </div>

    <!-- Back button (screen only) -->
    <div class="mb-6 no-print">
      <a href="{% url 'similar_properties' target_property.account_number %}"
         class="inline-flex items-center text-blue-600 hover:text-blue-800 text-sm">
        <svg class="w-4 h-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                d="M10 19l-7-7m0 0l7-7m-7 7h18"/>
        </svg>
        Back to Similar Properties
      </a>
    </div>

    <!-- ── Subject Property Card ── -->
    <div class="bg-white rounded-2xl shadow-xl border border-gray-100 overflow-hidden mb-6">
      <div class="bg-gradient-to-r from-blue-700 to-indigo-700 px-8 py-5">
        <h1 class="text-2xl font-bold text-white">Protest Analysis</h1>
        <p class="text-blue-200 text-sm mt-1">
          {{ target_property.street_number }} {{ target_property.street_name }},
          {{ target_property.city }}, TX {{ target_property.zipcode }}
        </p>
      </div>
      <div class="px-8 py-5 grid grid-cols-2 md:grid-cols-4 gap-4">
        <div>
          <p class="text-xs text-gray-500 uppercase font-medium">Account</p>
          <p class="text-sm font-semibold text-gray-900">{{ target_property.account_number }}</p>
        </div>
        {% if target_property.assessed_value %}
        <div>
          <p class="text-xs text-gray-500 uppercase font-medium">Assessed Value</p>
          <p class="text-sm font-semibold text-gray-900">${{ target_property.assessed_value|floatformat:0|intcomma }}</p>
        </div>
        {% endif %}
        {% if subject_value_per_sqft %}
        <div>
          <p class="text-xs text-gray-500 uppercase font-medium">Your $/sqft</p>
          <p class="text-sm font-bold text-indigo-700">${{ subject_value_per_sqft|floatformat:2 }}</p>
        </div>
        {% endif %}
        {% if target_building %}
        <div>
          <p class="text-xs text-gray-500 uppercase font-medium">Living Area</p>
          <p class="text-sm font-semibold text-gray-900">
            {{ target_building.heat_area|floatformat:0|intcomma }} sqft
          </p>
        </div>
        <div>
          <p class="text-xs text-gray-500 uppercase font-medium">Bed / Bath</p>
          <p class="text-sm font-semibold text-gray-900">
            {{ target_building.bedrooms|default:"–" }} / {{ target_building.bathrooms|default:"–" }}
          </p>
        </div>
        <div>
          <p class="text-xs text-gray-500 uppercase font-medium">Year Built</p>
          <p class="text-sm font-semibold text-gray-900">{{ target_building.year_built|default:"–" }}</p>
        </div>
        <div>
          <p class="text-xs text-gray-500 uppercase font-medium">Quality</p>
          <p class="text-sm font-semibold text-gray-900">{{ target_building.quality_code|default:"–" }}</p>
        </div>
        {% endif %}
      </div>
    </div>

    <!-- ── Equity Summary Banner ── -->
    {% if subject_value_per_sqft and median_comp_value_per_sqft %}
      {% if equity_gap_per_sqft > 0 %}
      <div class="rounded-xl p-5 mb-6 bg-amber-50 border-2 border-amber-300 shadow-md">
        <h2 class="text-base font-bold text-amber-900">Potential Equity Protest Case</h2>
        <p class="mt-1 text-sm text-amber-800">
          Your property is assessed at
          <strong>${{ subject_value_per_sqft|floatformat:2 }}/sqft</strong>
          &mdash;
          <strong>${{ equity_gap_per_sqft|floatformat:2 }}/sqft above</strong>
          the median of {{ qualifying_comp_count }} comparable propert{{ qualifying_comp_count|pluralize:"y,ies" }}
          (${{ median_comp_value_per_sqft|floatformat:2 }}/sqft).
        </p>
        {% if estimated_savings %}
        <p class="mt-2 text-sm font-semibold text-amber-900">
          Estimated equity protest savings:
          <span class="text-lg">${{ estimated_savings|floatformat:0|intcomma }}</span>
        </p>
        {% endif %}
        <p class="mt-1 text-xs text-amber-700">
          {{ comps_below_subject }} of {{ qualifying_comp_count }}
          comparable propert{{ qualifying_comp_count|pluralize:"y,ies" }}
          {{ comps_below_subject|pluralize:"is,are" }} assessed below your $/sqft.
        </p>
      </div>
      {% else %}
      <div class="rounded-xl p-5 mb-6 bg-green-50 border-2 border-green-300 shadow-md">
        <h2 class="text-base font-bold text-green-900">At or Below Comparable Median</h2>
        <p class="mt-1 text-sm text-green-800">
          Your property's assessed value per sqft
          (${{ subject_value_per_sqft|floatformat:2 }})
          is at or below the median of {{ qualifying_comp_count }}
          comparable propert{{ qualifying_comp_count|pluralize:"y,ies" }}
          (${{ median_comp_value_per_sqft|floatformat:2 }}/sqft).
          An equity protest may not be the strongest argument for this property.
        </p>
      </div>
      {% endif %}
    {% elif not subject_value_per_sqft %}
    <div class="rounded-xl p-4 mb-6 bg-gray-50 border border-gray-200">
      <p class="text-sm text-gray-700">
        <strong>Equity analysis unavailable:</strong>
        This property does not have an assessed value and/or living area on record.
        The comparable table below is still available for review.
      </p>
    </div>
    {% elif not qualifying_comp_count %}
    <div class="rounded-xl p-4 mb-6 bg-blue-50 border border-blue-200">
      <p class="text-sm text-blue-800">
        No qualifying comparables found at the current similarity threshold.
        Try lowering the minimum score using the slider below.
      </p>
    </div>
    {% endif %}

    <!-- ── How to Use This Analysis ── -->
    <div class="mb-6 no-print">
      <details class="bg-white border border-gray-200 rounded-xl shadow-sm">
        <summary class="px-6 py-4 cursor-pointer text-sm font-semibold text-gray-800 select-none list-none flex items-center justify-between">
          <span>ℹ How to Use This Analysis</span>
          <svg class="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
          </svg>
        </summary>
        <div class="px-6 py-4 border-t border-gray-100 grid grid-cols-1 md:grid-cols-3 gap-6 text-sm text-gray-700">
          <div>
            <h3 class="font-semibold text-gray-900 mb-2">Equity Protest (Texas §41.43)</h3>
            <p>
              Texas law requires "equal and uniform" appraisal. If your property is appraised
              at a higher ratio than comparable properties of the same kind and character, the
              appraisal district must prove otherwise by <em>preponderance of the evidence</em>.
              The comparable table below is direct evidence for this argument.
            </p>
            <p class="mt-2 text-xs text-gray-500">
              Exchange evidence with the ARB at least <strong>5 business days</strong> before
              your hearing date.
            </p>
          </div>
          <div>
            <h3 class="font-semibold text-gray-900 mb-2">Market Value Protest</h3>
            <p>
              A separate argument: your appraised value exceeds what a willing buyer would pay.
              This requires <strong>recent sale prices</strong> (MLS data). This tool uses HCAD
              assessed values, which are a proxy — not sale prices. Supplement with MLS comps
              from a real estate professional for a market value argument.
            </p>
          </div>
          <div>
            <h3 class="font-semibold text-gray-900 mb-2">Submitting Your Evidence</h3>
            <ul class="list-disc list-inside space-y-1">
              <li>Print 3 copies of this report to bring to your ARB hearing.</li>
              <li>Upload evidence to HCAD's online protest portal at least 5 days before your hearing.</li>
              <li>Include photos of property condition issues if relevant.</li>
              <li>The protest deadline is typically <strong>May 15</strong> or 30 days after your notice of appraised value, whichever is later.</li>
            </ul>
          </div>
        </div>
      </details>
    </div>

    <!-- ── Controls Row ── -->
    <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 mb-6 no-print">
      <div class="flex items-center gap-3">
        <label for="min_score_slider" class="text-sm font-medium text-gray-700 whitespace-nowrap">
          Min similarity score:
        </label>
        <input
          type="range"
          id="min_score_slider"
          min="52" max="100" step="1"
          value="{{ min_score|floatformat:0 }}"
          class="w-32 accent-indigo-600"
          oninput="document.getElementById('score_display').textContent = this.value"
          onchange="window.location.href = '?min_score=' + this.value"
        />
        <span id="score_display" class="text-sm font-semibold text-indigo-700 w-8">
          {{ min_score|floatformat:0 }}
        </span>
      </div>
      <div class="flex items-center gap-3">
        <a href="{% url 'protest_analysis_export' target_property.account_number %}?min_score={{ min_score|floatformat:0 }}"
           class="inline-flex items-center px-4 py-2 border border-gray-300 rounded-lg text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 shadow-sm">
          <svg class="w-4 h-4 mr-2 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
          </svg>
          Download CSV
        </a>
        <button onclick="window.print()"
                class="inline-flex items-center px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm font-medium hover:bg-indigo-700 shadow-sm">
          <svg class="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M17 17h2a2 2 0 002-2v-4a2 2 0 00-2-2H5a2 2 0 00-2 2v4a2 2 0 002 2h2m2 4h6a2 2 0 002-2v-4a2 2 0 00-2-2H9a2 2 0 00-2 2v4a2 2 0 002 2zm8-12V5a2 2 0 00-2-2H9a2 2 0 00-2 2v4h10z"/>
          </svg>
          Print Report
        </button>
      </div>
    </div>

    <!-- ── Comparable Properties Table ── -->
    <div class="bg-white rounded-2xl shadow-xl border border-gray-100 overflow-hidden mb-8">
      <div class="bg-gradient-to-r from-green-600 to-emerald-700 px-8 py-5">
        <h2 class="text-xl font-bold text-white">Comparable Properties</h2>
        <p class="text-green-100 text-sm mt-1">
          {{ comps|length }} propert{{ comps|length|pluralize:"y,ies" }} with similarity score ≥ {{ min_score|floatformat:0 }}
        </p>
      </div>

      {% if comps %}
      <div class="overflow-x-auto">
        <table class="min-w-full divide-y divide-gray-200 text-xs lg:text-sm">
          <thead class="bg-gray-50 sticky top-0 z-10">
            <tr>
              <th class="px-3 py-3 text-left text-xs font-medium text-gray-600 uppercase">Address</th>
              <th class="px-3 py-3 text-left text-xs font-medium text-gray-600 uppercase">Score</th>
              <th class="px-3 py-3 text-right text-xs font-medium text-gray-600 uppercase">Sqft</th>
              <th class="px-3 py-3 text-right text-xs font-medium text-gray-600 uppercase">Bed/Bath</th>
              <th class="px-3 py-3 text-right text-xs font-medium text-gray-600 uppercase">Year</th>
              <th class="px-3 py-3 text-center text-xs font-medium text-gray-600 uppercase">Qual</th>
              <th class="px-3 py-3 text-right text-xs font-medium text-gray-600 uppercase">Assessed Value</th>
              {% if subject_value_per_sqft %}
              <th class="px-3 py-3 text-right text-xs font-medium text-gray-600 uppercase">$/sqft</th>
              <th class="px-3 py-3 text-right text-xs font-medium text-gray-600 uppercase">vs. Yours</th>
              {% endif %}
            </tr>
          </thead>
          <tbody class="bg-white divide-y divide-gray-100">
            {% for c in comps %}
            <tr class="hover:bg-gray-50">
              <td class="px-3 py-3">
                <a href="{% url 'similar_properties' c.account_number %}"
                   class="font-medium text-blue-700 hover:underline">
                  {{ c.address }} {{ c.street_name }}
                </a>
                <div class="text-xs text-gray-500">{{ c.zip_code }}</div>
              </td>
              <td class="px-3 py-3">
                <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium
                  {% if c.similarity_score >= 84 %}bg-green-100 text-green-800
                  {% elif c.similarity_score >= 70 %}bg-blue-100 text-blue-800
                  {% else %}bg-yellow-100 text-yellow-800{% endif %}">
                  {{ c.similarity_score|floatformat:0 }}
                </span>
                <div class="text-xs text-gray-500 mt-0.5">{{ c.match_label }}</div>
              </td>
              <td class="px-3 py-3 text-right text-gray-700">
                {% if c.heat_area %}{{ c.heat_area|floatformat:0 }}{% else %}–{% endif %}
              </td>
              <td class="px-3 py-3 text-right text-gray-700">
                {{ c.bedrooms|default:"–" }} / {{ c.bathrooms|default:"–" }}
              </td>
              <td class="px-3 py-3 text-right text-gray-700">
                {{ c.year_built|default:"–" }}
              </td>
              <td class="px-3 py-3 text-center">
                {% if c.quality_code %}
                <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium
                  {% if c.quality_code == 'X' %}bg-purple-100 text-purple-800
                  {% elif c.quality_code == 'A' %}bg-green-100 text-green-800
                  {% elif c.quality_code == 'B' %}bg-blue-100 text-blue-800
                  {% elif c.quality_code == 'C' %}bg-yellow-100 text-yellow-800
                  {% elif c.quality_code == 'D' %}bg-orange-100 text-orange-800
                  {% else %}bg-gray-100 text-gray-800{% endif %}">
                  {{ c.quality_code }}
                </span>
                {% else %}–{% endif %}
              </td>
              <td class="px-3 py-3 text-right font-semibold text-blue-700">
                {% if c.assessed_value %}${{ c.assessed_value|floatformat:0|intcomma }}{% else %}–{% endif %}
              </td>
              {% if subject_value_per_sqft %}
              <td class="px-3 py-3 text-right text-gray-700">
                {% if c.comp_value_per_sqft %}${{ c.comp_value_per_sqft|floatformat:2 }}{% else %}–{% endif %}
              </td>
              <td class="px-3 py-3 text-right font-semibold">
                {% if c.comp_delta is not None %}
                  {% if c.comp_delta < 0 %}
                  <span class="text-green-700">{{ c.comp_delta|floatformat:2 }}</span>
                  {% else %}
                  <span class="text-red-700">+{{ c.comp_delta|floatformat:2 }}</span>
                  {% endif %}
                {% else %}–{% endif %}
              </td>
              {% endif %}
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="p-10 text-center text-gray-500">
        <p class="text-base font-medium">No comparable properties found at similarity score ≥ {{ min_score|floatformat:0 }}.</p>
        <p class="mt-1 text-sm">Try lowering the minimum score using the slider above.</p>
      </div>
      {% endif %}
    </div>

    <!-- ── Legal Disclaimer ── -->
    <p class="text-xs text-gray-400 italic text-center mb-8 no-print">
      This analysis uses Harris County Appraisal District (HCAD) data and is for informational
      purposes only. It does not constitute legal or appraisal advice. Assessed values are from
      HCAD records and may not reflect current market sale prices. Consult a licensed property
      tax consultant or appraiser for professional advice.
    </p>

  </div>
</div>
{% endblock %}
```

- [ ] **Step 2: Run the full test suite**

```bash
docker compose exec web python manage.py test --verbosity=1
```

Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add templates/protest_analysis.html
git commit -m "feat: add protest_analysis.html template with print layout and equity comparison"
```

---

## Task 6: Add "Protest Analysis" link to `similar_properties.html`

**Files:**
- Modify: `templates/similar_properties.html`

- [ ] **Step 1: Add the "Protest Analysis" button in the target property card header**

In `templates/similar_properties.html`, the target property card header is the `<div class="bg-gradient-to-r from-indigo-500 to-purple-600 px-8 py-6">` block (around line 39–42). Replace the existing header div with:

```html
<div class="bg-gradient-to-r from-indigo-500 to-purple-600 px-8 py-6 flex items-center justify-between">
    <h1 class="text-2xl lg:text-3xl font-bold text-white">Finding Properties Similar To:</h1>
    {% if target_property %}
    <a href="{% url 'protest_analysis' target_property.account_number %}"
       class="inline-flex items-center px-4 py-2 bg-white bg-opacity-20 hover:bg-opacity-30 border border-white border-opacity-40 rounded-lg text-sm font-semibold text-white shadow-sm">
      <svg class="w-4 h-4 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
              d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
      </svg>
      Protest Analysis
    </a>
    {% endif %}
</div>
```

- [ ] **Step 2: Run the full test suite**

```bash
docker compose exec web python manage.py test --verbosity=1
```

Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add templates/similar_properties.html
git commit -m "feat: add Protest Analysis link to similar properties page"
```

---

## Verification Checklist

Run each step manually after all tasks are complete:

- [ ] `docker compose up web db redis` starts without errors
- [ ] Navigate to `/similar/<known_account>/` — "Protest Analysis" button appears in the header
- [ ] Click "Protest Analysis" — loads `/protest/<account>/`, shows subject card with address and assessed value
- [ ] If subject is overassessed: amber banner shows $/sqft comparison and estimated savings
- [ ] If subject is at/below median: green banner shows
- [ ] Comparable table shows score badges, $/sqft, and color-coded delta (green = comp cheaper, red = comp pricier)
- [ ] Move the slider from 70 to 52 — page reloads with more comps
- [ ] Click "Download CSV" — file downloads with correct 12 columns and data rows
- [ ] Click "Print Report" — navbar, slider, buttons, disclaimer hidden; subject card and table remain
- [ ] Navigate to `/protest/DOESNOTEXIST/` — returns 404
- [ ] `docker compose exec web python manage.py test` — all tests pass
