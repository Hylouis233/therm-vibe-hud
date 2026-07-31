import json
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from sources import claude_code


class ClaudeCodeProxyStatusTests(unittest.TestCase):
    def test_tail_lines_handles_a_large_json_line_without_losing_tail_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "large-line.jsonl"
            large_event = json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": "x" * (1024 * 1024)},
                }
            )
            transcript.write_text(
                "\n".join(["first", large_event, "second", "third"]) + "\n"
            )

            tail = claude_code._tail_lines(transcript, 3)

        self.assertEqual(tail, [large_event, "second", "third"])

    def test_load_proxy_config_reuses_claude_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_BASE_URL": "http://100.112.53.52:8317",
                            "ANTHROPIC_AUTH_TOKEN": "proxy-secret",
                        }
                    }
                )
            )
            with (
                mock.patch.object(claude_code, "SETTINGS_PATH", settings_path),
                mock.patch.dict(
                    claude_code.os.environ,
                    {
                        "ANTHROPIC_BASE_URL": "",
                        "ANTHROPIC_AUTH_TOKEN": "",
                        "ANTHROPIC_API_KEY": "",
                    },
                ),
            ):
                config = claude_code._load_proxy_config()

        self.assertEqual(config["base_url"], "http://100.112.53.52:8317")
        self.assertEqual(config["token"], "proxy-secret")

    def test_tailscale_host_is_eligible_for_local_proxy_probe(self):
        self.assertTrue(claude_code._is_local_proxy_host("100.112.53.52"))
        self.assertFalse(claude_code._is_local_proxy_host("api.anthropic.com"))

    def test_empty_proxy_sentinel_clears_cached_health(self):
        with (
            mock.patch.object(claude_code, "_load_proxy_config", return_value=None),
            mock.patch.object(
                claude_code._proxy_status_cache, "get", return_value={}
            ),
        ):
            self.assertEqual(claude_code._fetch_proxy_status(), {})
            self.assertIsNone(claude_code._proxy_status())

    def test_proxy_probe_verifies_auth_and_claude_models(self):
        captured = {}

        def fake_read(url, headers=None):
            if url.endswith("/healthz"):
                return 200, {"status": "ok"}
            captured["authorization"] = (headers or {}).get("Authorization")
            return 200, {
                "data": [
                    {"id": "claude-sonnet-4-6"},
                    {"id": "claude-opus-5"},
                    {"id": "gpt-5.6-sol"},
                ]
            }

        with (
            mock.patch.object(
                claude_code,
                "_load_proxy_config",
                return_value={
                    "base_url": "http://100.112.53.52:8317",
                    "token": "proxy-secret",
                },
            ),
            mock.patch.object(
                claude_code, "_read_proxy_json", side_effect=fake_read
            ),
        ):
            status = claude_code._fetch_proxy_status()

        self.assertEqual(status["health"], "online")
        self.assertEqual(status["proxy_auth"], "verified")
        self.assertEqual(status["proxy_model_count"], 3)
        self.assertEqual(status["proxy_claude_model_count"], 2)
        self.assertEqual(captured["authorization"], "Bearer proxy-secret")
        self.assertNotIn("token", status)

    def test_proxy_auth_failure_is_offline(self):
        def fake_read(url, headers=None):
            if url.endswith("/healthz"):
                return 200, {"status": "ok"}
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

        with (
            mock.patch.object(
                claude_code,
                "_load_proxy_config",
                return_value={
                    "base_url": "http://100.112.53.52:8317",
                    "token": "bad-secret",
                },
            ),
            mock.patch.object(
                claude_code, "_read_proxy_json", side_effect=fake_read
            ),
        ):
            status = claude_code._fetch_proxy_status()

        self.assertEqual(status["health"], "offline")
        self.assertEqual(status["proxy_error"], "authentication")

    def test_authenticated_probe_does_not_follow_redirects(self):
        redirect_hits = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/redirect-target":
                    redirect_hits.append(self.headers.get("Authorization"))
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"data":[]}')
                    return
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{self.server.server_port}/redirect-target",
                )
                self.end_headers()

            def log_message(self, _format, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/v1/models"
            with self.assertRaises(urllib.error.HTTPError) as raised:
                claude_code._read_proxy_json(
                    url, {"Authorization": "Bearer proxy-secret"}
                )
            self.assertEqual(raised.exception.code, 302)
            self.assertEqual(redirect_hits, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_online_proxy_keeps_empty_local_session_idle(self):
        with (
            mock.patch.object(
                claude_code,
                "_proxy_status",
                return_value={
                    "health": "online",
                    "data_source": "proxy-endpoint",
                },
            ),
            mock.patch.object(
                claude_code, "_read_usage", return_value=(12, 34, 1_800)
            ),
            mock.patch.object(
                claude_code, "_lifetime_stats", return_value=(0, 0, 0.0)
            ),
            mock.patch.object(claude_code, "_recent_transcripts", return_value=[]),
        ):
            status = claude_code.read_status()

        self.assertEqual(status["health"], "online")
        self.assertEqual(status["state"], "idle")
        self.assertEqual(status["active_count"], 0)
        self.assertEqual(status["usage_source"], "local-cache")

    def test_offline_proxy_clears_recent_transcript_rows(self):
        parsed = {
            "state": "running",
            "detail": "stale task",
            "project": "/tmp/stale",
            "updated_at": 100,
            "context_tokens": 50,
            "cache_hit_percent": 10,
            "last_assistant_response_at": 90,
            "model": "claude-sonnet-4-6",
        }
        with (
            mock.patch.object(
                claude_code,
                "_proxy_status",
                return_value={
                    "health": "offline",
                    "data_source": "proxy-endpoint",
                },
            ),
            mock.patch.object(
                claude_code, "_read_usage", return_value=(None, None, None)
            ),
            mock.patch.object(
                claude_code, "_lifetime_stats", return_value=(0, 0, 0.0)
            ),
            mock.patch.object(
                claude_code,
                "_recent_transcripts",
                return_value=[(Path("/tmp/stale"), 100)],
            ),
            mock.patch.object(claude_code, "_parse_session", return_value=parsed),
        ):
            status = claude_code.read_status()

        self.assertEqual(status["health"], "offline")
        self.assertEqual(status["state"], "no session")
        self.assertEqual(status["sessions"], [])
        self.assertEqual(status["active_count"], 0)


if __name__ == "__main__":
    unittest.main()
