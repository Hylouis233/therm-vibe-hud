import random
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from renderer.render import render  # noqa: E402
from sources import claude_code, codex_cli, kimi, minimax, zcode, hardware, history  # noqa: E402
from sources.background_cache import BackgroundCache  # noqa: E402
from scripts import theme  # noqa: E402

TRCC_BIN = "/Applications/TRCC.app/Contents/MacOS/TRCC"
TRCC_HELPER_DIR = ROOT / "scripts" / "trcc-bin"
DEVICE_KEY = "0416:5408"
FRAME_PATH = ROOT / "frame.png"
# LY-protocol firmware (this Trofeo Vision 9.16) reverts to its built-in boot
# logo if it goes ~2-3s without a new frame, so this must stay well under that.
INTERVAL_SEC = 1.5
TRCC_COMMAND_TIMEOUT_SEC = 30
# TRCC.app can lose the USB claim race against its own display-sleep
# handshake for the first moment after waking — only the wake-transition tick
# gets these extra attempts, so steady-state ticks never pay the retry cost.
WAKE_SEND_IMAGE_RETRIES = 3
WAKE_SEND_IMAGE_RETRY_DELAY_SEC = 0.4
PROVIDER_POLL_INTERVAL_SEC = 3
HARDWARE_POLL_INTERVAL_SEC = 2
# Blank the panel after this long with no real keyboard/mouse input — mirrors
# a normal screensaver. Deliberately NOT tied to Claude Code/Codex/Kimi/zcode
# session state: an actively-running agent still keeps its own transcript
# "thinking" while the human has walked away, which previously reset this
# timer every tick and meant the screen never blanked during a long session.
SCREEN_OFF_IDLE_SEC = 15 * 60
HID_IDLE_RE = __import__("re").compile(rb'"HIDIdleTime"\s*=\s*(\d+)')

READERS = (
    claude_code.read_status,
    codex_cli.read_status,
    kimi.read_status,
    zcode.read_status,
    minimax.read_status,
)
READER_NAMES = (
    "Claude Code",
    "Codex",
    "Kimi Code",
    "zcode",
    "MiniMax",
)
BACKGROUNDS_DIR = ROOT / "assets" / "backgrounds"

_idle_since = None
_screen_blanked = False
_session_background = None  # random pick for this process's lifetime, unless state.json pins one
_reader_caches = {}
_hardware_reader = None
_hardware_cache = None
_cache_lock = threading.Lock()


def _current_background(state):
    global _session_background
    if "background" in state:
        return state["background"]
    if _session_background is None:
        choices = sorted(p.stem for p in BACKGROUNDS_DIR.glob("*.png") if not p.stem.endswith("_raw"))
        _session_background = random.choice(choices) if choices else None
    return _session_background


