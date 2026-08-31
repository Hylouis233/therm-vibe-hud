import ctypes
import os
import random
import re
import signal
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

TRCC_BIN = os.environ.get(
    "THERM_VIBE_TRCC_BIN", "/Applications/TRCC.app/Contents/MacOS/TRCC"
)
TRCC_HELPER_DIR = ROOT / "scripts" / "trcc-bin"
USB_POWER_HELPER = ROOT / "scripts" / "trcc-usb-power"
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
# TRCC 9.9.11's LY keepalive path grows over long runs.  Bound it without
# interrupting a frame: inspect once a minute and recycle before it can become
# system-significant.  Suspending the panel also tears the daemon down.
DAEMON_MAX_RSS_MB = 512
DAEMON_HEALTH_CHECK_INTERVAL_SEC = 60
# The daemon can wedge itself into a state where its in-memory device registry
# still says "already connected" but the underlying USB handle is dead — every
# frame then fails with "send() called before connect()" forever, with no
# self-heal. Restarting the daemon (trcc kill + let the next CLI call respawn
# it) clears it. This kicks in only after this many consecutive frame
# failures, so transient single-tick hiccups never trigger a restart.
DAEMON_RESTART_FAIL_THRESHOLD = 5
DAEMON_RESTART_COOLDOWN_SEC = 30
DAEMON_KILL_GRACE_SEC = 5
PROVIDER_POLL_INTERVAL_SEC = 3
HARDWARE_POLL_INTERVAL_SEC = 2
# Blank the panel after this long with no real keyboard/mouse input — mirrors
# a normal screensaver. Deliberately NOT tied to Claude Code/Codex/Kimi/zcode
# session state: an actively-running agent still keeps its own transcript
# "thinking" while the human has walked away, which previously reset this
# timer every tick and meant the screen never blanked during a long session.
SCREEN_OFF_IDLE_SEC = 15 * 60
HID_IDLE_RE = re.compile(rb'"HIDIdleTime"\s*=\s*(\d+)')

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
_trcc_lock = threading.RLock()
_screen_sleep_event = threading.Event()
_daemon_ready_event = threading.Event()
_send_fail_streak = 0
_last_daemon_restart_at = 0.0
_last_daemon_health_check_at = 0.0
_panel_power_checked = False

try:
    _core_graphics = ctypes.CDLL(
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    )
    _cg_main_display_id = _core_graphics.CGMainDisplayID
    _cg_main_display_id.restype = ctypes.c_uint32
    _cg_display_is_asleep = _core_graphics.CGDisplayIsAsleep
    _cg_display_is_asleep.argtypes = [ctypes.c_uint32]
    _cg_display_is_asleep.restype = ctypes.c_bool
except (AttributeError, OSError):
    _cg_main_display_id = None
    _cg_display_is_asleep = None


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


def _display_is_asleep():
    """Return the actual macOS main-display sleep state when available."""
    if _cg_main_display_id is None or _cg_display_is_asleep is None:
        return False
    try:
        return bool(_cg_display_is_asleep(_cg_main_display_id()))
    except (OSError, ValueError):
        return False


def _screen_sleep_reason():
    if _display_is_asleep():
        return "main display asleep"
    idle_sec = _human_idle_sec()
    if idle_sec is not None and idle_sec > SCREEN_OFF_IDLE_SEC:
        return f"{idle_sec:.0f}s with no HID input"
    return None


