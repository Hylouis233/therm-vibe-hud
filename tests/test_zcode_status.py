import io
import json
import sqlite3
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


def _payload(limits, level="pro"):
    return {"code": 200, "success": True, "data": {"limits": limits, "level": level}}


# (type, unit, number) is the window's identity — unit 3=HOUR, 5=MONTH, 6=WEEK.
FIVE_HOUR = {
    "type": "TOKENS_LIMIT", "unit": 3, "number": 5,
    "percentage": 100, "nextResetTime": 1785495930259,
}
WEEKLY = {
    "type": "TOKENS_LIMIT", "unit": 6, "number": 1,
    "percentage": 42, "nextResetTime": 1785900000000,
}
MONTHLY_TOOLS = {
    "type": "TIME_LIMIT", "unit": 5, "number": 1,
    "usage": 1000, "currentValue": 113, "remaining": 887, "percentage": 11,
    "nextResetTime": 1786811712993,
    "usageDetails": [
        {"modelCode": "search-prime", "usage": 69},
        {"modelCode": "web-reader", "usage": 33},
    ],
}


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
        payload = _payload([FIVE_HOUR, MONTHLY_TOOLS])
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
        self.assertEqual(metrics["zcode_five_hour_percent"], 100)
        self.assertEqual(metrics["zcode_request_percent"], 11)
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
                return_value={"zcode_five_hour_percent": 7, "zcode_plan_level_raw": "pro"},
            ),
            mock.patch.object(
                zcode,
                "_entitlement_metrics",
                return_value={"zcode_five_hour_percent": 2, "zcode_plan": "GLM Coding Pro"},
            ),
            mock.patch.object(zcode, "_read_sessions", return_value=([], None, 0, None)),
            mock.patch.object(zcode, "_host_process_running", return_value=True),
        ):
            status = zcode.read_status()

        self.assertEqual(status["zcode_five_hour_percent"], 7)
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


class ZcodeQuotaParsingTests(unittest.TestCase):
    def test_maps_five_hour_weekly_and_monthly_tool_windows(self):
        parsed = zcode._parse_live_entitlement(
            _payload([MONTHLY_TOOLS, FIVE_HOUR, WEEKLY])
        )

        self.assertEqual(parsed["zcode_five_hour_percent"], 100)
        self.assertEqual(parsed["zcode_five_hour_resets_at"], 1785495930.259)
        self.assertEqual(parsed["zcode_weekly_percent"], 42)
        self.assertEqual(parsed["zcode_weekly_resets_at"], 1785900000.0)
        self.assertEqual(parsed["zcode_request_percent"], 11)
        self.assertEqual(parsed["zcode_request_remaining"], 887)
        self.assertEqual(parsed["zcode_request_total"], 1000)
        self.assertEqual(parsed["zcode_top_feature"], "search-prime")

    def test_absent_weekly_window_is_none_not_misread_from_five_hour(self):
        # Matching on type alone (the old behaviour) made the 5-hour pool
        # masquerade as whichever TOKENS_LIMIT row came first.
        parsed = zcode._parse_live_entitlement(_payload([MONTHLY_TOOLS, FIVE_HOUR]))

        self.assertEqual(parsed["zcode_five_hour_percent"], 100)
        self.assertIsNone(parsed["zcode_weekly_percent"])
        self.assertIsNone(parsed["zcode_weekly_resets_at"])

    def test_five_hour_is_not_confused_with_weekly_token_window(self):
        parsed = zcode._parse_live_entitlement(_payload([WEEKLY]))

        self.assertIsNone(parsed["zcode_five_hour_percent"])
        self.assertEqual(parsed["zcode_weekly_percent"], 42)

    def test_top_feature_ignored_when_all_usage_is_zero(self):
        limit = {**MONTHLY_TOOLS, "usageDetails": [{"modelCode": "zread", "usage": 0}]}
        parsed = zcode._parse_live_entitlement(_payload([limit]))

        self.assertIsNone(parsed["zcode_top_feature"])

    def test_matches_windows_when_unit_and_number_arrive_as_strings(self):
        # z.ai and bigmodel.cn are separate backends; a stringified "3" failing
        # a strict int comparison would blank every GLM bar at once.
        limits = [
            {**FIVE_HOUR, "unit": "3", "number": "5"},
            {**WEEKLY, "unit": "6", "number": "1"},
        ]
        parsed = zcode._parse_live_entitlement(_payload(limits))

        self.assertEqual(parsed["zcode_five_hour_percent"], 100)
        self.assertEqual(parsed["zcode_weekly_percent"], 42)

    def test_zero_percent_is_preserved_and_not_treated_as_missing(self):
        parsed = zcode._parse_live_entitlement(
            _payload([{**FIVE_HOUR, "percentage": 0}])
        )

        self.assertEqual(parsed["zcode_five_hour_percent"], 0)

    def test_malformed_limit_entries_do_not_raise(self):
        parsed = zcode._parse_live_entitlement(
            _payload(["not-a-dict", None, {"type": "TOKENS_LIMIT"}, FIVE_HOUR])
        )

        self.assertEqual(parsed["zcode_five_hour_percent"], 100)

    def test_rejects_unsuccessful_payload(self):
        self.assertIsNone(zcode._parse_live_entitlement({"success": False, "data": {}}))


class ZcodeCacheHitTests(unittest.TestCase):
    def _make_db(self, rows):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "db.sqlite"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE message (id INTEGER PRIMARY KEY, session_id TEXT, data TEXT)")
        con.executemany("INSERT INTO message (session_id, data) VALUES (?, ?)", rows)
        con.commit()
        con.close()
        return path

    def test_skips_zero_token_rows_to_find_real_cache_hit(self):
        # Each session's newest row is typically a zero-token assistant stub,
        # which is why reading only that row left CACHE HIT blank.
        path = self._make_db([
            ("s1", '{"tokens":{"total":34232,"input":30830,"cache":{"read":29760}}}'),
            ("s1", '{"tokens":{"total":null,"input":0,"cache":{"read":0}}}'),
            ("s1", '{"tokens":{"total":null,"input":0,"cache":{"read":0}}}'),
        ])

        with mock.patch.object(zcode, "DB_PATH", path):
            tokens, hit = zcode._latest_token_stats()

        self.assertEqual(tokens, 34232)
        self.assertAlmostEqual(hit, 29760 / 30830 * 100)

    def test_returns_none_when_no_row_carries_tokens(self):
        path = self._make_db([("s1", '{"tokens":{"total":null,"input":0}}')])

        with mock.patch.object(zcode, "DB_PATH", path):
            self.assertEqual(zcode._latest_token_stats(), (None, None))

    def test_missing_database_is_not_an_error(self):
        with mock.patch.object(zcode, "DB_PATH", Path("/nonexistent/db.sqlite")):
            self.assertEqual(zcode._latest_token_stats(), (None, None))


if __name__ == "__main__":
    unittest.main()
