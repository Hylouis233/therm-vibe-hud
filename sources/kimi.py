import contextlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

KIMI_HOME = Path.home() / ".kimi-code"
SESSION_INDEX_PATH = KIMI_HOME / "session_index.jsonl"
CREDENTIALS_PATH = KIMI_HOME / "credentials" / "kimi-code.json"
DESKTOP_LOG_PATH = Path.home() / "Library" / "Logs" / "kimi-desktop" / "main.log"
DESKTOP_DAIMON_ROOT = (
    Path.home()
    / "Library"
    / "Application Support"
    / "kimi-desktop"
    / "daimon-share"
    / "daimon"
)
DESKTOP_RUNNER_STATE_PATH = DESKTOP_DAIMON_ROOT / "agents" / "main" / "runner.state.json"

USAGE_URL = "https://api.kimi.com/coding/v1/usages"
OAUTH_TOKEN_URL = "https://auth.kimi.com/api/oauth/token"
OAUTH_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"

USAGE_CACHE_SEC = 60
USAGE_REQUEST_TIMEOUT_SEC = 30
OAUTH_REQUEST_TIMEOUT_SEC = 30
TOKEN_REFRESH_MARGIN_SEC = 60
ACTIVE_WINDOW_SEC = 30 * 60
IDLE_THRESHOLD_SEC = 90
MAX_SESSIONS = 6
TAIL_BYTES = 512 * 1024
# Escalating tail sizes for the desktop log — see _read_desktop_subscription.
DESKTOP_LOG_SCAN_BYTES = (128 * 1024, 512 * 1024, 4 * 1024 * 1024)

STATE_PRIORITY = {"running": 2, "thinking": 1, "idle": 0}
ACTIVE_EVENT_TYPES = {
    "turn.started",
    "turn.step.started",
    "turn.step.completed",
    "tool.call.started",
    "tool.call.updated",
}
THINKING_EVENT_TYPES = {"turn.step.started", "turn.step.completed"}

_DESKTOP_SUB_RE = re.compile(
    r"refreshed\(sub\):.*?\blevel=(?P<level>\d+)"
    r".*?\bisMember=(?P<member>true|false)"
    r".*?\bomniRatio=(?P<ratio>[0-9.]+)"
    r".*?\bexhausted=(?P<exhausted>true|false)"
    r".*?\bresetAt=(?P<reset>\S+)"
)
_DESKTOP_MEMBERSHIP_RE = re.compile(
    r"\bmembershipLevel=(?P<plan>[A-Za-z][A-Za-z0-9_-]*)"
)

_usage_cache_at = 0.0
_usage_cache = {}
_last_good_usage = {}
# Log size at the last completed subscription scan, and that scan's result —
# see _read_desktop_subscription.
_subscription_scan_key = None
_subscription_scan_result = {}


def _read_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _tail_lines(path, max_bytes=TAIL_BYTES):
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()
            return handle.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return []


def _percent(used, limit):
    try:
        used_f, limit_f = float(used), float(limit)
    except (TypeError, ValueError):
        return None
    if limit_f <= 0:
        return None
    return max(0.0, min(100.0, used_f / limit_f * 100.0))


def _is_five_hour_window(item):
    window = item.get("window") if isinstance(item, dict) else None
    if not isinstance(window, dict):
        return False
    try:
        duration = int(window.get("duration"))
    except (TypeError, ValueError):
        return False
    unit = window.get("timeUnit")
    return (unit == "TIME_UNIT_MINUTE" and duration == 300) or (
        unit == "TIME_UNIT_HOUR" and duration == 5
    )


