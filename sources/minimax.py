import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources import claude_code, codex_cli
from sources.background_cache import BackgroundCache

PROVIDER_ENV_PATH = Path.home() / ".claude" / "providers" / "minimax.env"
DEFAULT_BASE_URL = "https://api.minimaxi.com"
ENDPOINT_TIMEOUT_SEC = 10
ENDPOINT_CACHE_SEC = 60
RESPONSE_MAX_BYTES = 512 * 1024
DESKTOP_PROCESS_CACHE_SEC = 5
DESKTOP_PLAN_CACHE_SEC = 60
DESKTOP_CONFIG_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "MiniMax"
    / "minimax-agent-cn-config.json"
)
DESKTOP_LEVELDB_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "MiniMax"
    / "Local Storage"
    / "leveldb"
)
DESKTOP_EXECUTABLE = "/Applications/MiniMax Code.app/Contents/MacOS/MiniMax Code"
DESKTOP_ORIGIN = "https://agent.minimaxi.com"
DESKTOP_MEMBERSHIP_PATH = "/matrix/api/v1/user/get_user_extra_info"

STATE_PRIORITY = {"running": 2, "thinking": 1, "idle": 0}
MINIMAX_CONTEXT_WINDOWS = {
    "minimax-m3": 1_000_000,
    "minimax-m2.7": 204_800,
}
_LOCAL_ENV_KEYS = {
    "MINIMAX_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "MINIMAX_BASE_URL",
    "ANTHROPIC_BASE_URL",
}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_no_redirect_opener = urllib.request.build_opener(_NoRedirectHandler)


def _read_provider_env():
    try:
        lines = PROVIDER_ENV_PATH.read_text().splitlines()
    except OSError:
        return {}

    values = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key not in _LOCAL_ENV_KEYS:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if value:
            values[key] = value
    return values


def _minimax_host_family(hostname):
    hostname = (hostname or "").lower()
    for domain in ("minimaxi.com", "minimax.io"):
        if hostname == domain or hostname.endswith(f".{domain}"):
            return domain
    return None


def _load_endpoint_config():
    local_env = _read_provider_env()
    base_url = (
        os.environ.get("MINIMAX_BASE_URL")
        or local_env.get("MINIMAX_BASE_URL")
        or local_env.get("ANTHROPIC_BASE_URL")
        or DEFAULT_BASE_URL
    )
    api_key = (
        os.environ.get("MINIMAX_API_KEY")
        or local_env.get("MINIMAX_API_KEY")
        or local_env.get("ANTHROPIC_AUTH_TOKEN")
    )
    if not api_key:
        return None

    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "https" or _minimax_host_family(parsed.hostname) is None:
        return None
    origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return {
        "origin": origin,
        "token_plan_url": f"{origin}/v1/token_plan/remains",
        "api_key": api_key,
    }


def _read_endpoint_json(url, api_key):
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "therm-vibe-hud",
        },
    )
    with _no_redirect_opener.open(request, timeout=ENDPOINT_TIMEOUT_SEC) as response:
        payload = response.read(RESPONSE_MAX_BYTES + 1)
        if len(payload) > RESPONSE_MAX_BYTES:
            raise ValueError("MiniMax response too large")
        return json.loads(payload)


def _epoch_ms_to_seconds(value):
    try:
        return float(value) / 1000 if value else None
    except (TypeError, ValueError):
        return None


def _used_percent(total, remaining):
    try:
        total, remaining = float(total), float(remaining)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return max(0.0, min(100.0, (total - remaining) / total * 100))


def _used_from_remaining_percent(remaining_percent):
    try:
        remaining_percent = float(remaining_percent)
    except (TypeError, ValueError):
        return None
    remaining_percent = max(0.0, min(100.0, remaining_percent))
    return 100.0 - remaining_percent


