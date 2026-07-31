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

    def test_steady_state_tick_does_not_retry_send_image(self):
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
        self.assertEqual(len(send_calls), 1)

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


if __name__ == "__main__":
    unittest.main()