def _parse_usage_payload(payload):
    if not isinstance(payload, dict):
        return {}

    summary = payload.get("usage")
    summary = summary if isinstance(summary, dict) else {}
    five = None
    for item in payload.get("limits") or []:
        if isinstance(item, dict) and _is_five_hour_window(item):
            detail = item.get("detail")
            if isinstance(detail, dict):
                five = detail
                break
    five = five or {}

    weekly_percent = _percent(summary.get("used"), summary.get("limit"))
    five_hour_percent = _percent(five.get("used"), five.get("limit"))
    if (
        five_hour_percent is None
        and five.get("used") is None
        and weekly_percent is not None
        and weekly_percent >= 100.0
    ):
        # Once the weekly quota is exhausted, the API stops reporting the
        # five-hour window's `used` field at all (instead of reporting it at
        # cap) — weekly exhaustion implies the 5h window is exhausted too, so
        # infer 100% rather than showing "no usage data" for a window that's
        # actually just as capped as its parent.
        five_hour_percent = 100.0

    result = {
        "kimi_weekly_percent": weekly_percent,
        "kimi_weekly_resets_at": summary.get("resetTime"),
        "kimi_weekly_used": summary.get("used"),
        "kimi_weekly_limit": summary.get("limit"),
        "kimi_five_hour_percent": five_hour_percent,
        "kimi_five_hour_resets_at": five.get("resetTime"),
        "kimi_five_hour_used": five.get("used"),
        "kimi_five_hour_limit": five.get("limit"),
    }
    user = payload.get("user")
    membership = user.get("membership") if isinstance(user, dict) else None
    level = membership.get("level") if isinstance(membership, dict) else None
    if isinstance(level, str) and level.strip():
        # LEVEL_STANDARD is an internal API enum, not the product name shown
        # to the user. Preserve it for diagnostics without putting it in the
        # HUD's plan caption.
        result["kimi_membership_level_raw"] = level.strip()
    return result


def _parse_desktop_subscription_line(line):
    match = _DESKTOP_SUB_RE.search(line)
    if not match:
        return {}
    level = int(match.group("level"))
    try:
        monthly_percent = max(0.0, min(100.0, float(match.group("ratio")) * 100.0))
    except ValueError:
        monthly_percent = None
    is_member = match.group("member") == "true"
    result = {
        "kimi_monthly_percent": monthly_percent,
        "kimi_monthly_resets_at": match.group("reset"),
        "kimi_is_member": is_member,
        "kimi_subscription_exhausted": match.group("exhausted") == "true",
        "kimi_membership_level_numeric": level,
    }
    if not is_member:
        result["plan_type"] = "Free"
    return result


def _parse_desktop_membership_line(line):
    match = _DESKTOP_MEMBERSHIP_RE.search(line)
    if not match:
        return {}
    return {"plan_type": match.group("plan").replace("_", " ")}


def _read_desktop_subscription():
    """Monthly/omni quota + plan name, scraped from Kimi Desktop's log.

    Scans progressively larger tails instead of a single fixed window: the app
    only logs `refreshed(sub)` when it actually refreshes the subscription
    (which can be days apart), while everything else it logs is high-volume, so
    the newest quota line drifts arbitrarily far from the end of the file. A
    fixed 128KB window silently lost it once the log grew past that, blanking
    the MONTHLY bar. Escalating keeps the common case (fresh line, small read)
    just as cheap while still finding an old line in a large log.
    """
    global _subscription_scan_key, _subscription_scan_result

    try:
        size = DESKTOP_LOG_PATH.stat().st_size
    except OSError:
        return {}

    # An incomplete scan escalates all the way to the largest tier, so without
    # this the "line has aged out entirely" case would re-decode multiple MB on
    # every 3s poll forever. The log only ever grows (Kimi rotates by replacing
    # the file, which changes its size), so size is a sound cache key: a new
    # line to find always means a new size.
    if _subscription_scan_key == size:
        return dict(_subscription_scan_result)

    result = {}
    for max_bytes in DESKTOP_LOG_SCAN_BYTES:
        for line in reversed(_tail_lines(DESKTOP_LOG_PATH, max_bytes=max_bytes)):
            if "plan_type" not in result:
                result.update(_parse_desktop_membership_line(line))
            if "kimi_monthly_percent" not in result:
                result.update(_parse_desktop_subscription_line(line))
            if "kimi_monthly_percent" in result and "plan_type" in result:
                break
        else:
            if size > max_bytes:
                continue
        break

    _subscription_scan_key = size
    _subscription_scan_result = dict(result)
    return result