def _parse_token_plan(payload):
    if not isinstance(payload, dict):
        return None
    base_resp = payload.get("base_resp")
    if isinstance(base_resp, dict) and base_resp.get("status_code") not in (None, 0):
        return None

    models = payload.get("model_remains")
    if not isinstance(models, list):
        return None
    quota_model = next(
        (
            model
            for model in models
            if isinstance(model, dict)
            and str(model.get("model_name") or "").lower().startswith("minimax-m")
        ),
        None,
    )
    if quota_model is None:
        quota_model = next(
            (
                model
                for model in models
                if isinstance(model, dict)
                and str(model.get("model_name") or "").lower() == "general"
            ),
            None,
        )
    if quota_model is None:
        return None

    five_total = quota_model.get("current_interval_total_count")
    five_remaining = quota_model.get("current_interval_usage_count")
    weekly_total = quota_model.get("current_weekly_total_count")
    weekly_remaining = quota_model.get("current_weekly_usage_count")
    five_percent = _used_from_remaining_percent(
        quota_model.get("current_interval_remaining_percent")
    )
    if five_percent is None:
        five_percent = _used_percent(five_total, five_remaining)
    weekly_percent = _used_from_remaining_percent(
        quota_model.get("current_weekly_remaining_percent")
    )
    if weekly_percent is None:
        weekly_percent = _used_percent(weekly_total, weekly_remaining)
    if five_percent is None and weekly_percent is None:
        return None

    return {
        "minimax_token_plan": True,
        "minimax_five_hour_percent": five_percent,
        "minimax_five_hour_remaining": five_remaining,
        "minimax_five_hour_total": five_total,
        "minimax_five_hour_resets_at": _epoch_ms_to_seconds(
            quota_model.get("end_time")
        ),
        "minimax_five_hour_status": quota_model.get("current_interval_status"),
        "minimax_weekly_percent": weekly_percent,
        "minimax_weekly_remaining": weekly_remaining,
        "minimax_weekly_total": weekly_total,
        "minimax_weekly_resets_at": _epoch_ms_to_seconds(
            quota_model.get("weekly_end_time")
        ),
        "minimax_weekly_status": quota_model.get("current_weekly_status"),
    }


def _offline_status(error):
    return {
        "health": "offline",
        "data_source": "minimax-token-plan",
        "endpoint_error": error,
        "plan_type": None,
        "minimax_token_plan": False,
        "minimax_five_hour_percent": None,
        "minimax_weekly_percent": None,
    }


def _fetch_endpoint_status():
    config = _load_endpoint_config()
    if not config:
        return _offline_status("no-local-auth")

    token_plan_url = config["token_plan_url"]
    api_key = config["api_key"]
    try:
        quota_payload = _read_endpoint_json(token_plan_url, api_key)
    except urllib.error.HTTPError as exc:
        error = "authentication" if exc.code in (401, 403) else f"http-{exc.code}"
        return _offline_status(error)
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ):
        return _offline_status("unreachable")

    status = {
        "health": "online",
        "data_source": "minimax-token-plan",
        "endpoint_error": None,
        "plan_type": "Token Plan",
        "minimax_token_plan": False,
        "minimax_five_hour_percent": None,
        "minimax_five_hour_remaining": None,
        "minimax_five_hour_total": None,
        "minimax_five_hour_resets_at": None,
        "minimax_five_hour_status": None,
        "minimax_weekly_percent": None,
        "minimax_weekly_remaining": None,
        "minimax_weekly_total": None,
        "minimax_weekly_resets_at": None,
        "minimax_weekly_status": None,
    }
    token_plan = _parse_token_plan(quota_payload)
    if token_plan:
        status.update(token_plan)
    else:
        status["quota_error"] = "token-plan-unavailable"
    return status


_endpoint_cache = BackgroundCache(_fetch_endpoint_status, ENDPOINT_CACHE_SEC)


def _endpoint_status():
    return _endpoint_cache.get()


def _decode_jwt_payload(token):
    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded))
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_desktop_user_detail():
    try:
        files = sorted(
            (
                path
                for path in DESKTOP_LEVELDB_PATH.iterdir()
                if path.suffix in (".log", ".ldb")
            ),
            key=lambda path: path.stat().st_mtime,
        )
    except OSError:
        return None

    latest = None
    key = b"user_detail_agent"
    decoder = json.JSONDecoder()
    for path in files:
        try:
            if path.stat().st_size > 4 * 1024 * 1024:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        offset = 0
        while (offset := data.find(key, offset)) >= 0:
            snippet = data[
                offset + len(key) : offset + len(key) + 16 * 1024
            ].decode("utf-8", "ignore")
            cursor = 0
            while (cursor := snippet.find("{", cursor)) >= 0:
                try:
                    candidate, _ = decoder.raw_decode(snippet[cursor:])
                except (TypeError, ValueError, json.JSONDecodeError):
                    cursor += 1
                    continue
                if (
                    isinstance(candidate, dict)
                    and candidate.get("realUserID")
                    and candidate.get("token")
                ):
                    latest = candidate
                    break
                cursor += 1
            offset += len(key)
    return latest


