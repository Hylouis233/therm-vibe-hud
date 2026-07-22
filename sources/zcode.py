import json
import re
import sqlite3
import time
from pathlib import Path

DB_PATH = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"
# The ZCode desktop app (v2, Electron) polls zcode.z.ai for real quota numbers
# and caches the response snapshot in its own Chromium Local Storage. Reading
# it here is passive: no network calls or credentials of our own are used.
LOCAL_STORAGE_DIR = (
    Path.home() / "Library" / "Application Support" / "ZCode" / "session" / "Local Storage" / "leveldb"
)
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


def read_status():
    entitlement = _entitlement_metrics()
    base = {"tool": "zcode", "identity": entitlement.get("zcode_plan"), **entitlement}
    if not DB_PATH.exists():
        return {**base, "state": "no session", "sessions": [], "active_count": 0,
                "session_tokens": None, "sessions_today": 0}

    now = time.time()
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)
    try:
        cur = con.cursor()
        cutoff_ms = int((now - ACTIVE_WINDOW_SEC) * 1000)
        cur.execute(
            "SELECT id, title, time_updated, directory FROM session "
            "WHERE time_updated >= ? ORDER BY time_updated DESC LIMIT ?",
            (cutoff_ms, MAX_SESSIONS),
        )
        rows = cur.fetchall()

        sessions = []
        latest_tokens = None
        for session_id, title, time_updated_ms, directory in rows:
            mtime = (time_updated_ms or 0) / 1000
            age = now - mtime

            cur.execute(
                "SELECT json_extract(data,'$.role'), json_extract(data,'$.tokens.total') "
                "FROM message WHERE session_id=? ORDER BY id DESC LIMIT 1",
                (session_id,),
            )
            last_row = cur.fetchone()
            last_role = last_row[0] if last_row else None
            session_tokens = last_row[1] if last_row else None
            if latest_tokens is None and session_tokens is not None:
                latest_tokens = session_tokens

            if age > IDLE_THRESHOLD_SEC:
                state, detail = "idle", "waiting for input"
            elif last_role == "assistant":
                state, detail = "thinking", ""
            else:
                state, detail = "running", ""

            sessions.append({
                "state": state,
                "detail": detail or (title or "")[:60],
                "project": (directory or "").replace(str(Path.home()), "~"),
                "updated_at": mtime,
            })

        midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        cur.execute("SELECT COUNT(*) FROM session WHERE time_created >= ?", (int(midnight * 1000),))
        sessions_today = cur.fetchone()[0]
    finally:
        con.close()

    if not sessions:
        return {**base, "state": "no session", "sessions": [], "active_count": 0,
                "session_tokens": None, "sessions_today": sessions_today}

    sessions.sort(key=lambda s: (-STATE_PRIORITY.get(s["state"], 0), -s["updated_at"]))
    active_count = sum(1 for s in sessions if s["state"] in ("running", "thinking"))
    aggregate_state = sessions[0]["state"]

    return {
        **base,
        "state": aggregate_state,
        "sessions": sessions,
        "active_count": active_count,
        "session_tokens": latest_tokens,
        "sessions_today": sessions_today,
    }


if __name__ == "__main__":
    print(json.dumps(read_status(), indent=2, ensure_ascii=False))