def _pid_is_running(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (TypeError, ValueError, OSError):
        return False


def _read_desktop_runtime():
    runner = _read_json(DESKTOP_RUNNER_STATE_PATH)
    generation = runner.get("daemonGeneration")
    generation = generation if isinstance(generation, dict) else {}
    if runner.get("lifecycleStatus") != "running" or not _pid_is_running(generation.get("pid")):
        return None

    active_keys = (
        "activeOperations",
        "activeKernelTurns",
        "activeKernelToolCalls",
        "activePendingInteractions",
    )
    has_active_work = any(
        isinstance(runner.get(key), list) and runner[key]
        for key in active_keys
    )
    return {
        "state": "running" if has_active_work else "idle",
        "detail": "desktop daemon",
        "project": "Kimi Desktop",
        "updated_at": time.time(),
    }


def _write_credentials_atomic(credentials):
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".kimi-code.", suffix=".tmp", dir=CREDENTIALS_PATH.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(credentials, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, CREDENTIALS_PATH)
        os.chmod(CREDENTIALS_PATH, 0o600)
    finally:
        with contextlib.suppress(OSError):
            temp_path.unlink()


@contextlib.contextmanager
def _oauth_refresh_lock():
    """Share Kimi Code's proper-lockfile directory protocol.

    The official CLI locks ~/.kimi-code/oauth/kimi-code by creating the
    sibling kimi-code.lock directory. Keeping that directory's mtime fresh
    prevents either process from rotating the one-time refresh token while
    the other is using it.
    """
    target = KIMI_HOME / "oauth" / "kimi-code"
    lock_dir = Path(f"{target}.lock")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=True)
        lock_dir.mkdir()
    except OSError:
        yield False
        return

    stop = threading.Event()

    def heartbeat():
        while not stop.wait(2):
            with contextlib.suppress(OSError):
                os.utime(lock_dir, None)

    thread = threading.Thread(target=heartbeat, name="kimi-oauth-lock", daemon=True)
    thread.start()
    try:
        yield True
    finally:
        stop.set()
        thread.join(timeout=1)
        with contextlib.suppress(OSError):
            lock_dir.rmdir()