def _load_desktop_auth():
    try:
        config = json.loads(DESKTOP_CONFIG_PATH.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(config, dict):
        return None

    tokens = config.get("tokens")
    token = tokens.get("accessToken") if isinstance(tokens, dict) else None
    if not isinstance(token, str) or not 100 < len(token) <= 16 * 1024:
        return None
    payload = _decode_jwt_payload(token)
    user = payload.get("user") if payload else None
    expires_at = payload.get("exp") if payload else None
    if not isinstance(user, dict):
        return None
    try:
        if expires_at is not None and float(expires_at) <= time.time():
            return None
    except (TypeError, ValueError):
        return None

    details = _read_desktop_user_detail()
    if details and details.get("token") != token:
        details = None
    config_user = config.get("user")
    config_device_id = (
        config_user.get("deviceID") if isinstance(config_user, dict) else None
    )
    user_id = details.get("realUserID") if details else user.get("id")
    device_id = user.get("deviceID") or config_device_id
    if not user_id or not device_id:
        return None
    return {
        "token": token,
        "user_id": str(user_id),
        "device_id": str(device_id),
    }


def _md5_hex(value):
    return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()


def _desktop_timezone_offset():
    local = time.localtime()
    return -(time.altzone if local.tm_isdst and time.daylight else time.timezone)


def _read_desktop_json(path, auth):
    if path != DESKTOP_MEMBERSHIP_PATH:
        raise ValueError("unsupported MiniMax desktop endpoint")

    body = b"{}"
    now_ms = int(time.time()) * 1000
    timestamp = now_ms // 1000
    params = {
        "device_platform": "web",
        "biz_id": 3,
        "app_id": "3001",
        "version_code": "22201",
        "unix": now_ms,
        "timezone_offset": _desktop_timezone_offset(),
        "is_desktop": 1,
        "sys_language": "zh",
        "lang": "zh",
        "uuid": auth["device_id"],
        "device_id": auth["device_id"],
        "os_name": "macOS",
        "browser_name": "Chrome",
        "user_id": auth["user_id"],
        "token": auth["token"],
        "client": "desktop",
    }
    path_with_query = f"{path}?{urllib.parse.urlencode(params)}"
    encoded_path = urllib.parse.quote(
        path_with_query,
        safe="-_.!~*'()",
    )
    yy = _md5_hex(
        f"{encoded_path}_{body.decode()}{_md5_hex(str(now_ms))}ooui"
    )
    signature = _md5_hex(
        f"{timestamp}I*7Cf%WZ#S&%1RlZJ&C2{body.decode()}"
    )
    token = auth["token"]
    request = urllib.request.Request(
        f"{DESKTOP_ORIGIN}{path_with_query}",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/140.0.0.0 "
                "Electron/38.3.0 Safari/537.36"
            ),
            "Origin": DESKTOP_ORIGIN,
            "Referer": f"{DESKTOP_ORIGIN}/",
            "Cookie": f"_token={token}",
            "token": token,
            "yy": yy,
            "x-timestamp": str(timestamp),
            "x-signature": signature,
        },
    )
    with _no_redirect_opener.open(request, timeout=ENDPOINT_TIMEOUT_SEC) as response:
        payload = response.read(RESPONSE_MAX_BYTES + 1)
        if len(payload) > RESPONSE_MAX_BYTES:
            raise ValueError("MiniMax desktop response too large")
        return json.loads(payload)


def _normalize_desktop_plan_type(value):
    normalized = str(value or "").strip().casefold()
    for marker, label in (
        ("ultra", "Ultra Plan"),
        ("max", "Max Plan"),
        ("plus", "Plus Plan"),
        ("starter", "Starter Plan"),
        ("standard", "Standard Plan"),
        ("pro", "Pro Plan"),
        ("basic", "Basic Plan"),
        ("free", "Free Plan"),
    ):
        if marker in normalized:
            return label
    return None


def _fetch_desktop_plan_status():
    auth = _load_desktop_auth()
    if not auth:
        return {}
    try:
        payload = _read_desktop_json(DESKTOP_MEMBERSHIP_PATH, auth)
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        urllib.error.HTTPError,
        urllib.error.URLError,
    ):
        return {}
    if not isinstance(payload, dict):
        return {}
    base_resp = payload.get("base_resp")
    if isinstance(base_resp, dict) and base_resp.get("status_code") not in (None, 0):
        return {}

    workspaces = payload.get("workspaces")
    if not isinstance(workspaces, list):
        return {}
    personal = [
        workspace
        for workspace in workspaces
        if isinstance(workspace, dict) and workspace.get("workspace_type") == 0
    ]
    workspace = next(
        (candidate for candidate in personal if candidate.get("selected")),
        personal[0] if personal else None,
    )
    if not workspace:
        return {}
    return {
        "has_token_plan": bool(workspace.get("has_token_plan")),
        "plan_type": _normalize_desktop_plan_type(
            workspace.get("token_plan_tier")
        ),
    }


