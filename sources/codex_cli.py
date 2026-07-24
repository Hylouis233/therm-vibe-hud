import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources import pricing  # noqa: E402
from sources.background_cache import BackgroundCache  # noqa: E402

SESSIONS_DIR = Path.home() / ".codex" / "sessions"
AUTH_PATH = Path.home() / ".codex" / "auth.json"
IDLE_THRESHOLD_SEC = 45
ACTIVE_WINDOW_SEC = 30 * 60
MAX_SESSIONS = 6
TAIL_LINES = 80
# Rollout lines can embed huge tool output/diffs (seen up to 100MB+ files) —
# a few hundred *lines* can mean tens of MB of reverse-seek I/O per file, so
# the lifetime scan bounds by bytes instead: one seek + one read per file,
# regardless of how huge any individual line is.
LIFETIME_TAIL_BYTES = 256 * 1024
LIFETIME_CACHE_TTL_SEC = 5 * 60

# Same live endpoint CodexBar itself calls (Sources/CodexBarCore/Providers/Codex/CodexOAuth/
# CodexOAuthUsageFetcher.swift) instead of ever reading it out of a rollout file. The primary
# rate-limit window is a continuously-recalculated rolling window server-side, so a snapshot
# scanned from an old session file can be tens of points stale by the time anyone looks at it.
CHATGPT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
OAUTH_REFRESH_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
LIVE_QUOTA_FETCH_TIMEOUT_SEC = 10

STATE_PRIORITY = {"running": 2, "thinking": 1, "idle": 0}


def _recent_rollouts():
    now = time.time()
    files = glob.glob(str(SESSIONS_DIR / "**" / "rollout-*.jsonl"), recursive=True)
    candidates = []
    for f in files:
        try:
            mtime = os.path.getmtime(f)
        except OSError:
            continue
        if now - mtime < ACTIVE_WINDOW_SEC:
            candidates.append((Path(f), mtime))
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

    project = None
    pending_calls = {}
    turn_open = False
    last_agent_message = None
    usage_percent = None
    usage_resets_at = None
    context_percent = None
    cache_hit_percent = None
    model = None

    for e in events:
        etype = e.get("type")
        payload = e.get("payload")
        if not isinstance(payload, dict):
            continue

        if etype == "session_meta":
            project = payload.get("cwd") or project
        elif etype == "turn_context":
            project = payload.get("cwd") or project
            model = payload.get("model") or model
        elif etype == "event_msg":
            ptype = payload.get("type")
            if ptype == "task_started":
                turn_open = True
            elif ptype == "task_complete":
                turn_open = False
                last_agent_message = payload.get("last_agent_message") or last_agent_message
            elif ptype == "token_count":
                rate_limits = payload.get("rate_limits") or {}
                primary = rate_limits.get("primary")
                if primary:
                    usage_percent = primary.get("used_percent")
                    usage_resets_at = primary.get("resets_at")
                info = payload.get("info")
                if info:
                    window = info.get("model_context_window")
                    # total_token_usage is cumulative for the whole session (grows every
                    # turn, blows past the window almost immediately on a long session);
                    # last_token_usage is this turn's actual request size, i.e. the real
                    # current context-window fill.
                    last = info.get("last_token_usage") or {}
                    total = last.get("total_tokens")
                    if window and total is not None:
                        context_percent = min(100.0, total / window * 100)
                    # cached_input_tokens is a subset of input_tokens (OpenAI-style
                    # reporting), so this is directly "share of this turn's input
                    # that was served from cache" — same concept as Claude Code's
                    # cache_hit_percent, different underlying convention.
                    input_t = last.get("input_tokens")
                    if input_t:
                        cache_hit_percent = (last.get("cached_input_tokens") or 0) / input_t * 100
        elif etype == "response_item":
            ptype = payload.get("type")
            if ptype in ("function_call", "custom_tool_call"):
                pending_calls[payload.get("call_id")] = payload.get("name", "tool")
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                pending_calls.pop(payload.get("call_id"), None)

    if age > IDLE_THRESHOLD_SEC:
        state, detail = "idle", "waiting for input"
    elif pending_calls:
        state, detail = "running", list(pending_calls.values())[-1]
    elif turn_open:
        state, detail = "thinking", ""
    else:
        state, detail = "idle", (last_agent_message or "")[:60]

    return {
        "state": state,
        "detail": detail,
        "project": (project or "").replace(str(Path.home()), "~"),
        "updated_at": mtime,
        "usage_percent": usage_percent,
        "usage_resets_at": usage_resets_at,
        "context_percent": context_percent,
        "cache_hit_percent": cache_hit_percent,
        "model": model,
    }


