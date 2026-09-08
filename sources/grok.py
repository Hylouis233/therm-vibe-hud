import base64
import ctypes
import hashlib
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

GROK_HOME = Path.home() / ".grok"
ACTIVE_SESSIONS_PATH = GROK_HOME / "active_sessions.json"
SESSIONS_DIR = GROK_HOME / "sessions"
UNIFIED_LOG_PATH = GROK_HOME / "logs" / "unified.jsonl"

GROK_BOT_APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Grok Bot"
GROK_BOT_SECRETS_PATH = GROK_BOT_APP_SUPPORT / "sand-secrets.json"
GROK_BOT_EXECUTABLE = "/Applications/Grok Bot.app/Contents/MacOS/Grok Bot"
GROK_BOT_API_BASE = os.environ.get(
    "THERM_VIBE_GROK_BOT_API_BASE", "https://api2.cursor.sh"
).rstrip("/")
DASHBOARD_SERVICE = "aiserver.v1.DashboardService"
BOT_USAGE_CACHE_SEC = 5
CLI_USAGE_CACHE_SEC = 5
REQUEST_TIMEOUT_SEC = 30
MAX_SESSIONS = 6
SESSION_IDLE_SEC = 45

_cache_lock = threading.Lock()
_cli_cache = None
_cli_cache_at = 0.0
_cli_log_mtime = 0.0
_bot_cache = None
_bot_cache_at = 0.0
_usage_cache = {}

