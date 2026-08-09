"""URL configuration for the taxprotest project.

Each county mounts the same set of pages from ``counties.common.urls``:

    /                          Harris County search        (name: index)
    /similar/<account>/        Harris comparables          (name: similar_properties)
    /protest/<account>/        Harris protest report       (name: protest_analysis)
    /brazos/                   Brazos County search        (name: brazos_index)
    /brazos/similar/<id>/      Brazos comparables          (name: brazos_similar_properties)
    /brazos/protest/<id>/      Brazos protest report       (name: brazos_protest_analysis)

plus each county's ``export/``, ``protest/<key>/export/``, and
``protest/<key>/pdf/`` routes. Site-wide pages live in ``taxprotest.views``.
"""

from django.contrib import admin
from django.urls import include, path

from .views import about, healthz, readiness

urlpatterns = [
    path("admin/", admin.site.urls),
    path("about/", about, name="about"),
    path("healthz/", healthz, name="healthz"),
    path("readiness/", readiness, name="readiness"),
    path("brazos/", include("counties.brazos.urls")),
    # Harris is mounted last because it owns the site root.
    path("", include("counties.harris.urls")),
]
