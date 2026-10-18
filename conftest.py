import pytest

from taxprotest.celery import app


@pytest.fixture(autouse=True, scope="session")
def run_queued_tasks_inline():
    """Admin views queue lifecycle work; tests run it inside the request to observe its outcome.

    Like the worker, a failing task does not raise into the view, which has already redirected.
    """
    app.conf.task_always_eager = True
