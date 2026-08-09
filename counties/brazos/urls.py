"""Brazos County pages, mounted under /brazos/ with ``brazos_`` URL names."""

from counties.brazos.adapter import adapter
from counties.common.urls import county_urlpatterns

urlpatterns = county_urlpatterns(adapter)
