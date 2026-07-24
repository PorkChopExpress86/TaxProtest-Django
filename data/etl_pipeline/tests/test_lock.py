"""Tests for the ETL pipeline concurrency lock (data/etl_pipeline/lock.py).

All tests mock the redis client -- none of this depends on a reachable Redis
instance, matching how the rest of the test suite mocks Celery's .delay().
"""

import json
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from data.etl_pipeline import lock as pipeline_lock


class PipelineLockTests(SimpleTestCase):
    def setUp(self):
        client_patcher = patch("data.etl_pipeline.lock._client")
        self.mock_client_factory = client_patcher.start()
        self.addCleanup(client_patcher.stop)
        self.mock_client = MagicMock()
        self.mock_client_factory.return_value = self.mock_client

    def test_acquire_succeeds_when_no_lock_held(self):
        self.mock_client.set.return_value = True

        pipeline_lock.acquire(scope="full", task_id="task-123")

        args, kwargs = self.mock_client.set.call_args
        self.assertEqual(args[0], pipeline_lock.LOCK_KEY)
        self.assertEqual(kwargs["nx"], True)
        self.assertEqual(kwargs["ex"], pipeline_lock.LOCK_TTL_SECONDS)
        payload = json.loads(args[1])
        self.assertEqual(payload["scope"], "full")
        self.assertEqual(payload["task_id"], "task-123")

    def test_acquire_raises_when_lock_already_held(self):
        self.mock_client.set.return_value = False
        self.mock_client.get.return_value = json.dumps(
            {"scope": "gis-only", "task_id": "other-task", "started_at": 100.0}
        )

        with self.assertRaises(pipeline_lock.PipelineAlreadyRunningError) as ctx:
            pipeline_lock.acquire(scope="full", task_id="task-123")

        self.assertIn("gis-only", str(ctx.exception))

    def test_release_deletes_lock_key(self):
        pipeline_lock.release()

        self.mock_client.delete.assert_called_once_with(pipeline_lock.LOCK_KEY)

    def test_current_run_returns_none_when_not_locked(self):
        self.mock_client.get.return_value = None

        self.assertIsNone(pipeline_lock.current_run())

    def test_current_run_returns_parsed_payload_when_locked(self):
        self.mock_client.get.return_value = json.dumps(
            {"scope": "building-only", "task_id": "task-456", "started_at": 200.0}
        )

        result = pipeline_lock.current_run()

        self.assertEqual(result["scope"], "building-only")
        self.assertEqual(result["task_id"], "task-456")