def _human_idle_sec():
    """Real HID idle time (mouse/keyboard), same signal macOS's own
    screensaver/display-sleep uses — independent of any tool's session state."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, timeout=5).stdout
        m = HID_IDLE_RE.search(out)
        return int(m.group(1)) / 1e9 if m else None
    except (subprocess.SubprocessError, OSError):
        return None


def _env():
    import os

    env = os.environ.copy()
    env["SSL_CERT_FILE"] = "/etc/ssl/cert.pem"
    original_path = env.get("PATH")
    env["PATH"] = (
        f"{TRCC_HELPER_DIR}{os.pathsep}{original_path}"
        if original_path
        else str(TRCC_HELPER_DIR)
    )
    # One long-lived TRCC daemon owns USB and serializes sensor reads, new
    # frames, and the LY firmware's intrinsic 150ms frame keepalive.  Without
    # this flag each CLI call reconnects and handshakes the device.
    env["TRCC_DAEMON"] = "1"
    return env


def _provider_placeholder(index, read_status):
    if len(READERS) == len(READER_NAMES):
        tool = READER_NAMES[index]
    else:
        tool = getattr(read_status, "__name__", "?").replace("_", " ")
    placeholder = {
        "tool": tool,
        "state": "loading",
        "detail": "background refresh",
        "project": "",
        "updated_at": None,
        "sessions": [],
        "active_count": 0,
    }
    if tool == "zcode":
        placeholder["display_name"] = "GLM"
    return placeholder


def _cached_statuses():
    statuses = []
    with _cache_lock:
        active_readers = set(READERS)
        for old_reader in tuple(_reader_caches):
            if old_reader not in active_readers:
                del _reader_caches[old_reader]
        for read_status in READERS:
            if read_status not in _reader_caches:
                _reader_caches[read_status] = BackgroundCache(
                    read_status, PROVIDER_POLL_INTERVAL_SEC
                )
        caches = [_reader_caches[read_status] for read_status in READERS]

    for index, (read_status, cache) in enumerate(zip(READERS, caches)):
        statuses.append(cache.get() or _provider_placeholder(index, read_status))
    return statuses


def _cached_hardware():
    global _hardware_cache, _hardware_reader
    reader = hardware.read_status
    with _cache_lock:
        if _hardware_cache is None or _hardware_reader is not reader:
            _hardware_reader = reader
            _hardware_cache = BackgroundCache(reader, HARDWARE_POLL_INTERVAL_SEC)
        cache = _hardware_cache
    return cache.get() or {
        "tool": "Hardware",
        "cpu_temp": None,
        "cpu_usage": None,
        "mem_percent": None,
        "fan_rpm": None,
    }


def _send_image(retries=0, retry_delay=WAKE_SEND_IMAGE_RETRY_DELAY_SEC):
    attempt = 0
    while True:
        result = subprocess.run(
            [TRCC_BIN, "display", "send-image", DEVICE_KEY, str(FRAME_PATH)],
            capture_output=True,
            text=True,
            env=_env(),
            timeout=TRCC_COMMAND_TIMEOUT_SEC,
        )
        if result.returncode == 0 or attempt >= retries:
            return result
        attempt += 1
        time.sleep(retry_delay)


def tick(state):
    global _screen_blanked

    idle_sec = _human_idle_sec()
    if idle_sec is not None and idle_sec > SCREEN_OFF_IDLE_SEC:
        if not _screen_blanked:
            print(f"[push_loop] {idle_sec:.0f}s with no HID input — blanking screen", file=sys.stderr)
        _screen_blanked = True
        result = subprocess.run(
            [TRCC_BIN, "display", "sleep", DEVICE_KEY],
            capture_output=True,
            text=True,
            env=_env(),
            timeout=TRCC_COMMAND_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            print(f"[push_loop] sleep failed: {result.stderr.strip()[-300:]}", file=sys.stderr)
        return

    just_woke = _screen_blanked
    if just_woke:
        print("[push_loop] HID input detected — waking screen", file=sys.stderr)
    _screen_blanked = False

    # Provider/session scans and sensor subprocesses are deliberately off the
    # frame hot path.  A 30s endpoint timeout may delay a refresh, but cannot
    # delay display delivery; the last successful snapshot stays visible.
    statuses = _cached_statuses()
    hw = _cached_hardware()

    try:
        history.record_all(statuses, hw)
    except Exception as exc:
        print(f"[push_loop] history record failed: {exc}", file=sys.stderr)

    img = render(statuses, hw, background=_current_background(state))
    img.save(FRAME_PATH)

    result = _send_image(retries=WAKE_SEND_IMAGE_RETRIES if just_woke else 0)
    if result.returncode != 0:
        print(f"[push_loop] send-image failed: {result.stderr.strip()[-300:]}", file=sys.stderr)


def _run_official_theme(theme_id):
    print(f"[push_loop] official theme mode: {theme_id}")
    proc = subprocess.Popen([TRCC_BIN, "display", "play", DEVICE_KEY], env=_env())
    try:
        while True:
            time.sleep(1.0)
            state = theme.read_state()
            if state.get("mode") != "official" or state.get("theme_id") != theme_id:
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("[push_loop] left official theme mode")


def main():
    print(f"[push_loop] pushing to {DEVICE_KEY} every {INTERVAL_SEC}s — Ctrl-C to stop")
    while True:
        state = theme.read_state()
        if state.get("mode") == "official" and state.get("theme_id"):
            try:
                _run_official_theme(state["theme_id"])
            except Exception as exc:
                print(f"[push_loop] official theme run failed: {exc}", file=sys.stderr)
                time.sleep(2.0)
            continue

        start = time.monotonic()
        try:
            tick(state)
        except Exception as exc:
            print(f"[push_loop] tick failed: {exc}", file=sys.stderr)
        elapsed = time.monotonic() - start
        time.sleep(max(0.0, INTERVAL_SEC - elapsed))


if __name__ == "__main__":
    main()
