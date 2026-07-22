import os
import re
import shutil
import subprocess
import time

TRCC_BIN = "/Applications/TRCC.app/Contents/MacOS/TRCC"
LINE_RE = re.compile(r"^\s*([\w:]+)\s+([\d.]+)\s*(\S*)")

# net:total_up/down are cumulative MB counters; diff across ticks to get a
# real-time rate. Module-level state persists across calls within the
# long-running push_loop process.
_prev_net = None  # (timestamp, total_up_mb, total_down_mb)


def _env():
    env = os.environ.copy()
    env["SSL_CERT_FILE"] = "/etc/ssl/cert.pem"
    return env


_BOOTTIME_RE = re.compile(r"sec\s*=\s*(\d+)")
_SWAP_RE = re.compile(r"total\s*=\s*([\d.]+)M\s+used\s*=\s*([\d.]+)M")
_VM_PAGESIZE_RE = re.compile(r"page size of (\d+) bytes")


def _uptime_sec():
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5).stdout
        m = _BOOTTIME_RE.search(out)
        return time.time() - int(m.group(1)) if m else None
    except (subprocess.SubprocessError, OSError):
        return None


def _swap_usage_gb():
    try:
        out = subprocess.run(["sysctl", "vm.swapusage"], capture_output=True, text=True, timeout=5).stdout
        m = _SWAP_RE.search(out)
        return (float(m.group(2)) / 1024, float(m.group(1)) / 1024) if m else (None, None)
    except (subprocess.SubprocessError, OSError):
        return None, None


def _mem_usage_gb():
    """TRCC's own `memory:used` sensor only counts a narrow subset of pages
    (~9.6GB when the system was actually at ~23GB used) — compute the same
    active+inactive+wired+compressed total macOS's own `top`/Activity Monitor
    report instead, straight from vm_stat + hw.memsize."""
    try:
        vm_out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    except (subprocess.SubprocessError, OSError):
        return None, None, None

    m = _VM_PAGESIZE_RE.search(vm_out)
    page_size = int(m.group(1)) if m else 4096

    pages = {}
    for line in vm_out.splitlines():
        label, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip().rstrip(".")
        if value.isdigit():
            pages[label.strip()] = int(value)

    active = pages.get("Pages active", 0)
    inactive = pages.get("Pages inactive", 0)
    wired = pages.get("Pages wired down", 0)
    compressor = pages.get("Pages occupied by compressor", 0)
    used_gb = (active + inactive + wired + compressor) * page_size / 1024**3

    try:
        total_bytes = int(subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5,
        ).stdout.strip())
        total_gb = total_bytes / 1024**3
    except (subprocess.SubprocessError, OSError, ValueError):
        total_gb = None

    avail_gb = (total_gb - used_gb) if total_gb is not None else None
    return used_gb, total_gb, avail_gb


def _net_rates(total_up, total_down):
    global _prev_net
    now = time.time()
    up_rate = down_rate = None
    if _prev_net is not None and total_up is not None and total_down is not None:
        prev_t, prev_up, prev_down = _prev_net
        dt = now - prev_t
        if dt > 0.1:
            up_rate = max(0.0, (total_up - prev_up) * 1024 / dt)
            down_rate = max(0.0, (total_down - prev_down) * 1024 / dt)
    if total_up is not None and total_down is not None:
        _prev_net = (now, total_up, total_down)
    return up_rate, down_rate


def read_status():
    try:
        result = subprocess.run(
            [TRCC_BIN, "system", "info"],
            capture_output=True,
            text=True,
            env=_env(),
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        result = None

    metrics = {}
    if result is not None:
        for line in result.stdout.splitlines():
            m = LINE_RE.match(line)
            if m:
                metrics[m.group(1)] = float(m.group(2))

    mem_used_gb, mem_total_gb, mem_avail_gb = _mem_usage_gb()
    mem_percent = (mem_used_gb / mem_total_gb * 100) if mem_total_gb else None

    up_rate, down_rate = _net_rates(metrics.get("net:total_up"), metrics.get("net:total_down"))

    try:
        disk = shutil.disk_usage("/")
        disk_total_gb = disk.total / 1024**3
        disk_free_gb = disk.free / 1024**3
        disk_percent = disk.used / disk.total * 100
    except OSError:
        disk_total_gb = disk_free_gb = disk_percent = None

    try:
        load1, load5, _ = os.getloadavg()
    except OSError:
        load1 = load5 = None

    swap_used_gb, swap_total_gb = _swap_usage_gb()

    return {
        "tool": "Hardware",
        "cpu_temp": metrics.get("cpu:temp"),
        "cpu_usage": metrics.get("cpu:usage"),
        "mem_percent": mem_percent,
        "mem_used_gb": mem_used_gb,
        "mem_total_gb": mem_total_gb,
        "mem_avail_gb": mem_avail_gb,
        "fan_rpm": metrics.get("fan:smc:fan0:rpm"),
        "net_up_kbps": up_rate,
        "net_down_kbps": down_rate,
        "net_total_up_gb": (metrics.get("net:total_up") or 0) / 1024 or None,
        "net_total_down_gb": (metrics.get("net:total_down") or 0) / 1024 or None,
        "disk_free_gb": disk_free_gb,
        "disk_total_gb": disk_total_gb,
        "disk_percent": disk_percent,
        "load1": load1,
        "load5": load5,
        "uptime_sec": _uptime_sec(),
        "swap_used_gb": swap_used_gb,
        "swap_total_gb": swap_total_gb,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(read_status(), indent=2))
