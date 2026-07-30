import json
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from sources.background_cache import BackgroundCache

DB_PATH = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"
V2_TASKS_DB_PATH = Path.home() / ".zcode" / "v2" / "tasks-index.sqlite"
CLI_CONFIG_PATH = Path.home() / ".zcode" / "cli" / "config.json"
# The endpoint path below reuses the provider key already selected by ZCode.
# Chromium Local Storage remains a read-only fallback for the last good plan
# name and quota snapshot when the live request is unavailable.
LOCAL_STORAGE_DIR = (
    Path.home() / "Library" / "Application Support" / "ZCode" / "session" / "Local Storage" / "leveldb"
)
BIGMODEL_QUOTA_URL = "https://bigmodel.cn/api/monitor/usage/quota/limit"
ZAI_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
LIVE_STATUS_TIMEOUT_SEC = 10
LIVE_ENTITLEMENT_CACHE_SEC = 60
PROCESS_PROBE_CACHE_SEC = 5
IDLE_THRESHOLD_SEC = 90
ACTIVE_WINDOW_SEC = 30 * 60
MAX_SESSIONS = 6

STATE_PRIORITY = {"running": 2, "thinking": 1, "idle": 0}

_ENTITLEMENT_RE = re.compile(r'\{"cachedAt":\d+,"snapshot":')


def _ms_to_s(ms):
    return ms / 1000 if ms else None


def _read_cached_entitlement():
    if not LOCAL_STORAGE_DIR.exists():
        return None
    best = None
    paths = sorted(LOCAL_STORAGE_DIR.glob("*.log")) + sorted(LOCAL_STORAGE_DIR.glob("*.ldb"))
    for path in paths:
        try:
            text = path.read_bytes().decode("utf-8", "ignore")
        except OSError:
            continue
        for m in _ENTITLEMENT_RE.finditer(text):
            try:
                obj, _ = json.JSONDecoder().raw_decode(text, m.start())
            except (json.JSONDecodeError, ValueError):
                continue
            if best is None or (obj.get("cachedAt") or 0) > (best.get("cachedAt") or 0):
                best = obj
    return best


def _entitlement_metrics():
    obj = _read_cached_entitlement()
    if not obj:
        return {}
    snap = obj.get("snapshot") or {}
    if not snap.get("authenticated"):
        return {}

    limits = ((snap.get("quota") or {}).get("limits")) or []
    token_limit = next((l for l in limits if l.get("type") == "TOKENS_LIMIT"), None)
    request_limit = next((l for l in limits if l.get("type") == "TIME_LIMIT"), None)

    usage_details = (request_limit or {}).get("usageDetails") or []
    top_feature = max(usage_details, key=lambda d: d.get("usage") or 0, default=None)
    if top_feature and not (top_feature.get("usage") or 0):
        top_feature = None

    return {
        "zcode_plan": (snap.get("context") or {}).get("displayName"),
        "zcode_token_percent": (token_limit or {}).get("percentage"),
        "zcode_token_resets_at": _ms_to_s((token_limit or {}).get("nextResetTime")),
        "zcode_request_percent": (request_limit or {}).get("percentage"),
        "zcode_request_remaining": (request_limit or {}).get("remaining"),
        "zcode_request_total": (request_limit or {}).get("usage"),
        "zcode_request_resets_at": _ms_to_s((request_limit or {}).get("nextResetTime")),
        "zcode_top_feature": (top_feature or {}).get("modelCode"),
        "zcode_top_feature_usage": (top_feature or {}).get("usage"),
    }


