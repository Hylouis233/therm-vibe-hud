import threading
import time


class BackgroundCache:
    """Runs an expensive `compute()` on a timer in its own thread so the
    render/push-loop hot path (which must stay well under the LY-protocol
    firmware's ~2-3s no-frame timeout) never blocks on it. Callers just read
    whatever the last completed computation produced."""

    def __init__(self, compute, interval_sec):
        self._compute = compute
        self._interval_sec = interval_sec
        self._lock = threading.Lock()
        self._value = None
        self._thread = None

    def _worker(self):
        while True:
            try:
                value = self._compute()
            except Exception:
                value = None
            if value is not None:
                with self._lock:
                    self._value = value
            time.sleep(self._interval_sec)

    def get(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()
        with self._lock:
            return self._value
