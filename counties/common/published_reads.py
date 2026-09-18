"""Keep all county queries in a shared response on one database snapshot."""

from functools import wraps

from django.db import connection, transaction


def consistent_published_read(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        outer = not connection.in_atomic_block
        with transaction.atomic():
            if outer and connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            response = view(*args, **kwargs)
            if hasattr(response, "render"):
                response.render()
            return response

    return wrapped
