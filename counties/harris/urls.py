"""Harris County pages, mounted at the site root with unprefixed URL names."""

from counties.common.urls import county_urlpatterns
from counties.harris.adapter import adapter

urlpatterns = county_urlpatterns(adapter)