def _tail_bytes(path, n_bytes):
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - n_bytes))
        data = f.read()
    return data.decode("utf-8", "ignore").splitlines()


def _compute_lifetime_stats():
    files = glob.glob(str(SESSIONS_DIR / "**" / "rollout-*.jsonl"), recursive=True)
    total_tokens = 0
    session_count = 0
    per_model = {}
    for f in files:
        session_count += 1
        model = None
        last_usage = None
        try:
            lines = _tail_bytes(f, LIFETIME_TAIL_BYTES)
        except OSError:
            continue
        for line in lines:
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            if obj.get("type") == "turn_context" and payload.get("model"):
                model = payload["model"]
            elif obj.get("type") == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info")
                if info and info.get("total_token_usage"):
                    last_usage = info["total_token_usage"]

        if not last_usage:
            continue
        # total_token_usage is already cumulative for the whole session, so
        # the last event seen fully represents this file's lifetime contribution.
        total_tokens += last_usage.get("total_tokens") or 0
        bucket = per_model.setdefault(model or "unknown", {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0})
        # cached_input_tokens is a subset of input_tokens (OpenAI-style reporting,
        # unlike Anthropic's additive input/cache_read split) — subtract it out of
        # the input bucket or estimate_cost_usd double-charges the cached portion
        # once at full input price and again at cache_read price.
        input_t = last_usage.get("input_tokens") or 0
        cache_read_t = last_usage.get("cached_input_tokens") or 0
        bucket["input"] += input_t - cache_read_t
        bucket["output"] += last_usage.get("output_tokens") or 0
        bucket["cache_write"] += last_usage.get("cache_write_input_tokens") or 0
        bucket["cache_read"] += cache_read_t

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


LAST_KNOWN_QUOTA_SCAN_FILES = 20
LAST_KNOWN_QUOTA_TTL_SEC = 60
# A brief live-fetch hiccup (network blip, timeout, transient 5xx) shouldn't
# yank the displayed % back to a rollout-file snapshot that could be far
# staler than the outage itself — bridge short gaps with the last value that
# really did come from the live account-wide endpoint.
LAST_LIVE_QUOTA_MAX_AGE_SEC = 15 * 60
# The rate_limits.primary.used_percent embedded in an old rollout file's last
# token_count event is a point-in-time snapshot, not a live read. Past this
# age, usage has almost certainly moved on enough that presenting it as
# current would actively mislead rather than help — the fallback scan stops
# trusting it as a usage_percent source (though it still trusts old files for
# last-used *model*, which doesn't go stale the same way).
ROLLOUT_QUOTA_STALE_SEC = 3 * 3600


def _load_auth_tokens():
    try:
        data = json.loads(AUTH_PATH.read_text())
    except (OSError, ValueError):
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access_token = tokens.get("access_token")
    if not access_token:
        return None
    return access_token, tokens.get("refresh_token"), tokens.get("account_id")


def _fetch_usage_once(access_token, account_id):
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "therm-vibe-hud",
        "Accept": "application/json",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    req = urllib.request.Request(CHATGPT_USAGE_URL, headers=headers)
    with urllib.request.urlopen(req, timeout=LIVE_QUOTA_FETCH_TIMEOUT_SEC) as resp:
        return json.loads(resp.read())


