import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources import pricing  # noqa: E402
from sources.background_cache import BackgroundCache  # noqa: E402

PROJECTS_DIR = Path.home() / ".claude" / "projects"
IDLE_THRESHOLD_SEC = 45
ACTIVE_WINDOW_SEC = 30 * 60  # a session stops counting as a "parallel thread" after this
MAX_SESSIONS = 6
TAIL_LINES = 60
# Cache maintained by the user's own active statusLine (OMC's hud) after every
# interaction — reading it is passive, no extra Anthropic API/Keychain calls.
USAGE_CACHE_PATH = Path.home() / ".claude" / "plugins" / "oh-my-claudecode" / ".usage-cache.json"

LIFETIME_CACHE_TTL_SEC = 5 * 60

STATE_PRIORITY = {"running": 2, "thinking": 1, "idle": 0}


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
        data = b""
        pos = size
        while data.count(b"\n") <= n and pos > 0:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
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
        "model": model,
    }


def read_status():
    usage_percent, usage_seven_day_percent, usage_resets_at = _read_usage()
    lifetime_total_tokens, lifetime_session_count, lifetime_cost_usd = _lifetime_stats()
    base = {
        "tool": "Claude Code",
        "usage_percent": usage_percent,
        "usage_seven_day_percent": usage_seven_day_percent,
        "usage_resets_at": usage_resets_at,
        "lifetime_total_tokens": lifetime_total_tokens,
        "lifetime_session_count": lifetime_session_count,
        "lifetime_cost_usd": lifetime_cost_usd,
    }

    files = _recent_transcripts()
    if not files:
        return {**base, "state": "no session", "sessions": [], "active_count": 0,
                "context_tokens": None, "cache_hit_percent": None, "identity": None}

    parsed = [_parse_session(f, mtime) for f, mtime in files]

    # Local, always-available substitute for when usage_percent is unreachable
    # (e.g. a custom ANTHROPIC_BASE_URL with no real Anthropic usage API behind it):
    # context size of whichever session most recently carried a usage event.
    context_tokens = cache_hit_percent = identity = None
    for s in sorted(parsed, key=lambda s: -s["updated_at"]):
        if s["context_tokens"] is not None:
            context_tokens = s["context_tokens"]
            cache_hit_percent = s["cache_hit_percent"]
        if identity is None and s.get("model"):
            identity = s["model"]
        if context_tokens is not None and identity is not None:
            break

    rows = [{"state": s["state"], "detail": s["detail"], "project": s["project"], "updated_at": s["updated_at"]}
            for s in parsed]
    rows.sort(key=lambda s: (-STATE_PRIORITY.get(s["state"], 0), -s["updated_at"]))
    active_count = sum(1 for s in rows if s["state"] in ("running", "thinking"))
    aggregate_state = rows[0]["state"] if rows else "no session"

    return {**base, "state": aggregate_state, "sessions": rows, "active_count": active_count, "identity": identity,
            "context_tokens": context_tokens, "cache_hit_percent": cache_hit_percent}


if __name__ == "__main__":
    print(json.dumps(read_status(), indent=2, ensure_ascii=False))
