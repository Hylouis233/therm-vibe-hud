import subprocess
import threading
import time
import unittest
from contextlib import ExitStack
from unittest import mock

from scripts import push_loop


class _FakeImage:
    def save(self, path):
        self.saved_path = path


class PushLoopTests(unittest.TestCase):
    def setUp(self):
        self._display_sleep_patch = mock.patch.object(
            push_loop, "_display_is_asleep", return_value=False
        )
        self._display_sleep_patch.start()
        push_loop._panel_power_checked = True
        push_loop._screen_sleep_event.clear()
        push_loop._daemon_ready_event.set()
        push_loop._screen_blanked = False
        push_loop._send_fail_streak = 0
        push_loop._last_daemon_health_check_at = time.monotonic()
        push_loop._last_usb_suspend_at = 0.0

    def tearDown(self):
        push_loop._screen_sleep_event.clear()
        push_loop._daemon_ready_event.clear()
        self._display_sleep_patch.stop()

    def _cache_state_patches(self):
        return (
            mock.patch.object(push_loop, "_reader_caches", {}, create=True),
            mock.patch.object(push_loop, "_hardware_cache", None, create=True),
            mock.patch.object(push_loop, "_hardware_reader", None, create=True),
        )

    def test_tick_does_not_wait_for_slow_status_or_hardware_readers(self):
        slow_reads_started = threading.Event()
        release_slow_reads = threading.Event()
        frame_sent = threading.Event()

        def slow_status():
            slow_reads_started.set()
            release_slow_reads.wait()
            return {"tool": "Slow", "state": "idle"}

        def slow_hardware():
            slow_reads_started.set()
            release_slow_reads.wait()
            return {"tool": "Hardware", "cpu_temp": 42}

        def fake_run(args, **kwargs):
            if "send-image" in args:
                frame_sent.set()
            return subprocess.CompletedProcess(args, 0, "", "")

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(push_loop, "READERS", (slow_status,)))
            stack.enter_context(
                mock.patch.object(push_loop.hardware, "read_status", slow_hardware)
            )
            stack.enter_context(
                mock.patch.object(push_loop, "_human_idle_sec", return_value=0)
            )
            stack.enter_context(
                mock.patch.object(push_loop, "_current_background", return_value=None)
            )
            stack.enter_context(
                mock.patch.object(push_loop, "render", return_value=_FakeImage())
            )
            stack.enter_context(
                mock.patch.object(push_loop.subprocess, "run", side_effect=fake_run)
            )
            for cache_patch in self._cache_state_patches():
                stack.enter_context(cache_patch)
            tick_thread = threading.Thread(target=push_loop.tick, args=({},))
            tick_thread.start()
            try:
                self.assertTrue(slow_reads_started.wait(1))
                self.assertTrue(
                    frame_sent.wait(0.25),
                    "frame delivery waited for a slow status reader",
                )
            finally:
                release_slow_reads.set()
                tick_thread.join(1)

    def test_tick_uses_completed_background_values(self):
        provider_value = {"tool": "Test", "state": "running", "updated_at": 1}
        hardware_value = {"tool": "Hardware", "cpu_temp": 42}
        provider_done = threading.Event()
        hardware_done = threading.Event()

        def read_provider():
            provider_done.set()
            return provider_value

        def read_hardware():
            hardware_done.set()
            return hardware_value

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(push_loop, "READERS", (read_provider,))
            )
            stack.enter_context(
                mock.patch.object(push_loop.hardware, "read_status", read_hardware)
            )
            stack.enter_context(
                mock.patch.object(push_loop, "_human_idle_sec", return_value=0)
            )
            stack.enter_context(
                mock.patch.object(push_loop, "_current_background", return_value=None)
            )
            render_mock = stack.enter_context(
                mock.patch.object(push_loop, "render", return_value=_FakeImage())
            )
            stack.enter_context(
                mock.patch.object(
                    push_loop.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                )
            )
            for cache_patch in self._cache_state_patches():
                stack.enter_context(cache_patch)
            push_loop.tick({})
            self.assertTrue(provider_done.wait(1))
            self.assertTrue(hardware_done.wait(1))

            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                push_loop.tick({})
                statuses, hardware = render_mock.call_args.args[:2]
                if statuses == [provider_value] and hardware == hardware_value:
                    break
                time.sleep(0.01)

            self.assertEqual(statuses, [provider_value])
            self.assertEqual(hardware, hardware_value)

    def test_wake_transition_retries_send_image_until_success(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            if "send-image" in args and len(
                [c for c in calls if "send-image" in c]
            ) < 3:
                return subprocess.CompletedProcess(args, 1, "", "still claiming device")
            return subprocess.CompletedProcess(args, 0, "", "")

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    push_loop,
                    "READERS",
                    (lambda: {"tool": "Test", "state": "idle"},),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    push_loop.hardware,
                    "read_status",
                    return_value={"tool": "Hardware"},
                )
            )
            stack.enter_context(
                mock.patch.object(push_loop, "_human_idle_sec", return_value=0)
            )
            stack.enter_context(
                mock.patch.object(push_loop, "_current_background", return_value=None)
            )
            stack.enter_context(
                mock.patch.object(push_loop, "render", return_value=_FakeImage())
            )
            stack.enter_context(
                mock.patch.object(push_loop.subprocess, "run", side_effect=fake_run)
            )
            stack.enter_context(mock.patch.object(push_loop.time, "sleep"))
            stack.enter_context(
                mock.patch.object(push_loop, "_screen_blanked", True, create=True)
            )
            for cache_patch in self._cache_state_patches():
                stack.enter_context(cache_patch)
            push_loop.tick({})

        send_calls = [c for c in calls if "send-image" in c]
        self.assertEqual(len(send_calls), 3)
        self.assertFalse(push_loop._screen_blanked)

    def test_steady_state_tick_retries_send_image_up_to_twice(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1, "", "transient failure")

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    push_loop,
                    "READERS",
                    (lambda: {"tool": "Test", "state": "idle"},),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    push_loop.hardware,
                    "read_status",
                    return_value={"tool": "Hardware"},
                )
            )
            stack.enter_context(
                mock.patch.object(push_loop, "_human_idle_sec", return_value=0)
            )
            stack.enter_context(
                mock.patch.object(push_loop, "_current_background", return_value=None)
            )
            stack.enter_context(
                mock.patch.object(push_loop, "render", return_value=_FakeImage())
            )
            stack.enter_context(
                mock.patch.object(push_loop.subprocess, "run", side_effect=fake_run)
            )
            stack.enter_context(mock.patch.object(push_loop.time, "sleep"))
            stack.enter_context(
                mock.patch.object(push_loop, "_screen_blanked", False, create=True)
            )
            for cache_patch in self._cache_state_patches():
                stack.enter_context(cache_patch)
            push_loop.tick({})

        send_calls = [c for c in calls if "send-image" in c]
        self.assertEqual(len(send_calls), 3)

    def test_sustained_usb_claim_denial_force_clears_daemons(self):
        push_loop._usb_claim_fail_streak = 0
        push_loop._usb_claim_recovery_at = 0.0

        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(
                args, 1, "", "USBError: [Errno 13] Access denied"
            )

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    push_loop,
                    "READERS",
                    (lambda: {"tool": "Test", "state": "idle"},),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    push_loop.hardware,
                    "read_status",
                    return_value={"tool": "Hardware"},
                )
            )
            stack.enter_context(
                mock.patch.object(push_loop, "_human_idle_sec", return_value=0)
            )
            stack.enter_context(
                mock.patch.object(push_loop, "_current_background", return_value=None)
            )
            stack.enter_context(
                mock.patch.object(push_loop, "render", return_value=_FakeImage())
            )
            stack.enter_context(
                mock.patch.object(push_loop.subprocess, "run", side_effect=fake_run)
            )
            stack.enter_context(mock.patch.object(push_loop.time, "sleep"))
            stack.enter_context(
                mock.patch.object(push_loop, "_screen_blanked", False, create=True)
            )
            kill_daemon = stack.enter_context(
                mock.patch.object(push_loop, "_kill_trcc_daemon")
            )
            stack.enter_context(
                mock.patch.object(
                    push_loop,
                    "_run_power_helper",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                )
            )
            for _ in range(3):
                push_loop.tick({})

            kill_daemon.assert_called_once_with(force=True)

            # Cooldown window: another denial streak must not force-clear
            # again until USB_CLAIM_RECOVERY_COOLDOWN_SEC has elapsed.
            for _ in range(3):
                push_loop.tick({})
            kill_daemon.assert_called_once_with(force=True)

        # Streak kept counting through the cooldown so the first failure
        # after it expires triggers recovery immediately.
        self.assertEqual(push_loop._usb_claim_fail_streak, 3)

    def test_trcc_commands_use_daemon_and_widened_timeout(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, "", "")

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    push_loop,
                    "READERS",
                    (lambda: {"tool": "Test", "state": "idle"},),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    push_loop.hardware,
                    "read_status",
                    return_value={"tool": "Hardware"},
                )
            )
            stack.enter_context(
                mock.patch.object(push_loop, "_human_idle_sec", return_value=0)
            )
            stack.enter_context(
                mock.patch.object(push_loop, "_current_background", return_value=None)
            )
            stack.enter_context(
                mock.patch.object(push_loop, "render", return_value=_FakeImage())
            )
            stack.enter_context(
                mock.patch.object(push_loop.subprocess, "run", side_effect=fake_run)
            )
            for cache_patch in self._cache_state_patches():
                stack.enter_context(cache_patch)
            push_loop.tick({})

        send_call = next(call for call in calls if "send-image" in call[0])
        self.assertEqual(send_call[1]["env"]["TRCC_DAEMON"], "1")
        self.assertEqual(send_call[1]["timeout"], 30)

    def test_display_sleep_takes_precedence_over_idle_fallback(self):
        with mock.patch.object(push_loop, "_display_is_asleep", return_value=True):
            with mock.patch.object(push_loop, "_human_idle_sec") as idle_mock:
                self.assertEqual(push_loop._screen_sleep_reason(), "main display asleep")
        idle_mock.assert_not_called()

    def test_suspend_panel_kills_and_suspends_without_display_sleep(self):
        completed = subprocess.CompletedProcess([], 0, "suspended=true", "")
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(push_loop, "_usb_helper_available", return_value=True)
            )
            run_trcc = stack.enter_context(
                mock.patch.object(push_loop, "_run_trcc", return_value=completed)
            )
            kill_daemon = stack.enter_context(
                mock.patch.object(push_loop, "_kill_trcc_daemon")
            )
            power = stack.enter_context(
                mock.patch.object(
                    push_loop, "_run_power_helper", return_value=completed
                )
            )
            push_loop._suspend_panel("main display asleep")
            push_loop._suspend_panel("main display asleep")

        # Initial suspend deliberately pushes a black frame before killing
        # the daemon, so a failed helper seizure can't leave bright content.
        run_trcc.assert_called_once_with("display", "sleep", "0416:5408")
        kill_daemon.assert_called_once_with(force=True)
        power.assert_called_once_with("suspend")
        self.assertTrue(push_loop._screen_blanked)
        self.assertTrue(push_loop._screen_sleep_event.is_set())

    def test_failed_idle_query_does_not_resume_blanked_panel(self):
        push_loop._screen_blanked = True
        push_loop._screen_sleep_event.set()
        push_loop._last_usb_suspend_at = time.monotonic()
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(push_loop, "_human_idle_sec", return_value=None)
            )
            resume = stack.enter_context(
                mock.patch.object(push_loop, "_resume_panel_if_needed")
            )
            suspend = stack.enter_context(
                mock.patch.object(push_loop, "_suspend_panel")
            )
            send = stack.enter_context(mock.patch.object(push_loop, "_send_image"))
            push_loop.tick({})

        resume.assert_not_called()
        send.assert_not_called()
        suspend.assert_called_once_with("idle query inconclusive")
        self.assertTrue(push_loop._screen_blanked)

    def test_recent_input_resumes_blanked_panel(self):
        push_loop._screen_blanked = True
        push_loop._screen_sleep_event.set()
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    push_loop,
                    "READERS",
                    (lambda: {"tool": "Test", "state": "idle"},),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    push_loop.hardware,
                    "read_status",
                    return_value={"tool": "Hardware"},
                )
            )
            stack.enter_context(
                mock.patch.object(push_loop, "_human_idle_sec", return_value=0.2)
            )
            stack.enter_context(
                mock.patch.object(push_loop, "_current_background", return_value=None)
            )
            stack.enter_context(
                mock.patch.object(push_loop, "render", return_value=_FakeImage())
            )
            stack.enter_context(
                mock.patch.object(
                    push_loop.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                )
            )
            resume = stack.enter_context(
                mock.patch.object(
                    push_loop, "_resume_panel_if_needed", return_value=True
                )
            )
            for cache_patch in self._cache_state_patches():
                stack.enter_context(cache_patch)
            push_loop.tick({})

        resume.assert_called_once_with(force=True)

    def test_suspend_reasserts_when_usb_not_suspended(self):
        completed = subprocess.CompletedProcess([], 0, "suspended=true", "")
        push_loop._screen_blanked = True
        push_loop._screen_sleep_event.set()
        push_loop._last_usb_suspend_at = 0.0
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(push_loop, "_usb_helper_available", return_value=True)
            )
            stack.enter_context(
                mock.patch.object(
                    push_loop, "_panel_status_suspended", return_value=False
                )
            )
            stack.enter_context(
                mock.patch.object(push_loop, "_trcc_daemons", return_value=[])
            )
            kill_daemon = stack.enter_context(
                mock.patch.object(push_loop, "_kill_trcc_daemon")
            )
            power = stack.enter_context(
                mock.patch.object(
                    push_loop, "_run_power_helper", return_value=completed
                )
            )
            push_loop._suspend_panel("901s with no HID input")

        kill_daemon.assert_not_called()
        power.assert_called_once_with("suspend")
        self.assertTrue(push_loop._screen_blanked)

    def test_resume_panel_recovers_state_left_by_previous_process(self):
        completed = subprocess.CompletedProcess([], 0, "suspended=false", "")
        push_loop._screen_blanked = True
        push_loop._screen_sleep_event.set()
        with mock.patch.object(
            push_loop, "_panel_status_suspended", return_value=True
        ):
            with mock.patch.object(
                push_loop, "_run_power_helper", return_value=completed
            ) as power:
                self.assertTrue(push_loop._resume_panel_if_needed())

        power.assert_called_once_with("resume")
        self.assertFalse(push_loop._screen_blanked)
        self.assertFalse(push_loop._screen_sleep_event.is_set())
        self.assertTrue(push_loop._panel_power_checked)

    def test_daemon_scan_ignores_shells_that_only_mention_trcc_daemon(self):
        listing = "\n".join(
            [
                " 123 400000 /Applications/TRCC.app/Contents/MacOS/TRCC daemon",
                # The vendor client spawns daemons with a lowercase argv0
                # copy of the bundle path (APFS is case-insensitive).
                " 999 50000 /Applications/TRCC.app/Contents/MacOS/trcc daemon",
                " 456 1000 /bin/zsh -c pgrep -f 'TRCC daemon'",
                " 789 2000 /Applications/TRCC.app/Contents/MacOS/TRCC status",
            ]
        )
        result = subprocess.CompletedProcess([], 0, listing, "")
        with mock.patch.object(push_loop.subprocess, "run", return_value=result):
            self.assertEqual(
                push_loop._trcc_daemons(), [(123, 400000), (999, 50000)]
            )

    def test_daemon_scan_accepts_configured_python_module_runtime(self):
        trcc_bin = "/opt/Therm Vibe/trcc-venv/bin/trcc"
        listing = (
            " 321 120000 /opt/Therm Vibe/trcc-venv/bin/python3.12 "
            "-m trcc daemon"
        )
        result = subprocess.CompletedProcess([], 0, listing, "")
        with mock.patch.object(push_loop, "TRCC_BIN", trcc_bin):
            with mock.patch.object(push_loop.subprocess, "run", return_value=result):
                self.assertEqual(push_loop._trcc_daemons(), [(321, 120000)])

    def test_daemon_scan_accepts_configured_console_script_runtime(self):
        trcc_bin = "/opt/Therm Vibe/trcc-venv/bin/trcc"
        listing = (
            " 654 130000 /opt/Therm Vibe/trcc-venv/bin/python "
            "/opt/Therm Vibe/trcc-venv/bin/trcc daemon"
        )
        result = subprocess.CompletedProcess([], 0, listing, "")
        with mock.patch.object(push_loop, "TRCC_BIN", trcc_bin):
            with mock.patch.object(push_loop.subprocess, "run", return_value=result):
                self.assertEqual(push_loop._trcc_daemons(), [(654, 130000)])

    def test_memory_guard_recycles_daemon_at_rss_limit(self):
        push_loop._last_daemon_health_check_at = 0
        with mock.patch.object(
            push_loop,
            "_trcc_daemons",
            return_value=[(123, push_loop.DAEMON_MAX_RSS_MB * 1024)],
        ):
            with mock.patch.object(
                push_loop, "_maybe_restart_daemon", return_value=True
            ) as restart:
                self.assertTrue(push_loop._maybe_recycle_daemon())

        restart.assert_called_once()
        self.assertIn("RSS", restart.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
