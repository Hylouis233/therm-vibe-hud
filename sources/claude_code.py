import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources import pricing  # noqa: E402
from sources.background_cache import BackgroundCache  # noqa: E402

PROJECTS_DIR = Path.home() / ".claude" / "projects"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
IDLE_THRESHOLD_SEC = 45
ACTIVE_WINDOW_SEC = 30 * 60  # a session stops counting as a "parallel thread" after this
MAX_SESSIONS = 6
TAIL_LINES = 60
PROXY_STATUS_TIMEOUT_SEC = 30
PROXY_STATUS_CACHE_SEC = 15
PROXY_RESPONSE_MAX_BYTES = 512 * 1024
# Cache maintained by the user's own active statusLine (OMC's hud) after every
# interaction — reading it is passive, no extra Anthropic API/Keychain calls.
USAGE_CACHE_PATH = Path.home() / ".claude" / "plugins" / "oh-my-claudecode" / ".usage-cache.json"

LIFETIME_CACHE_TTL_SEC = 5 * 60

STATE_PRIORITY = {"running": 2, "thinking": 1, "idle": 0}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_no_redirect_opener = urllib.request.build_opener(_NoRedirectHandler)


def _load_proxy_config():
    try:
        settings = json.loads(SETTINGS_PATH.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        settings = {}
    settings_env = settings.get("env") if isinstance(settings, dict) else {}
    if not isinstance(settings_env, dict):
        settings_env = {}

    base_url = os.environ.get("ANTHROPIC_BASE_URL") or settings_env.get(
        "ANTHROPIC_BASE_URL"
    )
    token = (
        os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
        or settings_env.get("ANTHROPIC_AUTH_TOKEN")
        or settings_env.get("ANTHROPIC_API_KEY")
    )
    if not isinstance(base_url, str) or not base_url.strip():
        return None

    parsed = urllib.parse.urlsplit(base_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return {
        "base_url": base_url.strip().rstrip("/"),
        "token": token.strip() if isinstance(token, str) and token.strip() else None,
    }


def _is_local_proxy_host(hostname):
    hostname = (hostname or "").strip().lower()
    if hostname in ("localhost", "localhost.localdomain") or hostname.endswith(
        (".local", ".ts.net")
    ):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    if address.is_loopback or address.is_private:
        return True
    return address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10")


def _proxy_urls(base_url):
    parsed = urllib.parse.urlsplit(base_url)
    origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    path = parsed.path.rstrip("/")
    models_path = f"{path}/models" if path.endswith("/v1") else f"{path}/v1/models"
    return f"{origin}/healthz", f"{origin}{models_path}"


def _read_proxy_json(url, headers=None):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", **(headers or {})},
    )
    with _no_redirect_opener.open(
        request, timeout=PROXY_STATUS_TIMEOUT_SEC
    ) as response:
        payload = response.read(PROXY_RESPONSE_MAX_BYTES + 1)
        if len(payload) > PROXY_RESPONSE_MAX_BYTES:
            raise ValueError("proxy response too large")
        return response.status, json.loads(payload)


def _fetch_proxy_status():
    config = _load_proxy_config()
    if not config:
        return {}

    parsed = urllib.parse.urlsplit(config["base_url"])
    if not _is_local_proxy_host(parsed.hostname):
        return {}

    health_url, models_url = _proxy_urls(config["base_url"])
    try:
        health_status, health = _read_proxy_json(health_url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        return {
            "health": "offline",
            "data_source": "proxy-endpoint",
            "proxy_error": f"http-{exc.code}",
        }
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
        return {
            "health": "offline",
            "data_source": "proxy-endpoint",
            "proxy_error": "unreachable",
        }

    if health_status != 200 or not isinstance(health, dict) or health.get("status") != "ok":
        return {
            "health": "offline",
            "data_source": "proxy-endpoint",
            "proxy_error": "unhealthy",
        }

    token = config.get("token")
    if not token:
        return {
            "health": "online",
            "data_source": "proxy-endpoint",
            "proxy_auth": "unverified",
        }

    try:
        models_status, payload = _read_proxy_json(
            models_url, {"Authorization": f"Bearer {token}"}
        )
    except urllib.error.HTTPError as exc:
        return {
            "health": "offline",
            "data_source": "proxy-endpoint",
            "proxy_error": "authentication" if exc.code in (401, 403) else f"http-{exc.code}",
        }
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
        return {
            "health": "offline",
            "data_source": "proxy-endpoint",
            "proxy_error": "models-unavailable",
        }

    models = payload.get("data") if isinstance(payload, dict) else None
    if models_status != 200 or not isinstance(models, list):
        return {
            "health": "offline",
            "data_source": "proxy-endpoint",
            "proxy_error": "invalid-models-response",
        }
    model_ids = [
        item.get("id")
        for item in models
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    claude_model_count = sum(model_id.startswith("claude-") for model_id in model_ids)
    return {
        "health": "online" if claude_model_count else "offline",
        "data_source": "proxy-endpoint",
        "proxy_auth": "verified",
        "proxy_model_count": len(model_ids),
        "proxy_claude_model_count": claude_model_count,
        "proxy_error": None if claude_model_count else "no-claude-models",
    }


_proxy_status_cache = BackgroundCache(_fetch_proxy_status, PROXY_STATUS_CACHE_SEC)


def _proxy_status():
    return _proxy_status_cache.get() or None


def _compute_lifetime_stats():
    # A full scan of history plus a pricing lookup can run into the seconds —
    # runs on a background timer (see BackgroundCache below), never inline on
    # the push_loop tick path.
    total_tokens = 0
    session_count = 0
    # Bucketed by model since a lifetime of history can span model changes,
    # and each model has its own $/token rate.
    per_model = {}
    for f in PROJECTS_DIR.rglob("*.jsonl"):
        session_count += 1
        try:
            with open(f, "rb") as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    msg = obj.get("message")
                    if not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    input_t = usage.get("input_tokens") or 0
                    output_t = usage.get("output_tokens") or 0
                    cache_creation = usage.get("cache_creation_input_tokens") or 0
                    cache_read = usage.get("cache_read_input_tokens") or 0
                    total_tokens += input_t + cache_creation + output_t
                    model = msg.get("model") or "unknown"
                    bucket = per_model.setdefault(model, {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0})
                    bucket["input"] += input_t
                    bucket["output"] += output_t
                    bucket["cache_write"] += cache_creation
                    bucket["cache_read"] += cache_read
        except OSError:
            continue

    pricing.refresh([m for m in per_model if m != "unknown"])
    cost_usd = 0.0
    priced_any = False
    for model, b in per_model.items():
        c = pricing.estimate_cost_usd(model, b["input"], b["output"], b["cache_read"], b["cache_write"])
        if c is not None:
            cost_usd += c
            priced_any = True
    cost_usd = cost_usd if priced_any else None

    return total_tokens, session_count, cost_usd


_lifetime_cache = BackgroundCache(_compute_lifetime_stats, LIFETIME_CACHE_TTL_SEC)


def _lifetime_stats():
    return _lifetime_cache.get() or (None, None, None)


def _read_usage():
    try:
        cache = json.loads(USAGE_CACHE_PATH.read_text())
    except (OSError, ValueError):
        return None, None, None
    if cache.get("error") or not cache.get("data"):
        return None, None, None
    data = cache["data"]
    five_hour = data.get("five_hour") or {}
    seven_day = data.get("seven_day") or {}
    return five_hour.get("utilization"), seven_day.get("utilization"), five_hour.get("resets_at")


def _recent_transcripts():
    now = time.time()
    candidates = []
    for f in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if now - mtime < ACTIVE_WINDOW_SEC:
            candidates.append((f, mtime))
    candidates.sort(key=lambda x: -x[1])
    return candidates[:MAX_SESSIONS]


def _tail_lines(path, n):
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        block = 4096
        chunks = []
        newline_count = 0
        pos = size
        while newline_count <= n and pos > 0:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            chunk = f.read(step)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
    data = b"".join(reversed(chunks))
    return data.decode("utf-8", "ignore").splitlines()[-n:]


def _parse_session(path, mtime):
    age = time.time() - mtime

    events = []
    for line in _tail_lines(path, TAIL_LINES):
        try:
            events.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue

    pending_tools = {}
    project = None
    context_tokens = None
    cache_hit_percent = None
    last_assistant_response_at = None
    model = None
    for e in events:
        cwd = e.get("cwd")
        if cwd:
            project = cwd
        msg = e.get("message")
        if not isinstance(msg, dict):
            continue
        if msg.get("model"):
            model = msg["model"]
        usage = msg.get("usage")
        if isinstance(usage, dict):
            input_t = usage.get("input_tokens") or 0
            cache_creation = usage.get("cache_creation_input_tokens") or 0
            cache_read = usage.get("cache_read_input_tokens") or 0
            total = input_t + cache_creation + cache_read
            if total:
                context_tokens = total
                cache_hit_percent = cache_read / total * 100
                # Anthropic's prompt cache is keyed off wall-clock time since the
                # last turn that actually populated it, not this session's own
                # idle timer — a usage-bearing turn is always an assistant reply,
                # so its own event timestamp is exactly that reference point.
                ts = e.get("timestamp")
                if ts:
                    try:
                        last_assistant_response_at = datetime.fromisoformat(
                            str(ts).replace("Z", "+00:00")).timestamp()
                    except (ValueError, TypeError):
                        pass
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                pending_tools[block.get("id")] = block.get("name", "tool")
            elif btype == "tool_result":
                pending_tools.pop(block.get("tool_use_id"), None)

    if age > IDLE_THRESHOLD_SEC:
        state, detail = "idle", "waiting for input"
    elif pending_tools:
        state, detail = "running", list(pending_tools.values())[-1]
    else:
        state, detail = "thinking", ""

    return {
        "state": state,
        "detail": detail,
        "project": (project or "").replace(str(Path.home()), "~"),
        "updated_at": mtime,
        "context_tokens": context_tokens,
        "cache_hit_percent": cache_hit_percent,
        "last_assistant_response_at": last_assistant_response_at,
        "model": model,
    }


def read_status():
    usage_percent, usage_seven_day_percent, usage_resets_at = _read_usage()
    lifetime_total_tokens, lifetime_session_count, lifetime_cost_usd = _lifetime_stats()
    proxy_status = _proxy_status()
    base = {
        "tool": "Claude Code",
        "usage_percent": usage_percent,
        "usage_seven_day_percent": usage_seven_day_percent,
        "usage_resets_at": usage_resets_at,
        "lifetime_total_tokens": lifetime_total_tokens,
        "lifetime_session_count": lifetime_session_count,
        "lifetime_cost_usd": lifetime_cost_usd,
        "usage_source": "local-cache" if usage_percent is not None else "unavailable",
    }
    if proxy_status is not None:
        base.update(proxy_status)

    files = _recent_transcripts()
    if not files:
        state = "idle" if base.get("health") == "online" else "no session"
        return {**base, "state": state, "sessions": [], "active_count": 0,
                "context_tokens": None, "cache_hit_percent": None,
                "last_assistant_response_at": None, "identity": None}

    parsed = [_parse_session(f, mtime) for f, mtime in files]

    # Local, always-available substitute for when usage_percent is unreachable
    # (e.g. a custom ANTHROPIC_BASE_URL with no real Anthropic usage API behind it):
    # context size of whichever session most recently carried a usage event.
    context_tokens = cache_hit_percent = last_assistant_response_at = identity = None
    for s in sorted(parsed, key=lambda s: -s["updated_at"]):
        if s["context_tokens"] is not None:
            context_tokens = s["context_tokens"]
            cache_hit_percent = s["cache_hit_percent"]
            last_assistant_response_at = s["last_assistant_response_at"]
        if identity is None and s.get("model"):
            identity = s["model"]
        if context_tokens is not None and identity is not None:
            break

    rows = [{"state": s["state"], "detail": s["detail"], "project": s["project"], "updated_at": s["updated_at"]}
            for s in parsed]
    rows.sort(key=lambda s: (-STATE_PRIORITY.get(s["state"], 0), -s["updated_at"]))
    active_count = sum(1 for s in rows if s["state"] in ("running", "thinking"))
    aggregate_state = rows[0]["state"] if rows else "no session"
    if base.get("health") == "offline":
        rows = []
        active_count = 0
        aggregate_state = "no session"

    return {**base, "state": aggregate_state, "sessions": rows, "active_count": active_count, "identity": identity,
            "context_tokens": context_tokens, "cache_hit_percent": cache_hit_percent,
            "last_assistant_response_at": last_assistant_response_at}


if __name__ == "__main__":
    print(json.dumps(read_status(), indent=2, ensure_ascii=False))