def _refresh_access_token(refresh_token):
    body = json.dumps({
        "client_id": OAUTH_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": "openid profile email",
    }).encode()
    req = urllib.request.Request(
        OAUTH_REFRESH_URL, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=LIVE_QUOTA_FETCH_TIMEOUT_SEC) as resp:
        payload = json.loads(resp.read())
    return payload.get("access_token")


def _fetch_live_quota():
    """Same live endpoint CodexBar itself calls — a real-time read of the
    account's actual rolling-window usage, not a point-in-time value baked
    into an old rollout file. Never touches auth.json; refreshed tokens are
    only kept in memory for this process, so this stays strictly read-only
    against Codex CLI's own credential store."""
    auth = _load_auth_tokens()
    if not auth:
        return None
    access_token, refresh_token, account_id = auth

    for attempt in range(2):
        try:
            payload = _fetch_usage_once(access_token, account_id)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and refresh_token and attempt == 0:
                try:
                    new_token = _refresh_access_token(refresh_token)
                except (urllib.error.URLError, OSError, ValueError) as refresh_exc:
                    print(f"[codex_cli] token refresh failed: {refresh_exc}", file=sys.stderr)
                    return None
                if not new_token:
                    print("[codex_cli] token refresh returned no access_token", file=sys.stderr)
                    return None
                access_token = new_token
                continue
            # Failure reason only — never log headers/tokens/account_id, which
            # could carry the account email downstream.
            print(f"[codex_cli] live quota fetch failed: HTTP {exc.code}", file=sys.stderr)
            return None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"[codex_cli] live quota fetch failed: {exc}", file=sys.stderr)
            return None

        rate_limit = payload.get("rate_limit") or {}
        primary = rate_limit.get("primary_window") or {}
        used_percent = primary.get("used_percent")
        if used_percent is None:
            print("[codex_cli] live quota response missing primary_window.used_percent", file=sys.stderr)
            return None
        secondary = rate_limit.get("secondary_window") or {}
        credits = payload.get("credits") or {}
        credits_balance = credits.get("balance")
        try:
            credits_balance = float(credits_balance) if credits_balance not in (None, "") else None
        except (TypeError, ValueError):
            credits_balance = None
        return {
            "usage_percent": float(used_percent),
            "usage_resets_at": primary.get("reset_at"),
            "plan_type": payload.get("plan_type"),
            "secondary_percent": secondary.get("used_percent"),
            "secondary_resets_at": secondary.get("reset_at"),
            "credits_balance": credits_balance,
            "credits_unlimited": bool(credits.get("unlimited")),
        }

    return None


