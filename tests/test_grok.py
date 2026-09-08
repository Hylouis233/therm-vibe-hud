import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from sources import grok


class GrokStatusTests(unittest.TestCase):
    def setUp(self):
        grok._cli_cache = None
        grok._cli_cache_at = 0.0
        grok._bot_cache = None
        grok._bot_cache_at = 0.0

    def test_cli_and_bot_are_merged_without_private_paths(self):
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            session_root = root / "sessions"
            cwd = "/private/very/secret-project"
            encoded_cwd = quote(cwd, safe="")
            session_dir = session_root / encoded_cwd / "018f-uuid"
            session_dir.mkdir(parents=True)
            (session_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "updated_at": "2030-01-01T00:00:00Z",
                        "current_model_id": "grok-4.6",
                    }
                ),
                encoding="utf-8",
            )
            (session_dir / "signals.json").write_text(
                json.dumps(
                    {
                        "contextWindowUsage": 42,
                        "contextTokensUsed": 210_000,
                        "contextWindowTokens": 500_000,
                    }
                ),
                encoding="utf-8",
            )
            (session_dir / "updates.jsonl").write_text(
                json.dumps(
                    {
                        "params": {
                            "update": {
                                "usage": {
                                    "inputTokens": 100_000,
                                    "outputTokens": 5_000,
                                    "totalTokens": 105_000,
                                    "cachedReadTokens": 80_000,
                                    "modelCalls": 2,
                                }
                            }
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "active_sessions.json").write_text(
                json.dumps(
                    [
                        {
                            "session_id": "018f-uuid",
                            "pid": os.getpid(),
                            "cwd": cwd,
                            "opened_at": "2030-01-01T00:00:00Z",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with mock.patch.object(grok, "ACTIVE_SESSIONS_PATH", root / "active_sessions.json"):
                with mock.patch.object(grok, "SESSIONS_DIR", session_root):
                    with mock.patch.object(grok, "_bot_process_running", return_value=True):
                        with mock.patch.object(grok, "_bot_credentials", return_value=None):
                            with mock.patch.object(
                                grok,
                                "_latest_cli_billing",
                                return_value={
                                    "percent": 17.0,
                                    "resets_at": 1_893_542_400.0,
                                    "plan": "SuperGrok Heavy",
                                    "on_demand_cap": 0.0,
                                    "on_demand_used": 0.0,
                                    "prepaid_balance": 0.0,
                                    "updated_at": 1_893_456_000.0,
                                },
                            ):
                                status = grok.read_status()

        self.assertEqual(status["tool"], "Grok")
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["identity"], "grok-4.6")
        self.assertEqual(status["context_percent"], 42)
        self.assertEqual(status["grok_cli_percent"], 17.0)
        self.assertEqual(status["grok_cli_resets_at"], 1_893_542_400.0)
        self.assertEqual(status["grok_sessions_24h"], 1)
        self.assertEqual(status["grok_tokens_24h"], 105_000)
        self.assertEqual(status["cache_hit_percent"], 80.0)
        self.assertEqual(status["sessions"][0]["project"], "Grok Bot")
        self.assertEqual(status["sessions"][1]["project"], "secret-project")
        serialized = json.dumps(status)
        self.assertNotIn(cwd, serialized)
        self.assertNotIn("018f-uuid", serialized)

    def test_stale_cli_session_reports_idle_not_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = "/tmp/demo"
            directory = root / quote(cwd, safe="") / "018f-uuid"
            directory.mkdir(parents=True)
            stale_iso = datetime.fromtimestamp(
                time.time() - 20 * 3600, tz=timezone.utc
            ).isoformat()
            (directory / "summary.json").write_text(
                json.dumps({"updated_at": stale_iso}), encoding="utf-8"
            )
            (root / "active_sessions.json").write_text(
                json.dumps(
                    [{"pid": os.getpid(), "session_id": "018f-uuid", "cwd": cwd}]
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                grok, "ACTIVE_SESSIONS_PATH", root / "active_sessions.json"
            ):
                with mock.patch.object(grok, "SESSIONS_DIR", root):
                    sessions = grok._active_sessions()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["state"], "idle")

    def test_bot_usage_parses_percent_plan_and_millisecond_reset(self):
        status_payload = {
            "usagePercent": 1.748973,
            "nextResetTimestampUtc": "2030-01-02T03:04:05Z",
            "hasAvailableUsage": True,
            "grokPlanLabel": "SuperGrok Heavy",
        }
        period_payload = {
            "planUsage": {"totalPercentUsed": 3},
            "billingCycleEnd": "1893456000000",
        }

        def fake_api(method, _credentials):
            return status_payload if method == "GetSandUsageStatus" else period_payload

        with mock.patch.object(grok, "_bot_process_running", return_value=True):
            with mock.patch.object(grok, "_bot_credentials", return_value=("machine", "token")):
                with mock.patch.object(grok, "_api_request", side_effect=fake_api):
                    usage = grok._bot_usage(now=2_000_000_000)

        self.assertEqual(usage["percent"], 1.748973)
        self.assertEqual(usage["plan"], "SuperGrok Heavy")
        self.assertEqual(usage["period_percent"], 3.0)
        self.assertEqual(usage["period_resets_at"], 1_893_456_000.0)
        self.assertTrue(usage["running"])

    def test_latest_cli_billing_reads_official_log_record(self):
        with tempfile.TemporaryDirectory() as raw_root:
            log_path = Path(raw_root) / "unified.jsonl"
            old = {"msg": "billing: fetched credits config", "ctx": {"config": {"creditUsagePercent": 16}}}
            latest = {
                "ts": "2030-01-01T00:00:00Z",
                "msg": "billing: fetched credits config",
                "ctx": {
                    "config": {
                        "creditUsagePercent": 17,
                        "currentPeriod": {"end": "2030-01-02T00:00:00Z"},
                        "onDemandCap": {"val": 0},
                        "onDemandUsed": {"val": 0},
                        "prepaidBalance": {"val": 0},
                        "subscriptionTier": "SuperGrok Heavy",
                    }
                },
            }
            log_path.write_text(
                json.dumps(old) + "\n" + json.dumps(latest) + "\n", encoding="utf-8"
            )
            with mock.patch.object(grok, "UNIFIED_LOG_PATH", log_path):
                billing = grok._latest_cli_billing()

        self.assertEqual(billing["percent"], 17.0)
        self.assertEqual(billing["plan"], "SuperGrok Heavy")
        self.assertEqual(billing["resets_at"], 1893542400.0)
        self.assertEqual(billing["updated_at"], 1893456000.0)

    def test_usage_totals_are_cached_until_the_updates_file_changes(self):
        with tempfile.TemporaryDirectory() as raw_root:
            path = Path(raw_root) / "updates.jsonl"
            event = {
                "params": {
                    "update": {
                        "usage": {
                            "inputTokens": 10,
                            "outputTokens": 2,
                            "totalTokens": 12,
                            "cachedReadTokens": 8,
                            "modelCalls": 1,
                        }
                    }
                }
            }
            path.write_text(json.dumps(event) + "\n", encoding="utf-8")
            first = grok._usage_totals_from_updates(path)
            path.write_text(json.dumps(event) + "\n", encoding="utf-8")
            os.utime(path, ns=(0, 0))
            second = grok._usage_totals_from_updates(path)

        self.assertEqual(first["totalTokens"], 12)
        self.assertIsNot(first, second)

    def test_live_failure_does_not_leak_credentials_or_make_cli_offline(self):
        with mock.patch.object(grok, "_bot_process_running", return_value=False):
            with mock.patch.object(grok, "_bot_credentials", return_value=("machine", "token")):
                with mock.patch.object(grok, "_api_request", side_effect=OSError("network")):
                    usage = grok._bot_usage(now=2_000_000_000)

        self.assertIsNone(usage["percent"])
        self.assertFalse(usage["running"])
        self.assertNotIn("token", json.dumps(usage))


if __name__ == "__main__":
    unittest.main()
