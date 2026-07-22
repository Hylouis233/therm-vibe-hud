import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "usage_history.sqlite3"
# Snapshotting every push_loop tick (1.5s) would bloat the DB for no benefit —
# trend/prediction only care about hour-scale movement, so throttle writes.
SNAPSHOT_INTERVAL_SEC = 5 * 60
RETENTION_SEC = 7 * 24 * 3600

_last_snapshot_at = 0.0

# status-dict key -> (tool name it belongs to, metric name stored in the DB)
_AGENT_METRICS = (
    "usage_percent", "usage_seven_day_percent", "context_percent", "secondary_percent",
    "cache_hit_percent", "zcode_token_percent", "zcode_request_percent",
)
_HW_METRICS = ("cpu_usage", "mem_percent", "swap_used_gb")


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS snapshots (ts REAL, tool TEXT, metric TEXT, value REAL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_lookup ON snapshots(tool, metric, ts)")
    return conn


def record_all(agent_statuses, hw_status):
    """Throttled snapshot of every numeric usage/hardware metric currently
    on screen, keyed by tool+metric so each bar/stat can later pull its own
    trend line back out."""
    global _last_snapshot_at
    now = time.time()
    if now - _last_snapshot_at < SNAPSHOT_INTERVAL_SEC:
        return
    _last_snapshot_at = now

    rows = []
    for status in agent_statuses:
        tool = status.get("tool")
        if not tool:
            continue
        for metric in _AGENT_METRICS:
            value = status.get(metric)
            if value is not None:
                rows.append((now, tool, metric, float(value)))
    for metric in _HW_METRICS:
        value = hw_status.get(metric)
        if value is not None:
            rows.append((now, "Hardware", metric, float(value)))
    if not rows:
        return

    conn = _conn()
    try:
        with conn:
            conn.executemany("INSERT INTO snapshots VALUES (?, ?, ?, ?)", rows)
            conn.execute("DELETE FROM snapshots WHERE ts < ?", (now - RETENTION_SEC,))
    finally:
        conn.close()


def series(tool, metric, since_sec):
    conn = _conn()
    try:
        cur = conn.execute(
            "SELECT ts, value FROM snapshots WHERE tool=? AND metric=? AND ts > ? ORDER BY ts",
            (tool, metric, time.time() - since_sec),
        )
        return cur.fetchall()
    finally:
        conn.close()


def recent_values(tool, metric, hours=3, max_points=24):
    """Downsampled value list (oldest to newest) for a small sparkline."""
    rows = series(tool, metric, since_sec=hours * 3600)
    values = [v for _, v in rows]
    if len(values) > max_points:
        step = len(values) / max_points
        values = [values[int(i * step)] for i in range(max_points)]
    return values


def rate_per_hour(tool, metric, window_sec=3 * 3600):
    """Linear rate of change over the trailing window, in %/hour. None
    without at least two points spread over a meaningful span."""
    rows = series(tool, metric, since_sec=window_sec)
    if len(rows) < 2:
        return None
    (t0, v0), (t1, v1) = rows[0], rows[-1]
    dt_hours = (t1 - t0) / 3600
    if dt_hours < 0.1:
        return None
    return (v1 - v0) / dt_hours


if __name__ == "__main__":
    import sys

    print(f"db: {DB_PATH}")
    conn = _conn()
    try:
        for tool, metric, n in conn.execute(
            "SELECT tool, metric, COUNT(*) FROM snapshots GROUP BY tool, metric ORDER BY tool, metric"
        ):
            print(f"  {tool:12s} {metric:24s} {n} points")
    finally:
        conn.close()