def _scan_rollouts_for_last_known(scan_model_only=False, known_resets_at=None):
    """Fallback source, and the only source for last-used model identity and
    cache-hit % — neither the live usage endpoint nor an idle session (with
    no recent token_count event of its own) exposes them.

    known_resets_at, when available, is the resets_at the live endpoint most
    recently confirmed for the account's real window. Concurrent sessions
    (e.g. sub-agents spawned together) can carry a differently-scoped pool
    that also reports limit_id "codex" but a resets_at up to a day off from
    the real one — limit_id alone isn't a reliable pool identity, so a known
    resets_at, when we have one, is the stronger match to require."""
    files = glob.glob(str(SESSIONS_DIR / "**" / "rollout-*.jsonl"), recursive=True)
    candidates = []
    for f in files:
        try:
            mtime = os.path.getmtime(f)
        except OSError:
            continue
        candidates.append((f, mtime))
    candidates.sort(key=lambda x: -x[1])

    # model, usage_percent, and cache_hit_percent are tracked independently
    # across the scan (first i.e. most-recent hit for each, since candidates
    # are sorted newest-first) — a value rejected for being past
    # ROLLOUT_QUOTA_STALE_SEC must not also discard a perfectly good model
    # identity (or a perfectly good OTHER value) from that same file.
    now = time.time()
    model = usage_percent = usage_resets_at = cache_hit_percent = None
    for f, mtime in candidates[:LAST_KNOWN_QUOTA_SCAN_FILES]:
        try:
            lines = _tail_bytes(f, LIFETIME_TAIL_BYTES)
        except OSError:
            continue
        file_model = file_usage_percent = file_usage_resets_at = file_cache_hit_percent = None
        for line in lines:
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            if obj.get("type") == "turn_context" and payload.get("model"):
                file_model = payload["model"]
            elif obj.get("type") == "event_msg" and payload.get("type") == "token_count":
                if not scan_model_only:
                    rate_limits = payload.get("rate_limits") or {}
                    primary = rate_limits.get("primary")
                    # Codex tracks separate quota pools per model variant (e.g. a
                    # side session on "GPT-5.3-Codex-Spark" has its own, nearly-
                    # untouched pool). Only limit_id "codex" is the account-wide
                    # pool the live API and the rest of this trend track — a
                    # different pool's % would look like a bogus jump/drop if
                    # trusted here just because its file happened to be newest.
                    candidate_resets_at = primary.get("resets_at") if primary else None
                    if (primary and rate_limits.get("limit_id") == "codex"
                            and (known_resets_at is None or candidate_resets_at == known_resets_at)):
                        file_usage_percent = primary.get("used_percent")
                        file_usage_resets_at = candidate_resets_at
                # Cache-hit % is never live-fetched (unlike usage_percent, it
                # has no account-wide API source at all) so this scan is its
                # ONLY fallback tier — always attempted, even when
                # scan_model_only skips the usage_percent extraction above.
                last = (payload.get("info") or {}).get("last_token_usage") or {}
                input_t = last.get("input_tokens")
                if input_t:
                    file_cache_hit_percent = (last.get("cached_input_tokens") or 0) / input_t * 100

        if model is None and file_model:
            model = file_model
        if (usage_percent is None and file_usage_percent is not None
                and now - mtime <= ROLLOUT_QUOTA_STALE_SEC):
            usage_percent, usage_resets_at = file_usage_percent, file_usage_resets_at
        if (cache_hit_percent is None and file_cache_hit_percent is not None
                and now - mtime <= ROLLOUT_QUOTA_STALE_SEC):
            cache_hit_percent = file_cache_hit_percent

        if model and cache_hit_percent is not None and (scan_model_only or usage_percent is not None):
            break

    return usage_percent, usage_resets_at, model, cache_hit_percent


_EMPTY_QUOTA = {
    "usage_percent": None, "usage_resets_at": None, "model": None, "plan_type": None,
    "secondary_percent": None, "secondary_resets_at": None, "credits_balance": None, "credits_unlimited": False,
    "cache_hit_percent": None,
}


_last_live_quota = None  # (quota_dict, fetched_at) from the most recent successful live fetch


def _compute_last_known_quota():
    """Account-wide rate limit % persists across sessions — a session that's
    now idle/closed shouldn't blank this out. Unlike context_percent (a
    specific session's context-window fill, which genuinely has no meaning
    once that session isn't active), quota is looked up live first (real
    current value, plus plan/secondary-window/credits that only the live API
    exposes), falling back to the last rollout snapshot only if the live call
    can't be made (offline, no stored auth, etc.)."""
    global _last_live_quota
    live = _fetch_live_quota()
    now = time.time()
    if live is not None:
        _last_live_quota = (live, now)
    elif _last_live_quota is not None and now - _last_live_quota[1] <= LAST_LIVE_QUOTA_MAX_AGE_SEC:
        # This cycle's live call failed, but we had a good one recently enough
        # to trust — reuse it rather than jumping to a rollout-file snapshot
        # that's very likely staler than this brief outage.
        live = _last_live_quota[0]
    else:
        print("[codex_cli] live quota fetch failed with no recent cached value "
              "— falling back to rollout-file scan", file=sys.stderr)

    # The window boundary itself (unlike the % within it) stays valid far
    # longer than LAST_LIVE_QUOTA_MAX_AGE_SEC — reuse it as a pool-identity
    # check even when _last_live_quota is too stale to trust its % directly.
    known_resets_at = _last_live_quota[0]["usage_resets_at"] if _last_live_quota is not None else None
    _fallback_percent, fallback_resets_at, model, fallback_cache_hit = _scan_rollouts_for_last_known(
        scan_model_only=live is not None, known_resets_at=known_resets_at)
    if live is not None:
        return {**_EMPTY_QUOTA, **live, "model": model, "cache_hit_percent": fallback_cache_hit}
    return {**_EMPTY_QUOTA, "usage_percent": _fallback_percent, "usage_resets_at": fallback_resets_at,
            "model": model, "cache_hit_percent": fallback_cache_hit}


