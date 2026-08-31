import os
import unittest
from pathlib import Path

from scripts import push_loop, theme
from sources import claude_code, codex_cli, hardware, kimi, minimax, zcode


class TimeoutPolicyTests(unittest.TestCase):
    def test_remote_status_windows_allow_slow_but_valid_responses(self):
        self.assertEqual(claude_code.PROXY_STATUS_TIMEOUT_SEC, 30)
        self.assertEqual(codex_cli.LIVE_QUOTA_FETCH_TIMEOUT_SEC, 30)
        self.assertEqual(kimi.USAGE_REQUEST_TIMEOUT_SEC, 30)
        self.assertEqual(kimi.OAUTH_REQUEST_TIMEOUT_SEC, 30)
        self.assertEqual(zcode.LIVE_STATUS_TIMEOUT_SEC, 30)
        self.assertEqual(minimax.ENDPOINT_TIMEOUT_SEC, 30)

    def test_local_codex_app_server_gets_a_larger_bounded_window(self):
        self.assertEqual(codex_cli.APP_SERVER_TIMEOUT_SEC, 3)

    def test_trcc_calls_share_the_daemon_and_thirty_second_window(self):
        self.assertEqual(push_loop.TRCC_COMMAND_TIMEOUT_SEC, 30)
        self.assertEqual(hardware.TRCC_INFO_TIMEOUT_SEC, 30)
        self.assertEqual(push_loop._env()["TRCC_DAEMON"], "1")
        self.assertEqual(hardware._env()["TRCC_DAEMON"], "1")
        self.assertEqual(theme._env()["TRCC_DAEMON"], "1")
        for module in (push_loop, hardware, theme):
            path_entries = module._env()["PATH"].split(os.pathsep)
            self.assertEqual(Path(path_entries[0]), Path(module.TRCC_BIN).parent)
            self.assertEqual(Path(path_entries[1]), push_loop.TRCC_HELPER_DIR)
        self.assertTrue(os.access(push_loop.TRCC_HELPER_DIR / "trcc", os.X_OK))


if __name__ == "__main__":
    unittest.main()
