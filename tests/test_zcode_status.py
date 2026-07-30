import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sources import zcode


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class ZCodeRuntimeStatusTests(unittest.TestCase):
    def test_local_auth_does_not_send_custom_provider_key_to_bigmodel(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "model": {"main": "custom/model"},
                        "provider": {
                            "custom": {
                                "options": {
                                    "apiKey": "custom-secret",
                                    "baseURL": "https://example.com/api",
                                }
                            }
                        },
                    }
                )
            )
            with mock.patch.object(zcode, "CLI_CONFIG_PATH", config_path):
                auth = zcode._load_zcode_auth()

        self.assertIsNone(auth)

    def test_host_probe_detects_zcode_process(self):
        with mock.patch.object(
            zcode.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run:
            self.assertTrue(zcode._probe_host_process_running())

        run.assert_called_once()

    def test_live_quota_reuses_local_provider_auth(self):
        payload = {
            "code": 200,
            "success": True,
            "data": {
                "level": "pro",
                "limits": [
                    {"type": "TOKENS_LIMIT", "percentage": 7, "nextResetTime": 2_000},
                    {
                        "type": "TIME_LIMIT",
                        "percentage": 12,
                        "remaining": 88,
                        "usage": 100,
                        "nextResetTime": 3_000,
                    },
                ],
            },
        }
        captured = {}

        def fake_urlopen(request, timeout):
            captured["authorization"] = request.get_header("Authorization")
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return FakeResponse(json.dumps(payload).encode())

        with (
            mock.patch.object(
                zcode,
                "_load_zcode_auth",
                return_value=("local-secret", "https://bigmodel.cn/api/monitor/usage/quota/limit"),
            ),
            mock.patch.object(zcode.urllib.request, "urlopen", side_effect=fake_urlopen),
        ):
            metrics = zcode._fetch_live_entitlement()

        self.assertEqual(captured["authorization"], "local-secret")
        self.assertEqual(metrics["zcode_token_percent"], 7)
        self.assertEqual(metrics["zcode_request_percent"], 12)
        self.assertEqual(metrics["zcode_plan_level_raw"], "pro")
        self.assertNotIn("authorization", metrics)

    def test_host_process_is_idle_without_recent_tasks(self):
        with (
            mock.patch.object(zcode, "_live_entitlement", return_value={}),
            mock.patch.object(zcode, "_entitlement_metrics", return_value={"zcode_plan": "GLM Coding Pro"}),
            mock.patch.object(zcode, "_read_sessions", return_value=([], None, 0, None)),
            mock.patch.object(zcode, "_host_process_running", return_value=True),
        ):
            status = zcode.read_status()

        self.assertEqual(status["health"], "online")
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["display_name"], "GLM")
        self.assertEqual(status["data_source"], "host-process")
        self.assertEqual(status["sessions"][0]["project"], "ZCode Desktop")

    def test_endpoint_metrics_override_cached_usage_but_not_product_name(self):
        with (
            mock.patch.object(
                zcode,
                "_live_entitlement",
                return_value={"zcode_token_percent": 7, "zcode_plan_level_raw": "pro"},
            ),
            mock.patch.object(
                zcode,
                "_entitlement_metrics",
                return_value={"zcode_token_percent": 2, "zcode_plan": "GLM Coding Pro"},
            ),
            mock.patch.object(zcode, "_read_sessions", return_value=([], None, 0, None)),
            mock.patch.object(zcode, "_host_process_running", return_value=True),
        ):
            status = zcode.read_status()

        self.assertEqual(status["zcode_token_percent"], 7)
        self.assertEqual(status["zcode_plan"], "GLM Coding Pro")
        self.assertEqual(status["identity"], "GLM Coding Pro")
        self.assertEqual(status["usage_source"], "endpoint")

    def test_offline_requires_host_process_to_be_absent(self):
        with (
            mock.patch.object(zcode, "_live_entitlement", return_value={}),
            mock.patch.object(zcode, "_entitlement_metrics", return_value={}),
            mock.patch.object(zcode, "_read_sessions", return_value=([], None, 0, None)),
            mock.patch.object(zcode, "_host_process_running", return_value=False),
        ):
            status = zcode.read_status()

        self.assertEqual(status["health"], "offline")
        self.assertEqual(status["state"], "no session")
        self.assertEqual(status["active_count"], 0)

    def test_offline_clears_recent_task_rows(self):
        stale_sessions = [
            {
                "state": "running",
                "detail": "stale task",
                "project": "/tmp/stale",
                "updated_at": 100,
            }
        ]
        with (
            mock.patch.object(zcode, "_live_entitlement", return_value={}),
            mock.patch.object(zcode, "_entitlement_metrics", return_value={}),
            mock.patch.object(
                zcode,
                "_read_sessions",
                return_value=(stale_sessions, None, 0, None),
            ),
            mock.patch.object(zcode, "_host_process_running", return_value=False),
        ):
            status = zcode.read_status()

        self.assertEqual(status["health"], "offline")
        self.assertEqual(status["sessions"], [])


if __name__ == "__main__":
    unittest.main()
