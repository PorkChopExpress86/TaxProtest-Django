"""Keep all county queries in a shared response on one database snapshot."""

from counties.common.views import consistent_published_read

__all__ = ["consistent_published_read"]