def _load_zcode_auth():
    try:
        config = json.loads(CLI_CONFIG_PATH.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    main_model = (config.get("model") or {}).get("main")
    if not isinstance(main_model, str):
        return None
    provider_id = main_model.split("/", 1)[0]
    provider = (config.get("provider") or {}).get(provider_id)
    options = provider.get("options") if isinstance(provider, dict) else None
    api_key = options.get("apiKey") if isinstance(options, dict) else None
    base_url = options.get("baseURL") if isinstance(options, dict) else None
    if not isinstance(api_key, str) or not api_key.strip():
        return None
    hostname = urllib.parse.urlparse(base_url or "").hostname or ""
    if hostname == "z.ai" or hostname.endswith(".z.ai"):
        quota_url = ZAI_QUOTA_URL
    elif hostname == "bigmodel.cn" or hostname.endswith(".bigmodel.cn"):
        quota_url = BIGMODEL_QUOTA_URL
    else:
        return None
    return api_key.strip(), quota_url


def _parse_live_entitlement(payload):
    if not isinstance(payload, dict):
        return None
    if payload.get("success") is False or payload.get("code") not in (None, 0, 200):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    limits = data.get("limits") or []
    token_limit = next(
        (item for item in limits if isinstance(item, dict) and item.get("type") == "TOKENS_LIMIT"),
        None,
    )
    request_limit = next(
        (item for item in limits if isinstance(item, dict) and item.get("type") == "TIME_LIMIT"),
        None,
    )
    usage_details = (request_limit or {}).get("usageDetails") or []
    top_feature = max(
        (item for item in usage_details if isinstance(item, dict)),
        key=lambda item: item.get("usage") or 0,
        default=None,
    )
    if top_feature and not (top_feature.get("usage") or 0):
        top_feature = None

    return {
        "zcode_plan_level_raw": data.get("level"),
        "zcode_token_percent": (token_limit or {}).get("percentage"),
        "zcode_token_resets_at": _ms_to_s((token_limit or {}).get("nextResetTime")),
        "zcode_request_percent": (request_limit or {}).get("percentage"),
        "zcode_request_remaining": (request_limit or {}).get("remaining"),
        "zcode_request_total": (request_limit or {}).get("usage"),
        "zcode_request_resets_at": _ms_to_s((request_limit or {}).get("nextResetTime")),
        "zcode_top_feature": (top_feature or {}).get("modelCode"),
        "zcode_top_feature_usage": (top_feature or {}).get("usage"),
    }


def _fetch_live_entitlement():
    auth = _load_zcode_auth()
    if not auth:
        return None
    api_key, quota_url = auth
    req = urllib.request.Request(
        quota_url,
        headers={
            "authorization": api_key,
            "Accept": "application/json",
            "User-Agent": "therm-vibe-hud",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=LIVE_STATUS_TIMEOUT_SEC) as response:
            payload = json.load(response)
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.HTTPError, urllib.error.URLError):
        return None
    return _parse_live_entitlement(payload)


_live_entitlement_cache = BackgroundCache(_fetch_live_entitlement, LIVE_ENTITLEMENT_CACHE_SEC)


def _live_entitlement():
    return _live_entitlement_cache.get() or {}


def _probe_host_process_running():
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", r"^zcode-host-local-1( |$)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=0.8,
            check=False,
        )
        cached = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        cached = False
    return cached


_host_process_cache = BackgroundCache(
    _probe_host_process_running, PROCESS_PROBE_CACHE_SEC
)


def _host_process_running():
    return bool(_host_process_cache.get())


