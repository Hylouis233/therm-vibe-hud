import json
import os
import unittest
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import mock

from sources import codex_cli


EMPTY_QUOTA = {
    "usage_percent": None,
    "usage_resets_at": None,
    "model": None,
    "cache_hit_percent": None,
    "plan_type": None,
    "secondary_percent": None,
    "secondary_resets_at": None,
    "credits_balance": None,
    "credits_unlimited": False,
}


class CodexRuntimeStatusTests(unittest.TestCase):
    def test_tail_lines_handles_a_large_json_line_without_losing_tail_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout-large-line.jsonl"
            large_event = json.dumps(
                {
                    "type": "response_item",
                    "payload": {"content": "x" * (1024 * 1024)},
                }
            )
            rollout.write_text(
                "\n".join(["first", large_event, "second", "third"]) + "\n"
            )

            tail = codex_cli._tail_lines(rollout, 3)

        self.assertEqual(tail, [large_event, "second", "third"])

    def test_parse_session_uses_prefix_model_when_tail_has_no_turn_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout-main.jsonl"
            events = [
                {
                    "type": "session_meta",
                    "payload": {"cwd": "/tmp/main", "source": "cli"},
                },
                {
                    "type": "turn_context",
                    "payload": {"cwd": "/tmp/main", "model": "gpt-5.6-sol"},
                },
            ]
            events.extend(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": f"tail-{index}",
                    },
                }
                for index in range(codex_cli.TAIL_LINES + 10)
            )
            rollout.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n"
            )

            parsed = codex_cli._parse_session(rollout, time.time())

        self.assertEqual(parsed["model"], "gpt-5.6-sol")
        self.assertFalse(parsed["is_subagent"])

    def test_parse_session_marks_subagent_from_session_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout-subagent.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "cwd": "/tmp/subagent",
                            "source": {"subagent": {"path": "/root/review"}},
                        },
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "turn_context",
                        "payload": {"model": "gpt-5.5"},
                    }
                )
                + "\n"
            )

            parsed = codex_cli._parse_session(rollout, time.time())

        self.assertEqual(parsed["model"], "gpt-5.5")
        self.assertTrue(parsed["is_subagent"])

    def test_identity_ignores_newer_subagent_model(self):
        main = {
            "state": "running",
            "detail": "main task",
            "project": "/tmp/main",
            "updated_at": 100,
            "usage_percent": None,
            "usage_resets_at": None,
            "usage_limit_id": None,
            "context_percent": None,
            "cache_hit_percent": None,
            "model": "gpt-5.6-sol",
            "is_subagent": False,
        }
        subagent = {
            **main,
            "detail": "review",
            "project": "/tmp/subagent",
            "updated_at": 200,
            "model": "gpt-5.5",
            "is_subagent": True,
        }
        parsed = iter([subagent, main])
        with (
            mock.patch.object(
                codex_cli,
                "_recent_rollouts",
                return_value=[
                    (Path("/tmp/subagent"), 200),
                    (Path("/tmp/main"), 100),
                ],
            ),
            mock.patch.object(
                codex_cli, "_parse_session", side_effect=lambda *_: next(parsed)
            ),
            mock.patch.object(codex_cli, "_app_server_status", return_value=None),
            mock.patch.object(
                codex_cli, "_desktop_app_server_running", return_value=True
            ),
            mock.patch.object(
                codex_cli, "_lifetime_stats", return_value=(0, 0, 0.0)
            ),
            mock.patch.object(
                codex_cli,
                "_last_known_quota",
                return_value={**EMPTY_QUOTA, "model": "gpt-5.5"},
            ),
            mock.patch.object(
                codex_cli, "_load_persisted_resets_at", return_value=None
            ),
        ):
            status = codex_cli.read_status()

        self.assertEqual(status["identity"], "gpt-5.6-sol")

    def test_last_known_model_scan_ignores_newer_subagent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subagent = root / "rollout-subagent.jsonl"
            main = root / "rollout-main.jsonl"
            subagent.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "source": {"subagent": {"path": "/root/review"}}
                        },
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "turn_context",
                        "payload": {"model": "gpt-5.5"},
                    }
                )
                + "\n"
            )
            main.write_text(
                json.dumps(
                    {"type": "session_meta", "payload": {"source": "cli"}}
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "turn_context",
                        "payload": {"model": "gpt-5.6-sol"},
                    }
                )
                + "\n"
            )
            now = time.time()
            os.utime(main, (now - 10, now - 10))
            os.utime(subagent, (now, now))

            with mock.patch.object(codex_cli, "SESSIONS_DIR", root):
                _, _, model, _ = codex_cli._scan_rollouts_for_last_known(
                    scan_model_only=True
                )

        self.assertEqual(model, "gpt-5.6-sol")

    def test_cache_hit_survives_a_rollout_older_than_the_quota_window(self):
        # Cache-hit % has no live API source — the rollout scan is its only
        # tier — and it doesn't decay against a reset window the way
        # used_percent does. Sharing the quota staleness gate blanked the row
        # whenever Codex had been idle for a few hours.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rollout = root / "rollout-main.jsonl"
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"source": "cli"}})
                + "\n"
                + json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"last_token_usage": {
                            "input_tokens": 200, "cached_input_tokens": 150,
                        }},
                    },
                })
                + "\n"
            )
            stale = time.time() - (codex_cli.ROLLOUT_QUOTA_STALE_SEC + 3600)
            os.utime(rollout, (stale, stale))

            with mock.patch.object(codex_cli, "SESSIONS_DIR", root):
                _, _, _, cache_hit = codex_cli._scan_rollouts_for_last_known(
                    scan_model_only=True
                )

        self.assertAlmostEqual(cache_hit, 75.0)

    def test_cache_hit_is_dropped_once_past_its_own_staleness_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rollout = root / "rollout-main.jsonl"
            rollout.write_text(
                json.dumps({"type": "session_meta", "payload": {"source": "cli"}})
                + "\n"
                + json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"last_token_usage": {
                            "input_tokens": 200, "cached_input_tokens": 150,
                        }},
                    },
                })
                + "\n"
            )
            ancient = time.time() - (codex_cli.ROLLOUT_CACHE_HIT_STALE_SEC + 3600)
            os.utime(rollout, (ancient, ancient))

            with mock.patch.object(codex_cli, "SESSIONS_DIR", root):
                _, _, _, cache_hit = codex_cli._scan_rollouts_for_last_known(
                    scan_model_only=True
                )

        self.assertIsNone(cache_hit)

    def test_desktop_probe_detects_app_server_process(self):
        with mock.patch.object(
            codex_cli.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [],
                0,
                stdout=(
                    "/Applications/ChatGPT.app/Contents/Resources/codex "
                    "-c feature=true app-server\n"
                ),
            ),
        ) as run:
            self.assertTrue(codex_cli._probe_desktop_app_server_running())

        run.assert_called_once()

    def test_websocket_handshake_raises_when_socket_closes_early(self):
        client = mock.Mock()
        client.recv.return_value = b""

        with self.assertRaisesRegex(OSError, "closed during websocket upgrade"):
            codex_cli._websocket_handshake(client)

    def test_app_server_threads_are_authoritative(self):
        status = codex_cli._coerce_app_server_threads(
            {
                "data": [
                    {
                        "id": "active-thread",
                        "cwd": "/tmp/active",
                        "name": "Active task",
                        "preview": "fallback title",
                        "updatedAt": 200,
                        "status": {"type": "active", "activeFlags": []},
                    },
                    {
                        "id": "idle-thread",
                        "cwd": "/tmp/idle",
                        "name": None,
                        "preview": "Idle task",
                        "updatedAt": 100,
                        "status": {"type": "idle"},
                    },
                ]
            },
            now=250,
        )

        self.assertEqual(status["state"], "running")
        self.assertEqual(status["active_count"], 1)
        self.assertEqual(status["sessions"][0]["project"], "/tmp/active")
        self.assertEqual(status["sessions"][0]["detail"], "Active task")

    def test_read_status_prefers_app_server_endpoint(self):
        endpoint = {
            "state": "idle",
            "sessions": [],
            "active_count": 0,
            "health": "online",
            "data_source": "app-server",
        }
        with (
            mock.patch.object(codex_cli, "_recent_rollouts", return_value=[]),
            mock.patch.object(codex_cli, "_app_server_status", return_value=endpoint),
            mock.patch.object(codex_cli, "_desktop_app_server_running", return_value=False),
            mock.patch.object(codex_cli, "_lifetime_stats", return_value=(0, 0, 0.0)),
            mock.patch.object(codex_cli, "_last_known_quota", return_value=EMPTY_QUOTA),
        ):
            status = codex_cli.read_status()

        self.assertEqual(status["health"], "online")
        self.assertEqual(status["data_source"], "app-server")
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["active_count"], 0)

    def test_desktop_process_is_idle_without_recent_rollouts(self):
        with (
            mock.patch.object(codex_cli, "_recent_rollouts", return_value=[]),
            mock.patch.object(codex_cli, "_app_server_status", return_value=None),
            mock.patch.object(codex_cli, "_desktop_app_server_running", return_value=True),
            mock.patch.object(codex_cli, "_lifetime_stats", return_value=(0, 0, 0.0)),
            mock.patch.object(codex_cli, "_last_known_quota", return_value=EMPTY_QUOTA),
        ):
            status = codex_cli.read_status()

        self.assertEqual(status["health"], "online")
        self.assertEqual(status["data_source"], "desktop-process")
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["sessions"][0]["project"], "Codex Desktop")

    def test_offline_requires_endpoint_and_desktop_to_be_absent(self):
        with (
            mock.patch.object(codex_cli, "_recent_rollouts", return_value=[]),
            mock.patch.object(codex_cli, "_app_server_status", return_value=None),
            mock.patch.object(codex_cli, "_desktop_app_server_running", return_value=False),
            mock.patch.object(codex_cli, "_lifetime_stats", return_value=(0, 0, 0.0)),
            mock.patch.object(codex_cli, "_last_known_quota", return_value=EMPTY_QUOTA),
        ):
            status = codex_cli.read_status()

        self.assertEqual(status["health"], "offline")
        self.assertEqual(status["state"], "no session")
        self.assertEqual(status["active_count"], 0)

    def test_offline_clears_recent_rollout_rows(self):
        stale_session = {
            "state": "running",
            "detail": "stale task",
            "project": "/tmp/stale",
            "updated_at": 100,
            "usage_percent": None,
            "usage_resets_at": None,
            "usage_limit_id": None,
            "context_percent": None,
            "cache_hit_percent": None,
            "model": None,
        }
        with (
            mock.patch.object(
                codex_cli, "_recent_rollouts", return_value=[(Path("/tmp/stale"), 100)]
            ),
            mock.patch.object(codex_cli, "_parse_session", return_value=stale_session),
            mock.patch.object(codex_cli, "_app_server_status", return_value=None),
            mock.patch.object(codex_cli, "_desktop_app_server_running", return_value=False),
            mock.patch.object(codex_cli, "_lifetime_stats", return_value=(0, 0, 0.0)),
            mock.patch.object(codex_cli, "_last_known_quota", return_value=EMPTY_QUOTA),
            mock.patch.object(codex_cli, "_load_persisted_resets_at", return_value=None),
        ):
            status = codex_cli.read_status()

        self.assertEqual(status["health"], "offline")
        self.assertEqual(status["sessions"], [])