def _post_refresh(refresh_token):
    body = urllib.parse.urlencode({
        "client_id": OAUTH_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()
    request = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=OAUTH_REQUEST_TIMEOUT_SEC) as response:
        payload = json.load(response)
    return payload if isinstance(payload, dict) else {}


def _refresh_access_token(force=False):
    credentials = _read_json(CREDENTIALS_PATH)
    now = int(time.time())
    access_token = credentials.get("access_token")
    expires_at = credentials.get("expires_at") or 0
    if not force and access_token and expires_at > now + TOKEN_REFRESH_MARGIN_SEC:
        return access_token

    with _oauth_refresh_lock() as acquired:
        if not acquired:
            # A Kimi process may be rotating the token. Re-read once; the next
            # 60-second HUD poll will retry if the peer has not finished yet.
            credentials = _read_json(CREDENTIALS_PATH)
            return credentials.get("access_token")

        credentials = _read_json(CREDENTIALS_PATH)
        access_token = credentials.get("access_token")
        expires_at = credentials.get("expires_at") or 0
        if not force and access_token and expires_at > now + TOKEN_REFRESH_MARGIN_SEC:
            return access_token
        refresh_token = credentials.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            return access_token

        payload = _post_refresh(refresh_token)
        new_access = payload.get("access_token")
        new_refresh = payload.get("refresh_token")
        try:
            expires_in = int(payload.get("expires_in"))
        except (TypeError, ValueError):
            expires_in = 0
        if not new_access or not new_refresh or expires_in <= 0:
            return access_token

        updated = {
            **credentials,
            **payload,
            "access_token": new_access,
            "refresh_token": new_refresh,
            "expires_in": expires_in,
            "expires_at": int(time.time()) + expires_in,
        }
        _write_credentials_atomic(updated)
        return new_access


def _fetch_usage_payload(access_token):
    request = urllib.request.Request(
        USAGE_URL,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=USAGE_REQUEST_TIMEOUT_SEC) as response:
        payload = json.load(response)
    return payload if isinstance(payload, dict) else {}


def _managed_usage():
    global _usage_cache_at, _usage_cache, _last_good_usage

    now = time.time()
    if now - _usage_cache_at < USAGE_CACHE_SEC:
        return dict(_usage_cache)

    parsed = {}
    try:
        access_token = _refresh_access_token()
        if access_token:
            try:
                parsed = _parse_usage_payload(_fetch_usage_payload(access_token))
            except urllib.error.HTTPError as exc:
                if exc.code not in (401, 403):
                    raise
                access_token = _refresh_access_token(force=True)
                if access_token:
                    parsed = _parse_usage_payload(_fetch_usage_payload(access_token))
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
        parsed = {}

    if any(value is not None for key, value in parsed.items() if key.endswith("_percent")):
        _last_good_usage = dict(parsed)
    elif _last_good_usage:
        parsed = dict(_last_good_usage)
        parsed["kimi_usage_stale"] = True

    _usage_cache_at = now
    _usage_cache = dict(parsed)
    return parsed


def _session_event_summary(wire_path):
    last_event = {}
    latest_status = {}
    for line in _tail_lines(wire_path):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        last_event = event
        if event.get("type") == "agent.status.updated":
            latest_status = event
    return last_event, latest_status


def _read_sessions():
    now = time.time()
    entries = []
    for line in _tail_lines(SESSION_INDEX_PATH, max_bytes=256 * 1024):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("sessionDir"):
            entries.append(item)

    sessions = []
    latest_context_percent = None
    seen = set()
    for item in reversed(entries):
        session_dir = Path(item["sessionDir"])
        if session_dir in seen:
            continue
        seen.add(session_dir)
        wire_path = session_dir / "agents" / "main" / "wire.jsonl"
        state_path = session_dir / "state.json"
        mtimes = []
        for path in (wire_path, state_path):
            try:
                mtimes.append(path.stat().st_mtime)
            except OSError:
                pass
        if not mtimes:
            continue
        updated_at = max(mtimes)
        age = now - updated_at
        if age > ACTIVE_WINDOW_SEC:
            continue

        last_event, latest_status = _session_event_summary(wire_path)
        event_type = last_event.get("type")
        phase = latest_status.get("phase")
        if age <= IDLE_THRESHOLD_SEC and (
            event_type in ACTIVE_EVENT_TYPES or phase in ("thinking", "tool", "running")
        ):
            state = "thinking" if event_type in THINKING_EVENT_TYPES or phase == "thinking" else "running"
        else:
            state = "idle"

        context_percent = latest_status.get("contextUsage")
        if context_percent is not None and latest_context_percent is None:
            try:
                latest_context_percent = float(context_percent) * 100.0
            except (TypeError, ValueError):
                pass

        project = item.get("workDir") or _read_json(state_path).get("workDir") or ""
        project = str(project).replace(str(Path.home()), "~", 1)
        sessions.append({
            "state": state,
            "detail": event_type or "",
            "project": project,
            "updated_at": updated_at,
        })
        if len(sessions) >= MAX_SESSIONS:
            break

    sessions.sort(key=lambda row: (-STATE_PRIORITY.get(row["state"], 0), -row["updated_at"]))
    return sessions, latest_context_percent


def read_status():
    subscription = _read_desktop_subscription()
    usage = _managed_usage()
    sessions, context_percent = _read_sessions()
    desktop_session = _read_desktop_runtime()
    if desktop_session:
        sessions.append(desktop_session)
        sessions.sort(key=lambda row: (-STATE_PRIORITY.get(row["state"], 0), -row["updated_at"]))
        sessions = sessions[:MAX_SESSIONS]
    active_count = sum(1 for row in sessions if row["state"] in ("running", "thinking"))
    state = sessions[0]["state"] if sessions else "no session"
    return {
        "tool": "Kimi Code",
        **usage,
        # Kimi Desktop's own commercialInfo event contains the user-facing
        # product name (for example Vivace), so it takes precedence over any
        # internal API enum retained in the usage payload.
        **subscription,
        "state": state,
        "sessions": sessions,
        "active_count": active_count,
        "context_percent": context_percent,
    }


if __name__ == "__main__":
    # The status object intentionally contains no credential material.
    print(json.dumps(read_status(), indent=2, ensure_ascii=False))