def _env():
    import os

    env = os.environ.copy()
    env["SSL_CERT_FILE"] = "/etc/ssl/cert.pem"
    original_path = env.get("PATH")
    trcc_paths = os.pathsep.join((str(Path(TRCC_BIN).parent), str(TRCC_HELPER_DIR)))
    env["PATH"] = (
        f"{trcc_paths}{os.pathsep}{original_path}"
        if original_path
        else trcc_paths
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

    def read_unless_sleeping():
        if _screen_sleep_event.is_set() or not _daemon_ready_event.is_set():
            return None
        # The first successful frame starts the daemon before this reader is
        # allowed through, removing the autospawn race without putting a slow
        # sensor collection on the frame-delivery lock.
        return reader()

    with _cache_lock:
        if _hardware_cache is None or _hardware_reader is not reader:
            _hardware_reader = reader
            _hardware_cache = BackgroundCache(
                read_unless_sleeping, HARDWARE_POLL_INTERVAL_SEC
            )
        cache = _hardware_cache
    return cache.get() or {
        "tool": "Hardware",
        "cpu_temp": None,
        "cpu_usage": None,
        "mem_percent": None,
        "fan_rpm": None,
    }


def _run_trcc(*args, timeout=TRCC_COMMAND_TIMEOUT_SEC):
    with _trcc_lock:
        return subprocess.run(
            [TRCC_BIN, *args],
            capture_output=True,
            text=True,
            env=_env(),
            timeout=timeout,
        )


def _trcc_daemons():
    """Return exact TRCC daemon processes as ``(pid, rss_kib)`` pairs."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,rss=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    trcc_bin = Path(TRCC_BIN)
    direct_command = f"{TRCC_BIN} daemon"
    daemons = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        command = fields[2]
        module_suffix = " -m trcc daemon"
        python_command = (
            command[: -len(module_suffix)]
            if command.endswith(module_suffix)
            else ""
        )
        python_path = Path(python_command) if python_command else None
        module_command = (
            python_path is not None
            and python_path.parent == trcc_bin.parent
            and python_path.name.startswith("python")
        )
        script_suffix = f" {TRCC_BIN} daemon"
        script_python = (
            command[: -len(script_suffix)] if command.endswith(script_suffix) else ""
        )
        script_python_path = Path(script_python) if script_python else None
        script_command = (
            script_python_path is not None
            and script_python_path.parent == trcc_bin.parent
            and script_python_path.name.startswith("python")
        )
        if command != direct_command and not module_command and not script_command:
            continue
        try:
            daemons.append((int(fields[0]), int(fields[1])))
        except ValueError:
            continue
    return daemons


def _kill_trcc_daemon():
    """Stop every exact TRCC daemon and clear a stale IPC socket."""
    _daemon_ready_event.clear()
    with _trcc_lock:
        try:
            _run_trcc("kill", timeout=DAEMON_KILL_GRACE_SEC)
        except (OSError, subprocess.SubprocessError):
            pass

        deadline = time.monotonic() + DAEMON_KILL_GRACE_SEC
        survivors = _trcc_daemons()
        while survivors and time.monotonic() < deadline:
            time.sleep(0.2)
            survivors = _trcc_daemons()

        for pid, _rss_kib in survivors:
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass

        try:
            Path("/tmp/trcc.sock").unlink(missing_ok=True)
        except OSError:
            pass


def _maybe_restart_daemon(reason):
    global _last_daemon_restart_at
    now = time.monotonic()
    if now - _last_daemon_restart_at < DAEMON_RESTART_COOLDOWN_SEC:
        print("[push_loop] daemon restart on cool-down, skipping", file=sys.stderr)
        return False
    _last_daemon_restart_at = now
    print(f"[push_loop] recycling TRCC daemon: {reason}", file=sys.stderr)
    _kill_trcc_daemon()
    return True


def _maybe_recycle_daemon():
    """Bound the known 9.9.11 LY keepalive growth and remove duplicates."""
    global _last_daemon_health_check_at
    now = time.monotonic()
    if now - _last_daemon_health_check_at < DAEMON_HEALTH_CHECK_INTERVAL_SEC:
        return False
    _last_daemon_health_check_at = now

    daemons = _trcc_daemons()
    if len(daemons) > 1:
        return _maybe_restart_daemon(f"{len(daemons)} daemon processes detected")
    if daemons:
        _pid, rss_kib = daemons[0]
        rss_mb = rss_kib / 1024
        if rss_mb >= DAEMON_MAX_RSS_MB:
            return _maybe_restart_daemon(
                f"RSS {rss_mb:.0f} MiB reached {DAEMON_MAX_RSS_MB} MiB limit"
            )
    return False


def _run_power_helper(action):
    if not os.access(USB_POWER_HELPER, os.X_OK):
        return None
    try:
        with _trcc_lock:
            return subprocess.run(
                [str(USB_POWER_HELPER), action],
                capture_output=True,
                text=True,
                timeout=10,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(
            [str(USB_POWER_HELPER), action], 1, "", str(exc)
        )


def _panel_status_suspended():
    result = _run_power_helper("status")
    if result is None or result.returncode != 0:
        return None
    if "suspended=true" in result.stdout:
        return True
    if "suspended=false" in result.stdout:
        return False
    return None


def _suspend_panel(reason):
    """Blank once, release TRCC, then suspend only the target USB port."""
    global _panel_power_checked, _screen_blanked
    if _screen_blanked:
        return

    print(f"[push_loop] {reason} — suspending panel", file=sys.stderr)
    _screen_blanked = True
    _panel_power_checked = False
    _screen_sleep_event.set()
    _daemon_ready_event.clear()

    with _trcc_lock:
        try:
            result = _run_trcc("display", "sleep", DEVICE_KEY)
            if result.returncode != 0:
                print(
                    f"[push_loop] black-frame fallback failed: "
                    f"{result.stderr.strip()[-300:]}",
                    file=sys.stderr,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[push_loop] black-frame fallback failed: {exc}", file=sys.stderr)

        _kill_trcc_daemon()
        result = _run_power_helper("suspend")

    if result is None:
        print(
            "[push_loop] USB power helper missing — panel is black but backlight "
            "cannot be suspended",
            file=sys.stderr,
        )
    elif result.returncode != 0:
        print(
            f"[push_loop] USB suspend failed: {result.stderr.strip()[-300:]}",
            file=sys.stderr,
        )


def _resume_panel_if_needed(force=False):
    """Resume a panel left suspended by this or an earlier service process."""
    global _panel_power_checked, _screen_blanked
    suspended = True if force else _panel_status_suspended()
    if suspended:
        result = _run_power_helper("resume")
        if result is not None and result.returncode != 0:
            print(
                f"[push_loop] USB resume failed: {result.stderr.strip()[-300:]}",
                file=sys.stderr,
            )
            return False
    _screen_sleep_event.clear()
    _screen_blanked = False
    _panel_power_checked = True
    return True


def _send_image(retries=0, retry_delay=WAKE_SEND_IMAGE_RETRY_DELAY_SEC):
    attempt = 0
    while True:
        result = _run_trcc(
            "display", "send-image", DEVICE_KEY, str(FRAME_PATH)
        )
        if result.returncode == 0:
            _daemon_ready_event.set()
        if result.returncode == 0 or attempt >= retries:
            return result
        attempt += 1
        time.sleep(retry_delay)


def tick(state):
    global _panel_power_checked, _screen_blanked, _send_fail_streak

    sleep_reason = _screen_sleep_reason()
    if sleep_reason is not None:
        _suspend_panel(sleep_reason)
        return

    just_woke = _screen_blanked
    if just_woke:
        print("[push_loop] display/input wake — resuming panel", file=sys.stderr)
    if just_woke or not _panel_power_checked:
        if not _resume_panel_if_needed(force=just_woke):
            return

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
    if result.returncode == 0:
        _send_fail_streak = 0
        _maybe_recycle_daemon()
    else:
        _send_fail_streak += 1
        print(
            f"[push_loop] send-image failed (streak={_send_fail_streak}): "
            f"{result.stderr.strip()[-300:]}",
            file=sys.stderr,
        )
        if _send_fail_streak >= DAEMON_RESTART_FAIL_THRESHOLD:
            _maybe_restart_daemon("sustained frame failures")
            _send_fail_streak = 0


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
