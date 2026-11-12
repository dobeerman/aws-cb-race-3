"""Exercise the live budget runner without calling AWS."""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import smoke
import test_budgets as runner


class BudgetRunnerTest(unittest.TestCase):
    def test_only_budget_specific_422_counts_as_denial(self):
        path = "/v1/chat/completions"
        responses = (
            ({"error": {"type": "budget_exceeded"}}, True),
            ({"error": {"type": "auth_error",
                        "message": "Budget has been exceeded! Key=App A"}}, True),
            ({"detail": [{"type": "missing", "loc": ["body", "messages"]}]}, False),
        )
        for body, is_budget_denial in responses:
            with self.subTest(body=body):
                error = HTTPError(path, 422, "Unprocessable Entity", {},
                                  io.BytesIO(json.dumps(body).encode()))
                with patch.object(smoke.OPENER, "open", side_effect=error):
                    with self.assertRaisesRegex(smoke.GatewayError, "HTTP 422") as caught:
                        smoke.request(path, "test-key", {})
                self.assertEqual(caught.exception.key_budget_exceeded, is_budget_denial)

    def test_waits_for_spend_and_enforcement_before_using_app_b(self):
        with (
            patch.object(runner, "completion", side_effect=[0.006, 0.005, 0.000001, None, 0.000001]) as complete,
            patch.object(runner, "key_spend", side_effect=[0, 0.011, 0, 0, 0.000001]),
            patch.object(runner, "sleep") as sleep,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            runner.exercise("master", "a", "b")

        calls = complete.call_args_list
        self.assertEqual([call.args[:2] for call in calls], [
            ("App A", "a"), ("App A", "a"), ("App A", "a"), ("App A", "a"), ("App B", "b"),
        ])
        self.assertEqual(len(calls[0].args[2]), runner.PADDING_BYTES)
        self.assertEqual(sleep.call_count, 3)

    def test_stops_after_load_request_limit(self):
        with (
            patch.object(runner, "completion", return_value=0.000001) as complete,
            patch.object(runner, "key_spend") as spend,
            self.assertRaisesRegex(RuntimeError, "did not reach"),
        ):
            runner.exercise("master", "a", "b")
        self.assertEqual(complete.call_count, runner.MAX_LOAD_REQUESTS)
        spend.assert_not_called()

    def test_unrelated_throttle_fails_and_both_keys_are_deleted(self):
        calls = []

        def gateway(path, key, body=None, *, query=None):
            calls.append((path, body))
            if path == "/key/generate":
                return {"key": "a" if body["max_budget"] == runner.APP_A_BUDGET else "b"}
            if path == "/v1/chat/completions":
                raise runner.GatewayError(path, 429, False)
            return {}

        with (
            patch.object(runner, "request", side_effect=gateway),
            patch.object(runner, "key_spend", return_value=0),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(runner.run("master"), 1)
        self.assertIn("App A: /v1/chat/completions returned HTTP 429", errors.getvalue())
        self.assertEqual(calls[-2:], [
            ("/key/delete", {"keys": ["a"]}), ("/key/delete", {"keys": ["b"]}),
        ])