def _read_sessions():
    now = time.time()
    cutoff_ms = int((now - ACTIVE_WINDOW_SEC) * 1000)
    midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    midnight_ms = int(midnight * 1000)
    sessions = []
    seen_ids = set()
    sessions_today = 0
    latest_tokens = None
    latest_cache_hit_percent = None

    if V2_TASKS_DB_PATH.exists():
        try:
            con = sqlite3.connect(f"file:{V2_TASKS_DB_PATH}?mode=ro", uri=True, timeout=1)
            try:
                cur = con.cursor()
                cur.execute(
                    "SELECT task_id, title, task_status, updated_at, workspace_path "
                    "FROM tasks WHERE deleted=0 AND updated_at >= ? "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (cutoff_ms, MAX_SESSIONS),
                )
                for task_id, title, task_status, updated_at_ms, workspace_path in cur.fetchall():
                    seen_ids.add(task_id)
                    state = "idle"
                    normalized = (task_status or "").lower()
                    if normalized == "thinking":
                        state = "thinking"
                    elif normalized in {
                        "active",
                        "running",
                        "in_progress",
                        "queued",
                        "waiting",
                        "waiting_on_approval",
                        "waiting_on_user_input",
                    }:
                        state = "running"
                    sessions.append(
                        {
                            "state": state,
                            "detail": (title or task_status or "")[:60],
                            "project": (workspace_path or "").replace(str(Path.home()), "~", 1),
                            "updated_at": (updated_at_ms or 0) / 1000,
                        }
                    )
                cur.execute(
                    "SELECT COUNT(*) FROM tasks WHERE deleted=0 AND created_at >= ?",
                    (midnight_ms,),
                )
                sessions_today = cur.fetchone()[0]
            finally:
                con.close()
        except sqlite3.Error:
            pass

    if DB_PATH.exists():
        try:
            con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=1)
            try:
                cur = con.cursor()
                cur.execute(
                    "SELECT id, title, time_updated, directory FROM session "
                    "WHERE time_updated >= ? ORDER BY time_updated DESC LIMIT ?",
                    (cutoff_ms, MAX_SESSIONS),
                )
                for session_id, title, time_updated_ms, directory in cur.fetchall():
                    if session_id in seen_ids:
                        continue
                    mtime = (time_updated_ms or 0) / 1000
                    age = now - mtime
                    cur.execute(
                        "SELECT json_extract(data,'$.role'), json_extract(data,'$.tokens.total'), "
                        "json_extract(data,'$.tokens.input'), json_extract(data,'$.tokens.cache.read') "
                        "FROM message WHERE session_id=? ORDER BY id DESC LIMIT 1",
                        (session_id,),
                    )
                    last_row = cur.fetchone()
                    last_role = last_row[0] if last_row else None
                    session_tokens = last_row[1] if last_row else None
                    session_input, session_cache_read = (
                        (last_row[2], last_row[3]) if last_row else (None, None)
                    )
                    if latest_tokens is None and session_tokens is not None:
                        latest_tokens = session_tokens
                        if session_input:
                            latest_cache_hit_percent = (
                                (session_cache_read or 0) / session_input * 100
                            )
                    if age > IDLE_THRESHOLD_SEC:
                        state, detail = "idle", "waiting for input"
                    elif last_role == "assistant":
                        state, detail = "thinking", ""
                    else:
                        state, detail = "running", ""
                    sessions.append(
                        {
                            "state": state,
                            "detail": detail or (title or "")[:60],
                            "project": (directory or "").replace(str(Path.home()), "~", 1),
                            "updated_at": mtime,
                        }
                    )
                cur.execute(
                    "SELECT COUNT(*) FROM session WHERE time_created >= ?",
                    (midnight_ms,),
                )
                sessions_today = max(sessions_today, cur.fetchone()[0])
            finally:
                con.close()
        except sqlite3.Error:
            pass

    sessions.sort(key=lambda s: (-STATE_PRIORITY.get(s["state"], 0), -s["updated_at"]))
    return sessions[:MAX_SESSIONS], latest_tokens, sessions_today, latest_cache_hit_percent


def read_status():
    cached_entitlement = _entitlement_metrics()
    live_entitlement = _live_entitlement()
    entitlement = {**cached_entitlement, **live_entitlement}
    if cached_entitlement.get("zcode_plan"):
        entitlement["zcode_plan"] = cached_entitlement["zcode_plan"]
    elif live_entitlement.get("zcode_plan_level_raw"):
        entitlement["zcode_plan"] = str(live_entitlement["zcode_plan_level_raw"]).title()

    host_running = _host_process_running()
    sessions, latest_tokens, sessions_today, latest_cache_hit_percent = _read_sessions()
    if host_running and not sessions:
        sessions = [
            {
                "state": "idle",
                "detail": "local RPC host",
                "project": "ZCode Desktop",
                "updated_at": time.time(),
            }
        ]

    if host_running:
        active_count = sum(
            1 for session in sessions if session["state"] in ("running", "thinking")
        )
        aggregate_state = sessions[0]["state"] if sessions else "idle"
    else:
        sessions = []
        active_count = 0
        aggregate_state = "no session"

    return {
        "tool": "zcode",
        "display_name": "GLM",
        "identity": entitlement.get("zcode_plan"),
        **entitlement,
        "health": "online" if host_running else "offline",
        "data_source": "host-process" if host_running else "unavailable",
        "usage_source": (
            "endpoint"
            if live_entitlement
            else ("local-storage" if cached_entitlement else "unavailable")
        ),
        "state": aggregate_state,
        "sessions": sessions,
        "active_count": active_count,
        "session_tokens": latest_tokens,
        "sessions_today": sessions_today,
        "cache_hit_percent": latest_cache_hit_percent,
    }


if __name__ == "__main__":
    print(json.dumps(read_status(), indent=2, ensure_ascii=False))
