import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRCC_BIN = "/Applications/TRCC.app/Contents/MacOS/TRCC"
DEVICE_KEY = "0416:5408"
STATE_PATH = ROOT / "state.json"
BACKGROUNDS_DIR = ROOT / "assets" / "backgrounds"

CATEGORIES = {"a": "Gallery", "b": "Tech", "c": "HUD", "d": "Light", "e": "Nature", "y": "Aesthetic"}


def _env():
    env = os.environ.copy()
    env["SSL_CERT_FILE"] = "/etc/ssl/cert.pem"
    return env


def _run(*args):
    return subprocess.run([TRCC_BIN, *args], capture_output=True, text=True, env=_env(), timeout=60)


def read_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"mode": "dashboard"}


def write_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))


def cmd_list(category=None):
    args = ["theme", "cloud-list"]
    if category:
        args += ["-c", category]
    result = _run(*args)
    print(result.stdout.strip() or result.stderr.strip())


def cmd_categories():
    for key, name in CATEGORIES.items():
        print(f"  {key}  {name}")


def cmd_set(target):
    state = read_state()
    background = state.get("background")

    if target == "dashboard":
        new_state = {"mode": "dashboard"}
        if background:
            new_state["background"] = background
        write_state(new_state)
        print("switched to: custom dashboard (Claude Code / Codex / zcode / Hardware)")
        return

    result = _run("theme", "cloud-load", DEVICE_KEY, target)
    if result.returncode != 0:
        print(f"failed to load theme {target}: {result.stderr.strip()[-400:]}", file=sys.stderr)
        sys.exit(1)
    write_state({"mode": "official", "theme_id": target})
    print(f"switched to official theme: {target}")


def cmd_current():
    print(json.dumps(read_state(), indent=2))


def _available_backgrounds():
    return sorted(p.stem for p in BACKGROUNDS_DIR.glob("*.png") if not p.stem.endswith("_raw"))


def cmd_bg(action, name=None):
    names = _available_backgrounds()
    state = read_state()

    if action == "list":
        current = state.get("background") or "(random each restart)"
        for n in names:
            marker = " *" if n == state.get("background") else ""
            print(f"  {n}{marker}")
        print(f"current: {current}")
        return

    if action == "off":
        state.pop("background", None)
        write_state(state)
        print("background: off (plain gradient, random ink-wash pick disabled)")
        return

    if action == "random":
        state.pop("background", None)
        write_state(state)
        print("background: random — picks a new ink-wash scene each push_loop restart")
        return

    if action == "set":
        if name not in names:
            print(f"unknown background '{name}'. available: {', '.join(names)}", file=sys.stderr)
            sys.exit(1)
        state["background"] = name
        write_state(state)
        print(f"background pinned to: {name}")
        return

    print("usage: theme.py bg list | set <name> | random | off", file=sys.stderr)
    sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print("usage: theme.py list [category] | categories | set <dashboard|theme_id> | current | bg list|set <name>|random|off")
        sys.exit(1)

    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "list":
        cmd_list(rest[0] if rest else None)
    elif cmd == "categories":
        cmd_categories()
    elif cmd == "set":
        if not rest:
            print("usage: theme.py set <dashboard|theme_id>", file=sys.stderr)
            sys.exit(1)
        cmd_set(rest[0])
    elif cmd == "current":
        cmd_current()
    elif cmd == "bg":
        if not rest:
            print("usage: theme.py bg list | set <name> | random | off", file=sys.stderr)
            sys.exit(1)
        cmd_bg(rest[0], rest[1] if len(rest) > 1 else None)
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
