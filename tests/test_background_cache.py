import threading
import time
import unittest

from sources.background_cache import BackgroundCache


class BackgroundCacheTests(unittest.TestCase):
    def test_first_get_returns_while_compute_is_still_blocked(self):
        compute_started = threading.Event()
        release_compute = threading.Event()

        def compute():
            compute_started.set()
            release_compute.wait()
            return {"state": "ready"}

        cache = BackgroundCache(compute, interval_sec=3600)
        started_at = time.monotonic()
        try:
            self.assertIsNone(cache.get())
            self.assertTrue(compute_started.wait(1))
            self.assertLess(time.monotonic() - started_at, 0.25)
        finally:
            release_compute.set()

    def test_last_good_value_survives_a_later_exception(self):
        calls = 0
        second_call_finished = threading.Event()

        def compute():
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"state": "ready"}
            second_call_finished.set()
            raise RuntimeError("temporary failure")

        cache = BackgroundCache(compute, interval_sec=0.01)
        first_value = cache.get()
        self.assertIn(first_value, (None, {"state": "ready"}))

        deadline = time.monotonic() + 1
        while cache.get() is None and time.monotonic() < deadline:
            time.sleep(0.005)

        self.assertEqual(cache.get(), {"state": "ready"})
        self.assertTrue(second_call_finished.wait(1))
        self.assertEqual(cache.get(), {"state": "ready"})

    def test_concurrent_gets_start_only_one_worker(self):
        caller_count = 24
        callers_ready = threading.Barrier(caller_count + 1)
        release_callers = threading.Event()
        release_compute = threading.Event()
        compute_started = threading.Event()
        compute_calls = 0
        compute_lock = threading.Lock()

        def compute():
            nonlocal compute_calls
            with compute_lock:
                compute_calls += 1
                compute_started.set()
            release_compute.wait()
            return {"state": "ready"}

        def get_cached_value():
            callers_ready.wait()
            release_callers.wait()
            cache.get()

        cache = BackgroundCache(compute, interval_sec=3600)
        callers = [
            threading.Thread(target=get_cached_value) for _ in range(caller_count)
        ]
        for caller in callers:
            caller.start()

        callers_ready.wait()
        release_callers.set()
        try:
            self.assertTrue(compute_started.wait(1))
            for caller in callers:
                caller.join(1)
            self.assertTrue(all(not caller.is_alive() for caller in callers))
            self.assertEqual(compute_calls, 1)
        finally:
            release_compute.set()


if __name__ == "__main__":
    unittest.main()