_COMMON_CRYPTO = None
if os.uname().sysname == "Darwin":
    _COMMON_CRYPTO = ctypes.CDLL("/usr/lib/system/libcommonCrypto.dylib")
    _COMMON_CRYPTO.CCCrypt.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _COMMON_CRYPTO.CCCrypt.restype = ctypes.c_int32


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _parse_time(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _pid_is_running(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _project_label(cwd):
    if not isinstance(cwd, str) or not cwd.strip():
        return "(root)"
    return Path(cwd).name or "(root)"


def _session_directory(cwd, session_id):
    return SESSIONS_DIR / quote(cwd, safe="") / session_id


def _active_sessions(now=None):
    payload = _read_json(ACTIVE_SESSIONS_PATH)
    if not isinstance(payload, list):
        return []
    now = time.time() if now is None else now
    sessions = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item["pid"])
            session_id = str(item["session_id"])
            cwd = str(item.get("cwd") or "")
        except (KeyError, TypeError, ValueError):
            continue
        if not _pid_is_running(pid):
            continue
        directory = _session_directory(cwd, session_id)
        summary = _read_json(directory / "summary.json") or {}
        signals = _read_json(directory / "signals.json") or {}
        updated_at = _parse_time(summary.get("updated_at")) or now
        sessions.append(
            {
                "id": session_id,
                "pid": pid,
                "cwd": cwd,
                "project": _project_label(cwd),
                # A live PID only proves some process exists (PIDs get
                # recycled); summary recency is the honest liveness signal —
                # otherwise a CLI left open overnight shows "running" forever.
                "state": "running" if now - updated_at <= SESSION_IDLE_SEC else "idle",
                "updated_at": updated_at,
                "model": summary.get("current_model_id"),
                "signals": signals,
            }
        )
    return sorted(sessions, key=lambda session: session["updated_at"], reverse=True)


def _empty_usage_totals():
    return {
        "inputTokens": 0,
        "outputTokens": 0,
        "totalTokens": 0,
        "cachedReadTokens": 0,
        "reasoningTokens": 0,
        "modelCalls": 0,
    }


def _usage_totals_from_updates(path):
    try:
        stat = path.stat()
        cache_key = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return _empty_usage_totals()
    cached = _usage_cache.get(path)
    if cached is not None and cached[0] == cache_key:
        return cached[1]

    totals = _empty_usage_totals()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                # Session updates can carry very large encrypted assistant
                # blobs.  JSON-decoding those lines allocates hundreds of MiB
                # across a day of sessions even though usage records are small;
                # pre-filter the textual key before parsing.
                if '"usage"' not in line:
                    continue
                try:
                    event = json.loads(line)
                    usage = event["params"]["update"]["usage"]
                except (ValueError, KeyError, TypeError):
                    continue
                if not isinstance(usage, dict):
                    continue
                for key in totals:
                    value = usage.get(key)
                    if isinstance(value, (int, float)) and value >= 0:
                        totals[key] += value
    except OSError:
        pass
    _usage_cache[path] = (cache_key, totals)
    return totals


def _latest_cli_billing():
    """Read the newest official billing config Grok CLI appends to its log.

    The CLI refreshes this record when it starts and after usage-changing
    turns. Reading its latest append is authoritative without invoking a model
    or reconstructing an undocumented HTTP endpoint.
    """
    try:
        size = UNIFIED_LOG_PATH.stat().st_size
        with UNIFIED_LOG_PATH.open("rb") as handle:
            handle.seek(max(0, size - 1_048_576), os.SEEK_SET)
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    for line in reversed(tail.splitlines()):
        try:
            event = json.loads(line)
            if event.get("msg") != "billing: fetched credits config":
                continue
            config = event["ctx"]["config"]
            percent = _number(config.get("creditUsagePercent"))
            current_period = config.get("currentPeriod") or {}
            resets_at = _parse_time(current_period.get("end"))
            on_demand_cap = _number((config.get("onDemandCap") or {}).get("val"))
            on_demand_used = _number((config.get("onDemandUsed") or {}).get("val"))
            prepaid_balance = _number((config.get("prepaidBalance") or {}).get("val"))
            return {
                "percent": percent,
                "resets_at": resets_at,
                "plan": config.get("subscriptionTier"),
                "on_demand_cap": on_demand_cap,
                "on_demand_used": on_demand_used,
                "prepaid_balance": prepaid_balance,
                "updated_at": _parse_time(event.get("ts")),
            }
        except (ValueError, KeyError, TypeError):
            continue
    return None


def _cli_usage(now=None):
    global _cli_cache, _cli_cache_at, _cli_log_mtime
    now = time.time() if now is None else now
    log_mtime = 0.0
    try:
        log_mtime = UNIFIED_LOG_PATH.stat().st_mtime
    except OSError:
        pass
    with _cache_lock:
        if (
            _cli_cache is not None
            and now - _cli_cache_at < CLI_USAGE_CACHE_SEC
            and log_mtime <= _cli_log_mtime
        ):
            return _cli_cache

    active = _active_sessions(now=now)
    cutoff = now - 24 * 3600
    recent = []
    try:
        candidates = list(SESSIONS_DIR.glob("*/*/summary.json"))
    except OSError:
        candidates = []
    for summary_path in candidates:
        summary = _read_json(summary_path)
        updated_at = _parse_time((summary or {}).get("updated_at"))
        if updated_at is None or updated_at < cutoff:
            continue
        recent.append((updated_at, summary_path.parent))

    totals = _empty_usage_totals()
    for _updated_at, directory in recent:
        session_totals = _usage_totals_from_updates(directory / "updates.jsonl")
        for key, value in session_totals.items():
            totals[key] += value

    context_percent = None
    context_tokens = None
    context_window = None
    for session in active:
        signals = session["signals"]
        percent = signals.get("contextWindowUsage")
        if percent is not None and (context_percent is None or percent > context_percent):
            context_percent = percent
            context_tokens = signals.get("contextTokensUsed")
            context_window = signals.get("contextWindowTokens")

    input_tokens = totals["inputTokens"]
    cache_hit = None
    if input_tokens:
        cache_hit = min(totals["cachedReadTokens"] / input_tokens * 100.0, 100.0)
    models = [session["model"] for session in active if session.get("model")]
    result = {
        "active": active,
        "context_percent": context_percent,
        "context_tokens": context_tokens,
        "context_window": context_window,
        "cache_hit_percent": cache_hit,
        "sessions_24h": len(recent),
        "tokens_24h": totals["totalTokens"],
        "input_tokens_24h": input_tokens,
        "output_tokens_24h": totals["outputTokens"],
        "cached_tokens_24h": totals["cachedReadTokens"],
        "model_calls_24h": totals["modelCalls"],
        "model": models[0] if models else None,
        "billing": _latest_cli_billing(),
    }
    with _cache_lock:
        _cli_cache = result
        _cli_cache_at = now
        _cli_log_mtime = log_mtime
    return result


def _bot_process_running():
    try:
        result = subprocess.run(
            ["ps", "-axo", "command="], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.strip() == GROK_BOT_EXECUTABLE for line in result.stdout.splitlines())


def _safe_storage_password():
    """Read the SafeStorage password into process memory without printing it."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Grok Bot Safe Storage", "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    password = result.stdout.strip()
    return password.encode("utf-8") if password else None


def _decrypt_safe_storage(value, key):
    if _COMMON_CRYPTO is None:
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return None
    if not raw.startswith(b"v10"):
        return None
    ciphertext = raw[3:]
    output = ctypes.create_string_buffer(len(ciphertext) + 32)
    output_len = ctypes.c_size_t(0)
    rc = _COMMON_CRYPTO.CCCrypt(
        1, 0, 1, key, len(key), b" " * 16,
        ciphertext, len(ciphertext), output, len(output), ctypes.byref(output_len),
    )
    if rc != 0:
        return None
    return output.raw[: output_len.value]


def _bot_credentials():
    """Return only the machine id and active access token, never refresh data."""
    if _COMMON_CRYPTO is None:
        return None
    payload = _read_json(GROK_BOT_SECRETS_PATH)
    if not isinstance(payload, dict):
        return None
    password = _safe_storage_password()
    if password is None:
        return None
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
    try:
        machine_id = _decrypt_safe_storage(payload["cursor-machine-id"], key)
        accounts = json.loads(payload["cursor-accounts"])
        active_id = accounts["active"]
        encrypted_token = accounts["accounts"][active_id]["cursor-access-token"]
        access_token = _decrypt_safe_storage(encrypted_token, key)
    except (KeyError, TypeError, ValueError):
        return None
    if not machine_id or not access_token:
        return None
    try:
        return machine_id.decode("utf-8"), access_token.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _cursor_checksum(machine_id):
    coarse_time = int(time.time() * 1000 // 1_000_000)
    encoded = bytearray(
        [
            (coarse_time >> 40) & 255,
            (coarse_time >> 32) & 255,
            (coarse_time >> 24) & 255,
            (coarse_time >> 16) & 255,
            (coarse_time >> 8) & 255,
            coarse_time & 255,
        ]
    )
    previous = 165
    for index, value in enumerate(encoded):
        encoded[index] = ((value ^ previous) + index) & 255
        previous = encoded[index]
    prefix = base64.urlsafe_b64encode(bytes(encoded)).rstrip(b"=").decode("ascii")
    return prefix + machine_id


def _api_request(method, credentials):
    machine_id, access_token = credentials
    url = f"{GROK_BOT_API_BASE}/{DASHBOARD_SERVICE}/{method}"
    headers = {
        "authorization": f"Bearer {access_token}",
        "x-cursor-checksum": _cursor_checksum(machine_id),
        "x-cursor-client-type": "sand",
        "x-cursor-client-version": "0.30.0",
        "x-sand-box-namespace": "prod",
        "x-ghost-mode": "true",
        "x-request-id": str(uuid.uuid4()),
        "content-type": "application/json",
        "connect-protocol-version": "1",
    }
    request = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SEC) as response:
        if response.status != 200:
            return None
        return json.loads(response.read(512 * 1024).decode("utf-8"))


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _bot_usage(now=None):
    global _bot_cache, _bot_cache_at
    now = time.time() if now is None else now
    with _cache_lock:
        if _bot_cache is not None and now - _bot_cache_at < BOT_USAGE_CACHE_SEC:
            return _bot_cache

    result = {
        "running": False,
        "percent": None,
        "resets_at": None,
        "period_percent": None,
        "period_resets_at": None,
        "plan": None,
        "available": None,
    }
    if not GROK_BOT_SECRETS_PATH.exists():
        with _cache_lock:
            _bot_cache = result
            _bot_cache_at = now
        return result
    result["running"] = _bot_process_running()
    credentials = _bot_credentials()
    if credentials is not None:
        try:
            status = _api_request("GetSandUsageStatus", credentials)
            period = _api_request("GetCurrentPeriodUsage", credentials)
        except (OSError, ValueError, TypeError, urllib.error.URLError):
            status = None
            period = None
        if isinstance(status, dict):
            result["percent"] = _number(status.get("usagePercent"))
            result["resets_at"] = _parse_time(status.get("nextResetTimestampUtc"))
            result["plan"] = status.get("grokPlanLabel")
            result["available"] = status.get("hasAvailableUsage")
        if isinstance(period, dict):
            plan_usage = period.get("planUsage")
            if isinstance(plan_usage, dict):
                result["period_percent"] = _number(plan_usage.get("totalPercentUsed"))
            period_resets_at = _number(period.get("billingCycleEnd"))
            if period_resets_at is not None and period_resets_at > 1_000_000_000_000:
                period_resets_at /= 1000.0
            result["period_resets_at"] = period_resets_at

    with _cache_lock:
        _bot_cache = result
        _bot_cache_at = now
    return result


def read_status():
    cli = _cli_usage()
    bot = _bot_usage()
    sessions = [
        {
            "project": session["project"],
            "state": session["state"],
            "updated_at": session["updated_at"],
        }
        for session in cli["active"][:MAX_SESSIONS]
    ]
    if bot["running"]:
        sessions.insert(
            0,
            {"project": "Grok Bot", "state": "running", "updated_at": time.time()},
        )

    active_count = (
        sum(1 for session in cli["active"] if session["state"] == "running")
        + int(bot["running"])
    )
    billing = cli.get("billing") or {}
    return {
        "tool": "Grok",
        "display_name": "Grok",
        "state": (
            "running"
            if active_count
            else ("idle" if cli["active"] else "no session")
        ),
        "identity": cli.get("model"),
        "plan_type": billing.get("plan"),
        "active_count": active_count,
        "sessions": sessions,
        "context_percent": cli.get("context_percent"),
        "context_tokens": cli.get("context_tokens"),
        "context_window": cli.get("context_window"),
        "cache_hit_percent": cli.get("cache_hit_percent"),
        "grok_cli_percent": billing.get("percent"),
        "grok_cli_resets_at": billing.get("resets_at"),
        "grok_cli_on_demand_used": billing.get("on_demand_used"),
        "grok_cli_on_demand_cap": billing.get("on_demand_cap"),
        "grok_cli_prepaid_balance": billing.get("prepaid_balance"),
        "grok_cli_quota_updated_at": billing.get("updated_at"),
        "grok_bot_percent": bot.get("percent"),
        "grok_bot_resets_at": bot.get("resets_at"),
        "grok_bot_period_percent": bot.get("period_percent"),
        "grok_bot_period_resets_at": bot.get("period_resets_at"),
        "grok_bot_available": bot.get("available"),
        "grok_sessions_24h": cli.get("sessions_24h"),
        "grok_tokens_24h": cli.get("tokens_24h"),
        "grok_model_calls_24h": cli.get("model_calls_24h"),
        "updated_at": time.time(),
    }


if __name__ == "__main__":
    print(json.dumps(read_status(), indent=2, ensure_ascii=False))