class LiveQuotaBackoffTests(unittest.TestCase):
    def setUp(self):
        codex_cli._live_quota_failure_count = 0
        codex_cli._live_quota_backoff_until = 0.0

    def tearDown(self):
        codex_cli._live_quota_failure_count = 0
        codex_cli._live_quota_backoff_until = 0.0

    def test_repeated_failures_skip_network_during_backoff_window(self):
        with (
            mock.patch.object(
                codex_cli, "_load_auth_tokens", return_value=("token", None, "acct")
            ),
            mock.patch.object(
                codex_cli, "_fetch_usage_once", side_effect=OSError("network down")
            ) as fetch_once,
        ):
            first = codex_cli._fetch_live_quota()
            second = codex_cli._fetch_live_quota()

        self.assertIsNone(first)
        self.assertIsNone(second)
        # The second call landed inside the just-set backoff window, so it
        # must never have reached the network at all.
        fetch_once.assert_called_once()
        self.assertEqual(codex_cli._live_quota_failure_count, 1)

    def test_success_resets_backoff_state(self):
        codex_cli._live_quota_failure_count = 3
        codex_cli._live_quota_backoff_until = 0.0  # already past, so this call is allowed through
        payload = {"rate_limit": {"primary_window": {"used_percent": 42.0}}}
        with (
            mock.patch.object(
                codex_cli, "_load_auth_tokens", return_value=("token", None, "acct")
            ),
            mock.patch.object(codex_cli, "_fetch_usage_once", return_value=payload),
        ):
            result = codex_cli._fetch_live_quota()

        self.assertEqual(result["usage_percent"], 42.0)
        self.assertEqual(codex_cli._live_quota_failure_count, 0)
        self.assertEqual(codex_cli._live_quota_backoff_until, 0.0)


if __name__ == "__main__":
    unittest.main()
