import base64
import json
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from sources import minimax


class MiniMaxStatusTests(unittest.TestCase):
    def test_desktop_auth_pairs_config_token_with_local_real_user(self):
        def encoded(payload):
            return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

        token = ".".join(
            (
                encoded({"alg": "none", "typ": "JWT"}),
                encoded(
                    {
                        "exp": time.time() + 3600,
                        "user": {
                            "id": "sub-user",
                            "deviceID": "",
                        },
                    }
                ),
                "test-signature",
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "minimax-agent-cn-config.json"
            leveldb_path = root / "leveldb"
            leveldb_path.mkdir()
            config_path.write_text(
                json.dumps(
                    {
                        "tokens": {"accessToken": token},
                        "user": {"deviceID": "desktop-device"},
                    }
                )
            )
            (leveldb_path / "000001.log").write_bytes(
                b"\x00user_detail_agent\x01"
                + json.dumps(
                    {
                        "realUserID": "real-user",
                        "token": token,
                    }
                ).encode()
            )
            with (
                mock.patch.object(minimax, "DESKTOP_CONFIG_PATH", config_path),
                mock.patch.object(
                    minimax, "DESKTOP_LEVELDB_PATH", leveldb_path
                ),
            ):
                auth = minimax._load_desktop_auth()

        self.assertEqual(auth["token"], token)
        self.assertEqual(auth["user_id"], "real-user")
        self.assertEqual(auth["device_id"], "desktop-device")

    def test_load_endpoint_config_reuses_local_provider_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider_env = Path(tmp) / "minimax.env"
            provider_env.write_text(
                "MINIMAX_BASE_URL=https://api.minimaxi.com/anthropic\n"
                "MINIMAX_API_KEY=local-test-key\n"
            )
            with (
                mock.patch.object(minimax, "PROVIDER_ENV_PATH", provider_env),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                config = minimax._load_endpoint_config()

        self.assertEqual(config["origin"], "https://api.minimaxi.com")
        self.assertEqual(
            config["token_plan_url"],
            "https://api.minimaxi.com/v1/token_plan/remains",
        )
        self.assertEqual(config["api_key"], "local-test-key")

    def test_minimax_io_config_uses_matching_token_plan_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider_env = Path(tmp) / "minimax.env"
            provider_env.write_text(
                "MINIMAX_BASE_URL=https://api.minimax.io/v1\n"
                "MINIMAX_API_KEY=international-test-key\n"
            )
            with (
                mock.patch.object(minimax, "PROVIDER_ENV_PATH", provider_env),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                config = minimax._load_endpoint_config()

        self.assertEqual(config["origin"], "https://api.minimax.io")
        self.assertEqual(
            config["token_plan_url"],
            "https://api.minimax.io/v1/token_plan/remains",
        )

    def test_rejects_unverified_hosts_before_reusing_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider_env = Path(tmp) / "minimax.env"
            for base_url in (
                "https://example.com/v1",
                "https://minimax.com/v1",
                "https://anything.minimax.com/v1",
            ):
                provider_env.write_text(
                    f"MINIMAX_BASE_URL={base_url}\n"
                    "MINIMAX_API_KEY=must-not-leave-this-host\n"
                )
                with (
                    mock.patch.object(minimax, "PROVIDER_ENV_PATH", provider_env),
                    mock.patch.dict(os.environ, {}, clear=True),
                ):
                    self.assertIsNone(minimax._load_endpoint_config())

    def test_token_plan_remaining_counts_become_used_percentages(self):
        parsed = minimax._parse_token_plan(
            {
                "base_resp": {"status_code": 0},
                "model_remains": [
                    {
                        "model_name": "general",
                        "current_interval_total_count": 100,
                        "current_interval_usage_count": 75,
                        "end_time": 1_900_000_000_000,
                        "current_weekly_total_count": 400,
                        "current_weekly_usage_count": 100,
                        "weekly_end_time": 1_900_086_400_000,
                    }
                ],
            }
        )

        self.assertTrue(parsed["minimax_token_plan"])
        self.assertEqual(parsed["minimax_five_hour_percent"], 25.0)
        self.assertEqual(parsed["minimax_weekly_percent"], 75.0)
        self.assertEqual(parsed["minimax_five_hour_remaining"], 75)
        self.assertEqual(parsed["minimax_weekly_remaining"], 100)
        self.assertEqual(parsed["minimax_five_hour_resets_at"], 1_900_000_000)
        self.assertEqual(parsed["minimax_weekly_resets_at"], 1_900_086_400)

    def test_token_plan_remaining_percent_handles_zero_count_subscription(self):
        parsed = minimax._parse_token_plan(
            {
                "base_resp": {"status_code": 0},
                "model_remains": [
                    {
                        "model_name": "general",
                        "current_interval_total_count": 0,
                        "current_interval_usage_count": 0,
                        "current_interval_remaining_percent": 94,
                        "current_interval_status": 1,
                        "end_time": 1_900_000_000_000,
                        "current_weekly_total_count": 0,
                        "current_weekly_usage_count": 0,
                        "current_weekly_remaining_percent": 100,
                        "current_weekly_status": 3,
                        "weekly_end_time": 1_900_086_400_000,
                    }
                ],
            }
        )

        self.assertTrue(parsed["minimax_token_plan"])
        self.assertEqual(parsed["minimax_five_hour_percent"], 6.0)
        self.assertEqual(parsed["minimax_weekly_percent"], 0.0)
        self.assertEqual(parsed["minimax_five_hour_status"], 1)
        self.assertEqual(parsed["minimax_weekly_status"], 3)

    def test_token_plan_endpoint_is_the_online_authority(self):
        quota_payload = {
            "base_resp": {"status_code": 0},
            "model_remains": [
                {
                    "model_name": "general",
                    "current_interval_total_count": 0,
                    "current_interval_usage_count": 0,
                    "current_interval_remaining_percent": 94,
                    "current_interval_status": 1,
                    "current_weekly_total_count": 0,
                    "current_weekly_usage_count": 0,
                    "current_weekly_remaining_percent": 100,
                    "current_weekly_status": 3,
                }
            ],
        }
        with (
            mock.patch.object(
                minimax,
                "_load_endpoint_config",
                return_value={
                    "origin": "https://api.minimaxi.com",
                    "token_plan_url": "https://api.minimaxi.com/v1/token_plan/remains",
                    "api_key": "test-key",
                },
            ),
            mock.patch.object(
                minimax,
                "_read_endpoint_json",
                return_value=quota_payload,
            ) as read_json,
        ):
            status = minimax._fetch_endpoint_status()

        self.assertEqual(status["health"], "online")
        self.assertEqual(status["data_source"], "minimax-token-plan")
        self.assertEqual(status["plan_type"], "Token Plan")
        self.assertTrue(status["minimax_token_plan"])
        self.assertEqual(status["minimax_five_hour_percent"], 6.0)
        self.assertEqual(status["minimax_weekly_percent"], 0.0)
        self.assertEqual(read_json.call_count, 1)
        self.assertEqual(
            read_json.call_args.args[0],
            "https://api.minimaxi.com/v1/token_plan/remains",
        )

    def test_desktop_plan_uses_personal_workspace_tier_without_account_name(self):
        with (
            mock.patch.object(
                minimax,
                "_load_desktop_auth",
                return_value={
                    "token": "desktop-test-token",
                    "user_id": "user-id",
                    "device_id": "device-id",
                },
            ),
            mock.patch.object(
                minimax,
                "_read_desktop_json",
                return_value={
                    "base_resp": {"status_code": 0},
                    "workspaces": [
                        {
                            "workspace_type": 0,
                            "selected": True,
                            "has_token_plan": True,
                            "token_plan_tier": "PrivateName Ultra Plan",
                        }
                    ],
                },
            ),
        ):
            status = minimax._fetch_desktop_plan_status()

        self.assertEqual(status["plan_type"], "Ultra Plan")
        self.assertTrue(status["has_token_plan"])
        self.assertNotIn("PrivateName", status["plan_type"])

    def test_authenticated_probe_does_not_follow_redirects(self):
        redirect = urllib.error.HTTPError(
            "https://api.minimaxi.com/v1/models",
            302,
            "Found",
            {"Location": "https://example.com/steal"},
            None,
        )
        with mock.patch.object(
            minimax._no_redirect_opener, "open", side_effect=redirect
        ) as opened:
            with self.assertRaises(urllib.error.HTTPError):
                minimax._read_endpoint_json(
                    "https://api.minimaxi.com/v1/token_plan/remains", "test-key"
                )

        self.assertEqual(opened.call_count, 1)

    def test_recent_sessions_merge_codex_and_claude_minimax_usage(self):
        codex_session = {
            "state": "idle",
            "detail": "waiting for input",
            "project": "~/codex-project",
            "updated_at": 100,
            "context_percent": 12.0,
            "cache_hit_percent": 70.0,
            "model": "MiniMax-M3",
        }
        claude_session = {
            "state": "running",
            "detail": "tool",
            "project": "~/claude-project",
            "updated_at": 200,
            "context_tokens": 250_000,
            "cache_hit_percent": 80.0,
            "model": "MiniMax-M3",
        }
        with (
            mock.patch.object(
                minimax.codex_cli,
                "_recent_rollouts",
                return_value=[(Path("/tmp/codex"), 100)],
            ),
            mock.patch.object(
                minimax.codex_cli, "_parse_session", return_value=codex_session
            ),
            mock.patch.object(
                minimax.claude_code,
                "_recent_transcripts",
                return_value=[(Path("/tmp/claude"), 200)],
            ),
            mock.patch.object(
                minimax.claude_code, "_parse_session", return_value=claude_session
            ),
        ):
            sessions = minimax._recent_minimax_sessions()

        self.assertEqual(len(sessions), 2)
        claude = next(
            session
            for session in sessions
            if session["project"] == "~/claude-project"
        )
        self.assertEqual(claude["context_tokens"], 250_000)
        self.assertEqual(claude["context_window"], 400_000)
        self.assertEqual(claude["context_percent"], 62.5)
        self.assertEqual(claude["cache_hit_percent"], 80.0)

    def test_online_endpoint_is_idle_without_recent_minimax_session(self):
        endpoint = {
            "health": "online",
            "plan_type": "Token Plan",
            "minimax_token_plan": True,
        }
        with (
            mock.patch.object(minimax, "_endpoint_status", return_value=endpoint),
            mock.patch.object(minimax, "_recent_minimax_sessions", return_value=[]),
            mock.patch.object(minimax, "_desktop_running", return_value=False),
            mock.patch.object(minimax, "_desktop_plan_status", return_value={}),
        ):
            status = minimax.read_status()

        self.assertEqual(status["tool"], "MiniMax")
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["health"], "online")
        self.assertEqual(status["active_count"], 0)
        self.assertEqual(status["sessions"], [])

    def test_running_desktop_is_shown_even_when_token_plan_endpoint_is_offline(self):
        endpoint = {"health": "offline", "endpoint_error": "unreachable"}
        with (
            mock.patch.object(minimax, "_endpoint_status", return_value=endpoint),
            mock.patch.object(minimax, "_recent_minimax_sessions", return_value=[]),
            mock.patch.object(minimax, "_desktop_running", return_value=True),
            mock.patch.object(
                minimax,
                "_desktop_plan_status",
                return_value={"plan_type": "Ultra Plan", "has_token_plan": True},
            ),
        ):
            status = minimax.read_status()

        self.assertEqual(status["health"], "online")
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["plan_type"], "Ultra Plan")
        self.assertEqual(status["sessions"][0]["project"], "MiniMax Code")
        self.assertEqual(status["sessions"][0]["state"], "idle")

    def test_active_session_sorts_ahead_of_desktop_idle_row(self):
        endpoint = {
            "health": "online",
            "plan_type": "Token Plan",
            "minimax_token_plan": True,
        }
        recent = [
            {
                "state": "running",
                "detail": "tool",
                "project": "~/project",
                "updated_at": 1_900_000_000,
                "context_percent": 25.0,
                "cache_hit_percent": 80.0,
            }
        ]
        with (
            mock.patch.object(minimax, "_endpoint_status", return_value=endpoint),
            mock.patch.object(
                minimax, "_recent_minimax_sessions", return_value=recent
            ),
            mock.patch.object(minimax, "_desktop_running", return_value=True),
            mock.patch.object(
                minimax,
                "_desktop_plan_status",
                return_value={"plan_type": "Ultra Plan", "has_token_plan": True},
            ),
        ):
            status = minimax.read_status()

        self.assertEqual(status["sessions"][0]["project"], "~/project")
        self.assertEqual(status["sessions"][0]["state"], "running")
        self.assertEqual(status["sessions"][1]["project"], "MiniMax Code")
        self.assertEqual(status["active_count"], 1)

    def test_latest_session_usage_populates_context_and_cache(self):
        endpoint = {
            "health": "online",
            "plan_type": "Token Plan",
            "minimax_token_plan": True,
        }
        recent = [
            {
                "state": "running",
                "detail": "tool",
                "project": "~/project",
                "updated_at": 1_900_000_000,
                "context_tokens": 250_000,
                "context_window": 1_000_000,
                "context_percent": 25.0,
                "cache_hit_percent": 80.0,
            }
        ]
        with (
            mock.patch.object(minimax, "_endpoint_status", return_value=endpoint),
            mock.patch.object(
                minimax, "_recent_minimax_sessions", return_value=recent
            ),
            mock.patch.object(minimax, "_desktop_running", return_value=False),
            mock.patch.object(minimax, "_desktop_plan_status", return_value={}),
        ):
            status = minimax.read_status()

        self.assertEqual(status["context_tokens"], 250_000)
        self.assertEqual(status["context_window"], 1_000_000)
        self.assertEqual(status["context_percent"], 25.0)
        self.assertEqual(status["cache_hit_percent"], 80.0)

    def test_offline_endpoint_clears_recent_minimax_sessions(self):
        endpoint = {"health": "offline", "endpoint_error": "unreachable"}
        recent = [
            {
                "state": "running",
                "detail": "tool",
                "project": "~/project",
                "updated_at": 1_900_000_000,
                "context_percent": 12.0,
                "cache_hit_percent": 80.0,
            }
        ]
        with (
            mock.patch.object(minimax, "_endpoint_status", return_value=endpoint),
            mock.patch.object(
                minimax, "_recent_minimax_sessions", return_value=recent
            ),
            mock.patch.object(minimax, "_desktop_running", return_value=False),
            mock.patch.object(minimax, "_desktop_plan_status", return_value={}),
        ):
            status = minimax.read_status()

        self.assertEqual(status["state"], "no session")
        self.assertEqual(status["health"], "offline")
        self.assertEqual(status["active_count"], 0)
        self.assertEqual(status["sessions"], [])


class MiniMaxRuntimeUsageTests(unittest.TestCase):
    def _make_db(self, rows, name="runtime-state.sqlite",
                 table="local_runtime_token_usage"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / name
        con = sqlite3.connect(path)
        con.execute(
            f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, ts INTEGER, "
            "model TEXT, input_tokens INTEGER, output_tokens INTEGER, "
            "cache_read_tokens INTEGER, cache_write_tokens INTEGER)"
        )
        con.executemany(
            f"INSERT INTO {table} (ts, model, input_tokens, cache_read_tokens) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        con.commit()
        con.close()
        return path

    def test_derives_context_and_cache_hit_from_newest_turn(self):
        now_ms = int(time.time() * 1000)
        path = self._make_db([
            (now_ms - 60_000, "minimax/MiniMax-M3", 2178, 19814),
            (now_ms, "minimax/MiniMax-M3", 669, 22889),
        ])

        with mock.patch.object(minimax, "RUNTIME_DB_PATHS", (path,)):
            usage = minimax._read_runtime_usage()

        prompt = 669 + 22889
        self.assertEqual(usage["context_tokens"], prompt)
        self.assertEqual(usage["context_window"], 400_000)
        self.assertAlmostEqual(usage["context_percent"], prompt / 400_000 * 100)
        self.assertAlmostEqual(usage["cache_hit_percent"], 22889 / prompt * 100)

    def test_ignores_a_runtime_db_that_stopped_being_written(self):
        stale_ms = int((time.time() - minimax.RUNTIME_USAGE_STALE_SEC - 60) * 1000)
        path = self._make_db([(stale_ms, "minimax/MiniMax-M3", 100, 900)])

        with mock.patch.object(minimax, "RUNTIME_DB_PATHS", (path,)):
            self.assertEqual(minimax._read_runtime_usage(), {})

    def test_falls_back_to_the_legacy_v1_database(self):
        now_ms = int(time.time() * 1000)
        legacy = self._make_db(
            [(now_ms, "minimax/MiniMax-M2.7", 500, 1500)],
            name="sqlite.db",
            table="token_usage",
        )
        missing = legacy.parent / "runtime-state.sqlite"

        with mock.patch.object(minimax, "RUNTIME_DB_PATHS", (missing, legacy)):
            usage = minimax._read_runtime_usage()

        self.assertEqual(usage["context_tokens"], 2000)
        self.assertEqual(usage["context_window"], 200_000)

    def test_unknown_model_reports_cache_hit_without_a_context_percent(self):
        now_ms = int(time.time() * 1000)
        path = self._make_db([(now_ms, "minimax/Some-Future-Model", 100, 900)])

        with mock.patch.object(minimax, "RUNTIME_DB_PATHS", (path,)):
            usage = minimax._read_runtime_usage()

        self.assertIsNone(usage["context_percent"])
        self.assertAlmostEqual(usage["cache_hit_percent"], 90.0)

    def test_missing_database_is_not_an_error(self):
        with mock.patch.object(
            minimax, "RUNTIME_DB_PATHS", (Path("/nonexistent/runtime-state.sqlite"),)
        ):
            self.assertEqual(minimax._read_runtime_usage(), {})

    def test_session_derived_values_win_over_the_runtime_database(self):
        # The CONTEXT bar sits directly under the session rows, so when a
        # scanned session has real numbers they must not be overridden.
        endpoint = {
            "health": "online", "data_source": "minimax-token-plan",
            "endpoint_error": None, "plan_type": "Token Plan",
            "minimax_token_plan": True,
        }
        recent = [{
            "state": "idle", "detail": "", "project": "", "updated_at": time.time(),
            "context_tokens": 1234, "context_window": 400_000,
            "context_percent": 12.0, "cache_hit_percent": 50.0,
        }]
        runtime = {
            "context_tokens": 9999, "context_window": 400_000,
            "context_percent": 99.0, "cache_hit_percent": 99.0,
        }
        with (
            mock.patch.object(minimax, "_endpoint_status", return_value=endpoint),
            mock.patch.object(minimax, "_recent_minimax_sessions", return_value=recent),
            mock.patch.object(minimax, "_desktop_running", return_value=False),
            mock.patch.object(minimax, "_desktop_plan_status", return_value={}),
            mock.patch.object(minimax, "_runtime_usage", return_value=runtime),
        ):
            status = minimax.read_status()

        self.assertEqual(status["context_percent"], 12.0)
        self.assertEqual(status["cache_hit_percent"], 50.0)

    def test_runtime_database_fills_in_when_no_session_was_scanned(self):
        endpoint = {
            "health": "online", "data_source": "minimax-token-plan",
            "endpoint_error": None, "plan_type": "Token Plan",
            "minimax_token_plan": True,
        }
        runtime = {
            "context_tokens": 23558, "context_window": 400_000,
            "context_percent": 5.89, "cache_hit_percent": 97.16,
        }
        with (
            mock.patch.object(minimax, "_endpoint_status", return_value=endpoint),
            mock.patch.object(minimax, "_recent_minimax_sessions", return_value=[]),
            mock.patch.object(minimax, "_desktop_running", return_value=True),
            mock.patch.object(minimax, "_desktop_plan_status", return_value={}),
            mock.patch.object(minimax, "_runtime_usage", return_value=runtime),
        ):
            status = minimax.read_status()

        self.assertEqual(status["context_tokens"], 23558)
        self.assertAlmostEqual(status["context_percent"], 5.89)
        self.assertAlmostEqual(status["cache_hit_percent"], 97.16)


if __name__ == "__main__":
    unittest.main()
