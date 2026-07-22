import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from renderer.render import render  # noqa: E402
from sources import claude_code, codex_cli, zcode, hardware, history  # noqa: E402
from scripts import theme  # noqa: E402

TRCC_BIN = "/Applications/TRCC.app/Contents/MacOS/TRCC"
DEVICE_KEY = "0416:5408"
FRAME_PATH = ROOT / "frame.png"
# LY-protocol firmware (this Trofeo Vision 9.16) reverts to its built-in boot
# logo if it goes ~2-3s without a new frame, so this must stay well under that.
INTERVAL_SEC = 1.5
# Blank the panel after this long with no real keyboard/mouse input — mirrors
# a normal screensaver. Deliberately NOT tied to Claude Code/Codex/zcode
# session state: an actively-running agent still keeps its own transcript
# "thinking" while the human has walked away, which previously reset this
# timer every tick and meant the screen never blanked during a long session.
SCREEN_OFF_IDLE_SEC = 15 * 60
HID_IDLE_RE = __import__("re").compile(rb'"HIDIdleTime"\s*=\s*(\d+)')

READERS = (claude_code.read_status, codex_cli.read_status, zcode.read_status)
BACKGROUNDS_DIR = ROOT / "assets" / "backgrounds"

_idle_since = None
_screen_blanked = False
_session_background = None  # random pick for this process's lifetime, unless state.json pins one


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
    return env


def tick(state):
    global _screen_blanked

    idle_sec = _human_idle_sec()
    if idle_sec is not None and idle_sec > SCREEN_OFF_IDLE_SEC:
        if not _screen_blanked:
            print(f"[push_loop] {idle_sec:.0f}s with no HID input — blanking screen", file=sys.stderr)
        _screen_blanked = True
        result = subprocess.run(
            [TRCC_BIN, "display", "sleep", DEVICE_KEY],
            capture_output=True, text=True, env=_env(), timeout=20,
        )
        if result.returncode != 0:
            print(f"[push_loop] sleep failed: {result.stderr.strip()[-300:]}", file=sys.stderr)
        return

    if _screen_blanked:
        print("[push_loop] HID input detected — waking screen", file=sys.stderr)
    _screen_blanked = False

    statuses = []
    for read_status in READERS:
        try:
            statuses.append(read_status())
        except Exception as exc:
            statuses.append({"tool": "?", "state": "error", "detail": str(exc)[:60], "project": "", "updated_at": None})

    try:
        hw = hardware.read_status()
    except Exception:
        hw = {"tool": "Hardware", "cpu_temp": None, "cpu_usage": None, "mem_percent": None, "fan_rpm": None}

    try:
        history.record_all(statuses, hw)
    except Exception as exc:
        print(f"[push_loop] history record failed: {exc}", file=sys.stderr)

    img = render(statuses, hw, background=_current_background(state))
    img.save(FRAME_PATH)

    result = subprocess.run(
        [TRCC_BIN, "display", "send-image", DEVICE_KEY, str(FRAME_PATH)],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=20,
    )
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