_desktop_plan_cache = BackgroundCache(
    _fetch_desktop_plan_status, DESKTOP_PLAN_CACHE_SEC
)


def _desktop_plan_status():
    return _desktop_plan_cache.get() or {}


def _probe_desktop_running():
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "command="],
            capture_output=True,
            text=True,
            timeout=0.8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and any(
        line.startswith(DESKTOP_EXECUTABLE) for line in result.stdout.splitlines()
    )


_desktop_process_cache = BackgroundCache(
    _probe_desktop_running, DESKTOP_PROCESS_CACHE_SEC
)


def _desktop_running():
    return bool(_desktop_process_cache.get())


def _is_minimax_model(model):
    return "minimax-" in str(model or "").lower()


def _minimax_context_window(model):
    normalized = str(model or "").lower()
    return next(
        (
            window
            for model_prefix, window in MINIMAX_CONTEXT_WINDOWS.items()
            if model_prefix in normalized
        ),
        None,
    )


def _normalize_claude_session(session):
    normalized = dict(session)
    context_tokens = normalized.get("context_tokens")
    context_window = _minimax_context_window(normalized.get("model"))
    normalized["context_window"] = context_window
    if context_tokens is not None and context_window:
        normalized["context_percent"] = min(
            100.0, float(context_tokens) / context_window * 100
        )
    else:
        normalized["context_percent"] = None
    return normalized


def _recent_minimax_sessions():
    sessions = []
    for path, mtime in codex_cli._recent_rollouts():
        try:
            parsed = codex_cli._parse_session(path, mtime)
        except OSError:
            continue
        if _is_minimax_model(parsed.get("model")):
            sessions.append(parsed)
    for path, mtime in claude_code._recent_transcripts():
        try:
            parsed = claude_code._parse_session(path, mtime)
        except OSError:
            continue
        if _is_minimax_model(parsed.get("model")):
            sessions.append(_normalize_claude_session(parsed))
    return sessions


def read_status():
    endpoint = dict(_endpoint_status() or _offline_status("pending"))
    desktop_running = _desktop_running()
    desktop_plan = _desktop_plan_status() if desktop_running else {}
    if desktop_plan.get("plan_type"):
        endpoint["plan_type"] = desktop_plan["plan_type"]
    if desktop_running:
        endpoint["health"] = "online"
        endpoint["data_source"] = (
            "minimax-token-plan+desktop"
            if endpoint.get("minimax_token_plan")
            else "desktop-process"
        )

    sessions = _recent_minimax_sessions()
    if endpoint.get("health") != "online" and not desktop_running:
        sessions = []
    if desktop_running:
        sessions.append(
            {
                "state": "idle",
                "detail": "desktop app",
                "project": "MiniMax Code",
                "updated_at": time.time(),
                "context_tokens": None,
                "context_window": None,
                "context_percent": None,
                "cache_hit_percent": None,
            }
        )

    sessions.sort(
        key=lambda item: (
            -STATE_PRIORITY.get(item.get("state"), 0),
            -float(item.get("updated_at") or 0),
        )
    )
    rows = [
        {
            "state": session["state"],
            "detail": session.get("detail", ""),
            "project": session.get("project", ""),
            "updated_at": session.get("updated_at"),
        }
        for session in sessions
    ]
    active_count = sum(
        row["state"] in ("running", "thinking") for row in rows
    )
    aggregate_state = rows[0]["state"] if rows else (
        "idle" if endpoint.get("health") == "online" else "no session"
    )
    newest_first = sorted(
        sessions, key=lambda item: -(item.get("updated_at") or 0)
    )
    context_session = next(
        (
            session
            for session in newest_first
            if session.get("context_percent") is not None
        ),
        {},
    )
    cache_session = next(
        (
            session
            for session in newest_first
            if session.get("cache_hit_percent") is not None
        ),
        {},
    )
    return {
        "tool": "MiniMax",
        **endpoint,
        "state": aggregate_state,
        "sessions": rows,
        "active_count": active_count,
        "desktop_running": desktop_running,
        "context_tokens": context_session.get("context_tokens"),
        "context_window": context_session.get("context_window"),
        "context_percent": context_session.get("context_percent"),
        "cache_hit_percent": cache_session.get("cache_hit_percent"),
    }


if __name__ == "__main__":
    print(json.dumps(read_status(), indent=2, ensure_ascii=False))
