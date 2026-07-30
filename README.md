# therm-vibe-hud

A CodexBar-inspired external LCD dashboard for a TRCC-controlled Winbond
Trofeo Vision 9.16 panel (1920×462). It shows live session, subscription,
and quota status for Claude Code, Codex, Kimi Code, GLM, and MiniMax
alongside a hardware monitor — all from real, locally available data,
never fabricated placeholders.

## Panels

- **Claude Code** — active sessions, proxy health when a local/private
  `ANTHROPIC_BASE_URL` is configured, context/cache metrics, and
  lifetime token/cost tracking.
- **Codex** — live desktop task state through the local app server,
  account rate-limit windows through the same OAuth usage API
  [CodexBar](https://github.com/steipete/CodexBar) calls, current
  context/cache metrics, and lifetime totals.
- **Kimi Code** — local daemon/session state, real subscription name,
  and the account's five-hour and weekly usage windows.
- **GLM** — ZCode/GLM desktop state, token/request quota, cache usage,
  and recent local sessions.
- **MiniMax** — MiniMax Code desktop state, Token Plan tier, five-hour
  and weekly limits, plus context and cache metrics from matching local
  sessions.
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
installed — no extra accounts and no credentials committed to this
repository:

- `sources/claude_code.py` tails recent `~/.claude/projects/**/*.jsonl`
  transcripts. If Claude Code uses a loopback, private-LAN, or
  Tailscale proxy, the module may reuse the existing local
  `ANTHROPIC_BASE_URL` and token to verify that proxy's health.
- `sources/codex_cli.py` tails recent `~/.codex/sessions/**/rollout-*.jsonl`
  files, reads desktop task state from Codex's local app server, and
  calls the same live
  `chatgpt.com/backend-api/wham/usage` endpoint CodexBar uses for
  real-time quota, using the token already stored in `~/.codex/auth.json`.
  A refreshed access token (on 401/403) is kept in memory only for the
  running process — this never writes back to `auth.json`.
- `sources/kimi.py` reads Kimi Code's local daemon/session metadata and
  reuses its existing OAuth credentials for official status/usage
  endpoints.
- `sources/zcode.py` reads GLM/ZCode local session SQLite data and
  entitlement cache, and can reuse existing provider credentials for a
  live quota refresh.
- `sources/minimax.py` reads matching Codex/Claude-format local sessions,
  MiniMax Code desktop state, and the official Token Plan remaining
  endpoint using credentials already managed by the installed client.
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
tests/                  source and renderer regression tests
```

Runtime files such as `frame.png`, previews, logs, SQLite history,
pricing caches, quota anchors, and `state.json` are intentionally
ignored by Git.
