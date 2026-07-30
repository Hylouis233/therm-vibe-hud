import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sources import kimi


class KimiUsageTests(unittest.TestCase):
    def test_parses_weekly_and_five_hour_usage(self):
        parsed = kimi._parse_usage_payload({
            "usage": {"used": "25", "limit": "100", "resetTime": "2030-01-08T00:00:00Z"},
            "limits": [{
                "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                "detail": {"used": "3", "limit": "20", "resetTime": "2030-01-01T05:00:00Z"},
            }],
        })

        self.assertEqual(parsed["kimi_weekly_percent"], 25.0)
        self.assertEqual(parsed["kimi_five_hour_percent"], 15.0)
        self.assertEqual(parsed["kimi_weekly_resets_at"], "2030-01-08T00:00:00Z")
        self.assertEqual(parsed["kimi_five_hour_resets_at"], "2030-01-01T05:00:00Z")

    def test_keeps_internal_membership_level_out_of_plan_caption(self):
        parsed = kimi._parse_usage_payload({
            "user": {"membership": {"level": "LEVEL_STANDARD"}},
            "usage": {"used": "25", "limit": "100"},
        })

        self.assertEqual(parsed["kimi_membership_level_raw"], "LEVEL_STANDARD")
        self.assertNotIn("plan_type", parsed)

    def test_parses_desktop_subscription_without_credentials(self):
        parsed = kimi._parse_desktop_subscription_line(
            "[2026-07-29 14:36:31.658] [info] [SubscriptionManager] "
            "refreshed(sub): level=30 isMember=true omniRatio=0.1462 "
            "exhausted=false resetAt=2026-08-28T00:00:00.000Z"
        )

        self.assertAlmostEqual(parsed["kimi_monthly_percent"], 14.62)
        self.assertNotIn("plan_type", parsed)
        self.assertFalse(parsed["kimi_subscription_exhausted"])

    def test_reads_real_desktop_membership_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "main.log"
            log_path.write_text(
                "[SubscriptionManager] refreshed(sub): level=30 isMember=true "
                "omniRatio=0.1546 exhausted=false "
                "resetAt=2026-08-28T00:00:00.000Z\n"
                "[KimiAgent] commercialInfo sending (subscription-refresh): "
                "membershipLevel=Vivace canvas=2 localConversation=20 other=20\n"
            )

            with mock.patch.object(kimi, "DESKTOP_LOG_PATH", log_path):
                parsed = kimi._read_desktop_subscription()

        self.assertEqual(parsed["plan_type"], "Vivace")
        self.assertAlmostEqual(parsed["kimi_monthly_percent"], 15.46)

    def test_desktop_daemon_is_idle_when_online_without_active_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "runner.state.json"
            state_path.write_text(json.dumps({
                "daemonGeneration": {"pid": 1234},
                "lifecycleStatus": "running",
                "activeOperations": [],
                "activeKernelTurns": [],
                "activeKernelToolCalls": [],
            }))

            with (
                mock.patch.object(kimi, "DESKTOP_RUNNER_STATE_PATH", state_path),
                mock.patch.object(kimi, "_pid_is_running", return_value=True),
                mock.patch.object(kimi.time, "time", return_value=123456.0),
            ):
                session = kimi._read_desktop_runtime()

        self.assertEqual(session["state"], "idle")
        self.assertEqual(session["project"], "Kimi Desktop")
        self.assertEqual(session["updated_at"], 123456.0)

    def test_desktop_daemon_is_running_with_active_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "runner.state.json"
            state_path.write_text(json.dumps({
                "daemonGeneration": {"pid": 1234},
                "lifecycleStatus": "running",
                "activeOperations": [{"operationId": "local-only"}],
                "activeKernelTurns": [],
                "activeKernelToolCalls": [],
            }))

            with (
                mock.patch.object(kimi, "DESKTOP_RUNNER_STATE_PATH", state_path),
                mock.patch.object(kimi, "_pid_is_running", return_value=True),
            ):
                session = kimi._read_desktop_runtime()

        self.assertEqual(session["state"], "running")

    def test_reads_recent_session_metadata_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "session_1"
            wire = session / "agents" / "main" / "wire.jsonl"
            wire.parent.mkdir(parents=True)
            wire.write_text(json.dumps({"type": "turn.started", "turnId": 1}) + "\n")
            (session / "state.json").write_text(json.dumps({"workDir": "/tmp/project"}))
            index = root / "session_index.jsonl"
            index.write_text(json.dumps({
                "sessionId": "session_1",
                "sessionDir": str(session),
                "workDir": "/tmp/project",
            }) + "\n")

            with mock.patch.object(kimi, "SESSION_INDEX_PATH", index):
                sessions, context = kimi._read_sessions()

        self.assertEqual(context, None)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["project"], "/tmp/project")
        self.assertEqual(sessions[0]["state"], "running")

    def test_atomic_credentials_write_uses_owner_only_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "credentials" / "kimi-code.json"
            with mock.patch.object(kimi, "CREDENTIALS_PATH", target):
                kimi._write_credentials_atomic({
                    "access_token": "secret-access",
                    "refresh_token": "secret-refresh",
                    "expires_at": 123,
                })

            saved = json.loads(target.read_text())
            mode = os.stat(target).st_mode & 0o777

        self.assertEqual(saved["expires_at"], 123)
        self.assertEqual(mode, 0o600)

    def test_read_status_prefers_desktop_product_name_over_internal_level(self):
        with (
            mock.patch.object(kimi, "_read_desktop_subscription", return_value={"plan_type": "Vivace"}),
            mock.patch.object(
                kimi,
                "_managed_usage",
                return_value={"kimi_membership_level_raw": "LEVEL_STANDARD", "plan_type": "Standard"},
            ),
            mock.patch.object(kimi, "_read_sessions", return_value=([], None)),
            mock.patch.object(kimi, "_read_desktop_runtime", return_value=None),
        ):
            status = kimi.read_status()

        self.assertEqual(status["plan_type"], "Vivace")

    def test_read_status_uses_desktop_runtime_when_cli_has_no_session(self):
        desktop_session = {
            "state": "idle",
            "detail": "desktop daemon",
            "project": "Kimi Desktop",
            "updated_at": 123456.0,
        }
        with (
            mock.patch.object(kimi, "_read_desktop_subscription", return_value={}),
            mock.patch.object(kimi, "_managed_usage", return_value={}),
            mock.patch.object(kimi, "_read_sessions", return_value=([], None)),
            mock.patch.object(kimi, "_read_desktop_runtime", return_value=desktop_session),
        ):
            status = kimi.read_status()

        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["active_count"], 0)
        self.assertEqual(status["sessions"], [desktop_session])

    def test_read_status_does_not_return_model_identity(self):
        with (
            mock.patch.object(kimi, "_read_desktop_subscription", return_value={}),
            mock.patch.object(kimi, "_managed_usage", return_value={}),
            mock.patch.object(kimi, "_read_sessions", return_value=([], None)),
            mock.patch.object(kimi, "_read_desktop_runtime", return_value=None),
        ):
            status = kimi.read_status()

        self.assertNotIn("identity", status)


if __name__ == "__main__":
    unittest.main()
