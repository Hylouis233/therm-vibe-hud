# therm-vibe-hud

A CodexBar-inspired external LCD dashboard for a TRCC-controlled Winbond
Trofeo Vision 9.16 panel (1920×462). Shows live session and quota status
for Claude Code, Codex CLI, and zcode side by side with a hardware
monitor panel — all from real, locally-available data, never fabricated
placeholders.

## Panels

- **Claude Code** — active sessions, 5-hour/7-day quota bars (falls back
  to current context size + cache-hit-rate when no Anthropic quota API
  is reachable, e.g. behind a custom `ANTHROPIC_BASE_URL`), lifetime
  token/cost tracking.
- **Codex** — active sessions, live rate-limit % via the same OAuth
  usage API [CodexBar](https://github.com/steipete/CodexBar) itself
  calls, plan type, weekly window, credits balance, current
  context-window fill, lifetime cost.
- **zcode** — active sessions, token/request quota, which feature is
  driving request usage, today's session count.
- **Hardware** — CPU/memory/disk usage and temperature, fan RPM,
  network throughput, uptime, swap.

Each panel also renders a sparkline trend and a predictive "will exceed
100% before reset at this pace" warning for its quota bars, backed by a
local SQLite history (`sources/history.py`).

## Requirements

- macOS — `sources/hardware.py` reads `vm_stat`/`ioreg`/`sysctl`, which
  are macOS-only.
- [TRCC.app](https://www.trcc-app.com/) installed at
  `/Applications/TRCC.app`, the vendor CLI used to drive the panel.
- A TRCC-supported panel. `DEVICE_KEY = "0416:5408"` in
  `scripts/push_loop.py` and `scripts/theme.py` is this panel's
  vendor:product ID — change it if yours differs.
- Python 3.12+ and Pillow (`pip install pillow`).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pillow

# render one frame to preview.png without touching the device
python3 renderer/render.py

# start pushing frames to the physical panel
python3 scripts/push_loop.py
```

For always-on operation, run it under a launchd agent (`KeepAlive` +
`RunAtLoad`) rather than a foreground shell.

## How it reads data

Every source module is read-only against tools you already have
installed — no daemons, no extra accounts:

- `sources/claude_code.py` tails recent `~/.claude/projects/**/*.jsonl`
  transcripts.
- `sources/codex_cli.py` tails recent `~/.codex/sessions/**/rollout-*.jsonl`
  files for session state, and calls the same live
  `chatgpt.com/backend-api/wham/usage` endpoint CodexBar uses for
  real-time quota, using the token already stored in `~/.codex/auth.json`.
  A refreshed access token (on 401/403) is kept in memory only for the
  running process — this never writes back to `auth.json`.
- `sources/zcode.py` reads zcode's own local session SQLite DB and the
  ZCode desktop app's cached quota snapshot from its Local Storage.
- `sources/hardware.py` shells out to `vm_stat`, `sysctl`, and TRCC's
  own `system info` command.
- `sources/pricing.py` fetches the public [models.dev](https://models.dev)
  pricing catalog to estimate lifetime cost — an estimate, not an
  invoice.

## Layout

```
renderer/render.py     compositing + drawing (panels, bars, sparklines)
sources/                one read_status()-style module per data source
scripts/push_loop.py    the render/push loop that drives the physical panel
scripts/theme.py        switch between this dashboard and TRCC's official themes
assets/backgrounds/     ink-wash background art
```