_quota_cache = BackgroundCache(_compute_last_known_quota, LAST_KNOWN_QUOTA_TTL_SEC)


def _last_known_quota():
    return _quota_cache.get() or _EMPTY_QUOTA


def read_status():
    files = _recent_rollouts()
    lifetime_total_tokens, lifetime_session_count, lifetime_cost_usd = _lifetime_stats()
    quota = _last_known_quota()
    last_usage_percent, last_usage_resets_at, last_model, last_cache_hit_percent = (
        quota["usage_percent"], quota["usage_resets_at"], quota["model"], quota["cache_hit_percent"]
    )
    base = {
        "tool": "Codex",
        "lifetime_total_tokens": lifetime_total_tokens,
        "lifetime_session_count": lifetime_session_count,
        "lifetime_cost_usd": lifetime_cost_usd,
        "plan_type": quota["plan_type"],
        "secondary_percent": quota["secondary_percent"],
        "secondary_resets_at": quota["secondary_resets_at"],
        "credits_balance": quota["credits_balance"],
        "credits_unlimited": quota["credits_unlimited"],
    }
    if not files:
        return {**base, "state": "no session", "sessions": [], "active_count": 0,
                "usage_percent": last_usage_percent, "usage_resets_at": last_usage_resets_at,
                "context_percent": None, "cache_hit_percent": last_cache_hit_percent, "identity": last_model}

    sessions = []
    for f, mtime in files:
        parsed = _parse_session(f, mtime)
        sessions.append(parsed)

    # rate_limits are account-wide, not per-session — take them from whichever
    # parsed session actually carried a token_count event most recently.
    usage_percent = usage_resets_at = context_percent = cache_hit_percent = None
    for s in sorted(sessions, key=lambda s: -s["updated_at"]):
        if s["usage_percent"] is not None:
            usage_percent = s["usage_percent"]
            usage_resets_at = s["usage_resets_at"]
            context_percent = s["context_percent"]
            cache_hit_percent = s["cache_hit_percent"]
            break

    if usage_percent is None:
        usage_percent, usage_resets_at = last_usage_percent, last_usage_resets_at
    if cache_hit_percent is None:
        cache_hit_percent = last_cache_hit_percent

    identity = next((s["model"] for s in sorted(sessions, key=lambda s: -s["updated_at"]) if s.get("model")), None)
    if identity is None:
        identity = last_model

    rows = [{"state": s["state"], "detail": s["detail"], "project": s["project"], "updated_at": s["updated_at"]}
            for s in sessions]
    rows.sort(key=lambda s: (-STATE_PRIORITY.get(s["state"], 0), -s["updated_at"]))
    active_count = sum(1 for s in rows if s["state"] in ("running", "thinking"))
    aggregate_state = rows[0]["state"] if rows else "no session"

    return {
        **base,
        "state": aggregate_state,
        "sessions": rows,
        "active_count": active_count,
        "usage_percent": usage_percent,
        "usage_resets_at": usage_resets_at,
        "context_percent": context_percent,
        "cache_hit_percent": cache_hit_percent,
        "identity": identity,
    }


if __name__ == "__main__":
    print(json.dumps(read_status(), indent=2, ensure_ascii=False))
