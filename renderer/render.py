import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageStat

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources import history  # noqa: E402

CANVAS_W, CANVAS_H = 1920, 462
ROOT = Path(__file__).resolve().parent.parent
BACKGROUNDS_DIR = ROOT / "assets" / "backgrounds"
BACKGROUND_BRIGHTNESS = {"Background": 0.22}
# How much a card's tint shifts toward the artwork behind it, and how
# see-through the card is over that artwork.
BG_TINT_STRENGTH = 0.48
# Keep only a 20% dark-card layer in wallpaper mode so the artwork remains
# visible; the solid-gradient mode below still uses fully opaque cards.
CARD_ALPHA_ON_BG = round(255 * 0.20)
# Cap how much brightness a sampled backdrop region can contribute before
# blending — a bright sky/mist patch behind a card would otherwise wash the
# tint out toward white and kill text contrast; darker regions (below the
# cap) pass through unclamped so panels still visibly differ from each other.
BG_TINT_MAX_CHANNEL = 118
PANEL_COUNT = 7
PANEL_W = CANVAS_W // PANEL_COUNT
CARD_MARGIN = 14
PAD = 16
HEADER_BADGE_SIZE = 32
HEADER_TITLE_SIZE = 24
HEADER_GAP = 10
MAX_SESSION_ROWS = 4
# The last usage row is always CACHE HIT (see _usage_metrics) and can carry a
# trend sparkline reaching down to bar_y + SPARK_BOT_OFFSET. The lifetime-stats
# footer that immediately follows it must never start higher than that plus a
# real gap — shared here so the sparkline and the footer's own floor can't
# drift out of sync with each other.
SPARK_BOT_OFFSET = 27
FOOTER_SPARK_GAP = 7

BG_TOP, BG_BOTTOM = (12, 13, 18), (7, 8, 11)
CARD_TOP, CARD_BOTTOM = (27, 30, 39), (16, 18, 24)
CARD_TOP_STALE, CARD_BOTTOM_STALE = (20, 22, 29), (13, 14, 19)
DIVIDER = (58, 63, 78)
BAR_TRACK = (38, 42, 54)

FG = (238, 240, 247)
FG_DIM = (203, 208, 223)
FG_FAINT = (158, 164, 181)

GOOD = (110, 205, 150)
WARN = (255, 196, 80)
BAD = (255, 122, 92)
ACTIVE = (242, 159, 92)
NEUTRAL = (100, 106, 124)
VIOLET = (167, 139, 250)

STATE_COLORS = {
    "running": ACTIVE,
    "thinking": WARN,
    "idle": GOOD,
    "no session": NEUTRAL,
    "offline": NEUTRAL,
}

FONT_DIR = Path("/System/Library/Fonts")
_font_cache = {}


def _font(name, size):
    key = (name, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(str(FONT_DIR / name), size)
    return _font_cache[key]


def _title_font(size):
    return _font("SFNS.ttf", size)


def _mono_font(size):
    return _font("SFNSMono.ttf", size)


def _cjk_font(size):
    return _font("STHeiti Medium.ttc", size)


def _text_font_for(s, size):
    return _cjk_font(size) if any(ord(c) > 0x2E80 for c in s) else _title_font(size)


def _ellipsize(draw, s, font, max_w):
    if not s or draw.textlength(s, font=font) <= max_w:
        return s
    while s and draw.textlength(s + "…", font=font) > max_w:
        s = s[:-1]
    return s + "…" if s else ""


def _wrap_segments(draw, segments, font, max_w, sep=" · "):
    """Greedily pack sep-joined segments onto as few lines as fit max_w each —
    breaks between whole segments (never mid-word), same idea as text-wrap
    but along the natural ' · '-delimited clauses this caption is built from."""
    lines, current = [], ""
    for seg in segments:
        candidate = f"{current}{sep}{seg}" if current else seg
        if not current or draw.textlength(candidate, font=font) <= max_w:
            current = candidate
        else:
            lines.append(current)
            current = seg
    if current:
        lines.append(current)
    return lines


def _wrap_footer_lines(draw, segments, max_w):
    """Wrap footer segments at the normal caption size first; if that still
    needs more than one line, retry smaller so a 2-line footer costs less
    vertical room instead of just being an oversized copy of the 1-line
    case (which is what actually runs out of room against the row above)."""
    font, line_h = _mono_font(14), 18
    lines = _wrap_segments(draw, segments, font, max_w)
    if len(lines) > 1:
        font, line_h = _mono_font(12), 14
        lines = _wrap_segments(draw, segments, font, max_w)
    return lines, font, line_h


def _draw_wrapped_footer(draw, lines, x0, y_top, bottom_limit, font, fill, line_h=18, min_top=None):
    # Anchored to the card's own bottom edge, not just stacked down from
    # y_top: a 1-line footer (the common case) still starts at y_top exactly
    # as before, but a rarer 2-line one (e.g. a long-lived account's lifetime
    # stats) pulls itself up only as far as it actually needs to stay inside
    # the card, instead of assuming a fixed budget that was only ever tuned
    # for one line. min_top is the last usage row's own sparkline floor (when
    # it has one) — the block still prefers clearing the card edge, but never
    # gets pulled up far enough to sit on top of that row's trend line.
    y = min(y_top, bottom_limit - line_h * len(lines))
    if min_top is not None:
        y = max(y, min_top)
    for i, line in enumerate(lines):
        draw.text((x0, y + i * line_h), line, font=font, fill=fill)


def _relative_age(updated_at):
    if updated_at is None:
        return "—"
    age = max(0, time.time() - updated_at)
    if age < 60:
        return f"{int(age)}s"
    if age < 3600:
        return f"{int(age // 60)}m"
    return f"{int(age // 3600)}h"


def _format_resets(value):
    if value is None:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() if isinstance(value, str) else float(value)
    except (ValueError, TypeError):
        return None
    delta = ts - time.time()
    if delta <= 0:
        return "resetting…"
    hours = delta / 3600
    if hours < 1:
        return f"resets in {int(delta / 60)}m"
    if hours < 48:
        return f"resets in {int(hours)}h {int((delta - int(hours) * 3600) / 60)}m"
    return f"resets in {int(hours / 24)}d"


# Anthropic's default ephemeral prompt-cache TTL — resets on every turn that
# touches it, so this is "time left before the next message pays full
# cache-write cost again", not tied to this session's own idle timer. Accounts
# on the 1-hour extended-cache beta will see this undercount how long the
# cache actually survives; there's no local signal to tell the two apart.
PROMPT_CACHE_TTL_SEC = 5 * 60


def _cache_ttl_caption(last_response_at):
    if last_response_at is None:
        return ""
    remaining = last_response_at + PROMPT_CACHE_TTL_SEC - time.time()
    if remaining <= 0:
        return "cache expired"
    m, s = divmod(int(remaining), 60)
    return f"⏱ {m}m {s}s" if m else f"⏱ {s}s"


def _format_uptime(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _human_count(n):
    if n is None:
        return "—"
    n = float(n)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{int(n)}"


def _human_cost(usd):
    if usd is None:
        return None
    if usd >= 1000:
        return f"${usd / 1000:.1f}k"
    if usd >= 1:
        return f"${usd:.0f}"
    return f"${usd:.2f}"


# rate_per_hour's own trailing sample is only 3h wide; extrapolating it across
# a multi-day window (a weekly quota's hours_left can be ~165h) blows a tiny,
# noisy slope up into a wildly false 100%+ projection — e.g. 9% climbing at
# 3%/h early in a 7-day window "projects" past 500% even though that pace is
# completely normal. Capping the look-ahead keeps the warning meaningful for
# what it's actually for (you're about to blow through a *near-term* bar,
# like the 5-hour window, before it resets) without manufacturing alarms out
# of a few hours of ordinary usage inside a much longer window.
MAX_PROJECTION_HOURS = 12


def _predict_warning(tool, metric, current_pct, resets_at):
    """None, or the projected % this metric will reach within the next
    MAX_PROJECTION_HOURS (or by its own reset time, if sooner) if it keeps
    climbing at its trailing rate — only when that projection would blow
    past 100%, i.e. actually worth flagging."""
    if current_pct is None or resets_at is None:
        return None
    try:
        reset_ts = (datetime.fromisoformat(str(resets_at).replace("Z", "+00:00")).timestamp()
                    if isinstance(resets_at, str) else float(resets_at))
    except (ValueError, TypeError):
        return None
    hours_left = (reset_ts - time.time()) / 3600
    if hours_left <= 0:
        return None
    rate = history.rate_per_hour(tool, metric)
    if rate is None or rate <= 0:
        return None
    projected = current_pct + rate * min(hours_left, MAX_PROJECTION_HOURS)
    return projected if projected > 100 else None


def _severity_color(pct, invert=False):
    if pct is None:
        return FG_FAINT
    if invert:
        pct = 100 - pct
    if pct < 50:
        return GOOD
    if pct < 80:
        return WARN
    return BAD


def _temp_color(temp):
    if temp is None:
        return FG_DIM
    if temp < 55:
        return GOOD
    if temp < 75:
        return WARN
    return BAD


def _vertical_gradient(w, h, top, bottom):
    col = Image.new("RGB", (1, h))
    px = col.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return col.resize((w, h))


_bg_cache = {}
_region_color_cache = {}


def _background_path(name):
    bundled = BACKGROUNDS_DIR / f"{name}.png"
    if bundled.exists():
        return bundled
    return ROOT / f"{name}.png"


def _load_background(name):
    if name not in _bg_cache:
        path = _background_path(name)
        img = None
        if path.exists():
            img = Image.open(path).convert("RGB")
            if img.size != (CANVAS_W, CANVAS_H):
                img = img.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)
            brightness = BACKGROUND_BRIGHTNESS.get(name)
            if brightness is not None:
                img = ImageEnhance.Brightness(img).enhance(brightness)
        _bg_cache[name] = img
    return _bg_cache[name]


def _region_avg_color(name, bg, box):
    # The artwork behind a given card never changes tick-to-tick, so cache
    # the sampled color per (background, card box) instead of re-averaging
    # thousands of pixels every 1.5s frame.
    key = (name, tuple(int(v) for v in box))
    if key not in _region_color_cache:
        x0, y0, x1, y1 = key[1]
        crop = bg.crop((max(0, x0), max(0, y0), min(bg.width, x1), min(bg.height, y1)))
        mean = ImageStat.Stat(crop).mean
        _region_color_cache[key] = tuple(min(BG_TINT_MAX_CHANNEL, int(c)) for c in mean)
    return _region_color_cache[key]


def _blend(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def _rounded_card(img, box, radius, top_color, bottom_color, outline=None, outline_width=2,
                   bg=None, bg_name=None):
    x0, y0, x1, y1 = (int(v) for v in box)
    w, h = x1 - x0, y1 - y0

    tc, bc = top_color, bottom_color
    alpha = 255
    if bg is not None:
        # Tint the card toward the artwork's own color underneath it — the
        # "dynamic adjustment" the panels get per background image — while
        # keeping most of the weight on the dark base so text stays legible
        # regardless of what part of the scene sits behind any given card.
        avg = _region_avg_color(bg_name, bg, box)
        tc = _blend(top_color, avg, BG_TINT_STRENGTH)
        bc = _blend(bottom_color, avg, BG_TINT_STRENGTH)
        alpha = CARD_ALPHA_ON_BG

    grad = _vertical_gradient(w, h, tc, bc)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=alpha)
    img.paste(grad, (x0, y0), mask)
    if outline:
        ImageDraw.Draw(img).rounded_rectangle([x0, y0, x1, y1], radius=radius, outline=outline, width=outline_width)


def _hairline(draw, x0, x1, y):
    draw.line([(x0, y), (x1, y)], fill=DIVIDER, width=1)


def _badge(draw, x, y, size, label, color):
    draw.rounded_rectangle([x, y, x + size, y + size], radius=size * 0.28, fill=color)
    f = _title_font(int(size * 0.5))
    tw = draw.textlength(label, font=f)
    draw.text((x + (size - tw) / 2, y + size * 0.18), label, font=f, fill=(15, 16, 20))


def _capsule_bar(draw, x0, y0, x1, thickness, pct, fill_color, track_color=BAR_TRACK):
    y1 = y0 + thickness
    draw.rounded_rectangle([x0, y0, x1, y1], radius=thickness / 2, fill=track_color)
    if pct and pct > 0:
        fx1 = x0 + (x1 - x0) * min(1.0, pct / 100.0)
        if fx1 - x0 >= thickness:
            draw.rounded_rectangle([x0, y0, fx1, y1], radius=thickness / 2, fill=fill_color)
        else:
            draw.ellipse([x0, y0, x0 + thickness, y1], fill=fill_color)


def _sparkline(draw, x0, y0, x1, y1, values, color):
    if x1 - x0 < 20 or len(values) < 2:
        return
    lo, hi = min(values), max(values)
    span = hi - lo or 1
    w, h = x1 - x0, y1 - y0
    n = len(values)
    pts = [(x0 + w * i / (n - 1), y1 - (v - lo) / span * h) for i, v in enumerate(values)]
    draw.line(pts, fill=color, width=2, joint="curve")


def _bar_metric_row(draw, x0, x1, y, label, pct, caption, trend=None, warn=None, invert=False, compact=False):
    lf = _mono_font(14)
    draw.text((x0, y), label, font=lf, fill=FG_DIM)

    if pct is None:
        # No dead 0%-dash bar — a single dim line reads as "unmeasured", not "empty".
        # Offset must clear the label's own line box (font14 ascent+descent=17px)
        # or the note text's ascender row collides with the label's descender row.
        note = caption or "no data"
        nf = _mono_font(13)
        nw = draw.textlength(note, font=nf)
        draw.text((x1 - nw, y + (20 if compact else 24)), note, font=nf, fill=FG_FAINT)
        return

    # An uncapped quota tier (e.g. MiniMax weekly on an unlimited plan) is
    # signaled by the caller as +inf rather than the real (near-0%) used
    # value — 0% used reads as "barely touched", which is the opposite of
    # what an unlimited tier means. Shown as a full violet bar + ∞ instead.
    unlimited = pct == float("inf")
    pct_text = "∞" if unlimited else f"{pct:.0f}%"
    inf_font = _mono_font(16)
    color = VIOLET if unlimited else (BAD if warn else _severity_color(pct, invert=invert))
    pw = draw.textlength(pct_text, font=inf_font if unlimited else lf)
    draw.text((x1 - pw, y - (1 if unlimited else 0)), pct_text, font=inf_font if unlimited else lf, fill=color)

    # compact=True packs 3 rows into the space 2 normally use (panels that gained
    # a CACHE HIT row) — same bar thickness for visual consistency, tighter gaps.
    bar_y = y + (18 if compact else 22)
    _capsule_bar(draw, x0, bar_y, x1, 8, 100 if unlimited else pct, color)

    # A pace warning used to REPLACE the caption, which silently dropped the
    # "resets in …" countdown — the single most-wanted number on the row, and
    # exactly the context that makes a pace warning actionable. Keep both, and
    # drop the warning's own "by reset" wording since the countdown now says it.
    if warn:
        caption_text = f"⚠ pace → {warn:.0f}%"
        if caption:
            caption_text = f"{caption_text} · {caption}"
    else:
        caption_text = caption
    spark_x1 = x1
    # cap_gap is measured from bar_y, not from the bar's bottom edge (bar_y +
    # thickness=8) — must clear that plus the font's own ink offset (glyphs
    # render a couple px below the y coordinate passed to draw.text) or the
    # caption's ink sits on top of the bar fill. Pixel-scanned across every
    # caption string this row can show (plain "resets in…", the pace-warning
    # text, etc.) — 17/18 is the smallest value that still clears a real 10px
    # gap in the worst case; smaller values were verified to leave <10px.
    cap_gap = 17 if compact else 18
    cap_size = 11 if compact else 12
    if caption_text:
        cf = _mono_font(cap_size)
        # Captions are right-anchored at x1, so an over-long one runs off the
        # card's left edge (and under the label) instead of being clipped —
        # ellipsize against the row's real width rather than letting it bleed.
        caption_text = _ellipsize(draw, caption_text, cf, x1 - x0)
        cw = draw.textlength(caption_text, font=cf)
        caption_color = VIOLET if unlimited else (BAD if warn else FG_FAINT)
        draw.text((x1 - cw, bar_y + cap_gap), caption_text, font=cf, fill=caption_color)
        spark_x1 = x1 - cw - 14

    # Sparkline fills whatever horizontal space the caption doesn't use —
    # no dedicated row, so it never costs the panel extra height. Deliberately
    # NOT `color`: when warn overrides the bar+caption to BAD, a same-color
    # sparkline sitting a few px below reads as a smear of the bar rather than
    # a separate element — its own severity color keeps the row legible even
    # with the tight compact-mode gap.
    if trend:
        # A curve can touch its own top bound at any data point (unlike text,
        # which has a roughly fixed ink offset), so this needed its own
        # pixel-scan across volatile trend shapes: 19 is the smallest value
        # that still guarantees a real 10px gap after the bar in the worst
        # case. Same value in both modes — the sparkline's own geometry
        # doesn't depend on compact, only bar_y (handled separately) does.
        # spark_bot trimmed from 33 to SPARK_BOT_OFFSET to keep the last
        # row's trend line clear of the lifetime-stats footer that can
        # immediately follow it — still a real ~8px-tall curve.
        spark_top, spark_bot = 19, SPARK_BOT_OFFSET
        spark_color = _severity_color(pct, invert=invert)
        _sparkline(draw, x0, bar_y + spark_top, spark_x1, bar_y + spark_bot, trend, spark_color)


def _stat_metric_row(draw, x0, x1, y, label, value_text, caption):
    lf = _mono_font(14)
    draw.text((x0, y), label, font=lf, fill=FG_DIM)
    vf = _mono_font(22)
    vw = draw.textlength(value_text, font=vf)
    draw.text((x1 - vw, y - 3), value_text, font=vf, fill=FG)
    if caption:
        cf = _mono_font(12)
        draw.text((x0, y + 25), caption, font=cf, fill=FG_FAINT)


def _two_col_stat_row(draw, x0, x1, y, left_label, left_value, right_label, right_value):
    half = (x1 - x0) / 2
    lf, vf = _mono_font(12), _mono_font(16)
    draw.text((x0, y), left_label, font=lf, fill=FG_FAINT)
    draw.text((x0, y + 18), left_value, font=vf, fill=FG_DIM)
    draw.line([(x0 + half, y), (x0 + half, y + 34)], fill=DIVIDER, width=1)
    draw.text((x0 + half + 16, y), right_label, font=lf, fill=FG_FAINT)
    draw.text((x0 + half + 16, y + 18), right_value, font=vf, fill=FG_DIM)


def _hardware_side_values(hw):
    fan = hw.get("fan_rpm")
    temp = hw.get("cpu_temp")
    swap_used, swap_total = hw.get("swap_used_gb"), hw.get("swap_total_gb")

    if swap_used == 0 and swap_total == 0:
        swap_text = "0 GB"
    elif swap_used is not None and swap_total is not None:
        swap_text = f"{swap_used:.1f}/{swap_total:.0f} GB"
    else:
        swap_text = "—"

    return [
        {
            "label": "FAN",
            "value": f"{fan:.0f} RPM" if fan is not None else "—",
            "color": FG_DIM,
        },
        {
            "label": "TEMP",
            "value": f"{temp:.0f}°C" if temp is not None else "—",
            "color": _temp_color(temp),
        },
        {
            "label": "SWAP",
            "value": swap_text,
            "color": FG_DIM,
        },
    ]


def _side_metric_row(draw, x0, x1, y, label, value_text, value_color):
    lf = _mono_font(12)
    vf = _mono_font(15)
    draw.text((x0, y), label, font=lf, fill=FG_FAINT)
    value_text = _ellipsize(draw, value_text, vf, x1 - x0)
    draw.text((x0, y + 18), value_text, font=vf, fill=value_color)


def _session_row(draw, x0, x1, y, session):
    r = 5
    dot_x = x0 + 3
    color = STATE_COLORS.get(session["state"], FG_DIM)
    draw.ellipse([dot_x, y - r, dot_x + 2 * r, y + r], fill=color)

    age = _relative_age(session.get("updated_at"))
    af = _mono_font(14)
    aw = draw.textlength(age, font=af)

    text_x = dot_x + 2 * r + 12
    proj = session.get("project") or "(root)"
    pf = _text_font_for(proj, 16)
    proj = _ellipsize(draw, proj, pf, (x1 - text_x) - aw - 12)
    draw.text((text_x, y - 9), proj, font=pf, fill=FG_DIM)
    draw.text((x1 - aw, y - 7), age, font=af, fill=FG_FAINT)


def _identity_caption(status):
    plan_type = status.get("plan_type")
    plan_text = str(plan_type).replace("_", " ").title() if plan_type else None
    if status.get("tool") in ("Kimi Code", "MiniMax"):
        return plan_text

    identity = status.get("identity")
    if plan_text:
        return f"{identity} · {plan_text}" if identity else plan_text
    return identity


def _tool_display_name(status):
    return status.get("display_name") or status["tool"]


def _usage_metrics(status):
    # 6-tuple: (kind, label, value, caption, metric_key, resets_at) — metric_key
    # names the field this row's history is stored under (for trend/prediction),
    # None for rows that don't have a meaningful trend (e.g. a raw stat).
    tool = status["tool"]
    if tool == "Claude Code":
        five, seven = status.get("usage_percent"), status.get("usage_seven_day_percent")
        hit = status.get("cache_hit_percent")
        ttl_caption = _cache_ttl_caption(status.get("last_assistant_response_at"))
        if five is None and seven is None:
            # No Anthropic quota API reachable (e.g. a custom ANTHROPIC_BASE_URL) —
            # fall back to real, locally-derived context stats instead of a dead bar.
            ctx_tok = status.get("context_tokens")
            return [
                ("stat", "CONTEXT", _human_count(ctx_tok) + " tok" if ctx_tok else "—", "", None, None),
                ("bar", "CACHE HIT", hit, ttl_caption, "cache_hit_percent", None),
            ]
        resets = _format_resets(status.get("usage_resets_at"))
        return [
            ("bar", "5-HOUR", five, resets or ("no usage data" if five is None else ""), "usage_percent", status.get("usage_resets_at")),
            ("bar", "7-DAY", seven, "", "usage_seven_day_percent", None),
            ("bar", "CACHE HIT", hit, ttl_caption, "cache_hit_percent", None),
        ]
    if tool == "Codex":
        limit = status.get("usage_percent")
        resets = _format_resets(status.get("usage_resets_at"))
        rows = [("bar", "RATE LIMIT", limit, resets or ("no usage data" if limit is None else ""), "usage_percent", status.get("usage_resets_at"))]
        ctx = status.get("context_percent")
        secondary = status.get("secondary_percent")
        if ctx is not None:
            rows.append(("bar", "CONTEXT", ctx, "", "context_percent", None))
        elif secondary is not None:
            # Some plans' live usage API never returns a secondary_window at
            # all (primary_window IS the account's only limit, sometimes
            # already a weekly-length one) — that's a permanent "no data" for
            # this account, not a transient gap, so the row is dropped
            # entirely rather than shown pinned to "no usage data" forever.
            sec_resets = _format_resets(status.get("secondary_resets_at"))
            rows.append(("bar", "WEEKLY", secondary, sec_resets or "", "secondary_percent", status.get("secondary_resets_at")))
        rows.append(("bar", "CACHE HIT", status.get("cache_hit_percent"), "", "cache_hit_percent", None))
        return rows
    if tool == "Kimi Code":
        monthly = status.get("kimi_monthly_percent")
        five = status.get("kimi_five_hour_percent")
        weekly = status.get("kimi_weekly_percent")
        monthly_resets_at = status.get("kimi_monthly_resets_at")
        five_resets_at = status.get("kimi_five_hour_resets_at")
        weekly_resets_at = status.get("kimi_weekly_resets_at")
        return [
            ("bar", "MONTHLY", monthly,
             _format_resets(monthly_resets_at) or ("no usage data" if monthly is None else ""),
             "kimi_monthly_percent", monthly_resets_at),
            ("bar", "5-HOUR", five,
             _format_resets(five_resets_at) or ("no usage data" if five is None else ""),
             "kimi_five_hour_percent", five_resets_at),
            ("bar", "WEEKLY", weekly,
             _format_resets(weekly_resets_at) or ("no usage data" if weekly is None else ""),
             "kimi_weekly_percent", weekly_resets_at),
        ]
    if tool == "MiniMax":
        five = status.get("minimax_five_hour_percent")
        five_remaining = status.get("minimax_five_hour_remaining")
        five_total = status.get("minimax_five_hour_total")
        five_resets_at = status.get("minimax_five_hour_resets_at")
        weekly = status.get("minimax_weekly_percent")
        weekly_remaining = status.get("minimax_weekly_remaining")
        weekly_total = status.get("minimax_weekly_total")
        weekly_resets_at = status.get("minimax_weekly_resets_at")
        five_parts = []
        if status.get("minimax_five_hour_status") == 3:
            five_parts.append("unlimited")
        if five_remaining is not None and five_total:
            five_parts.append(f"{five_remaining}/{five_total} left")
        five_reset = _format_resets(five_resets_at)
        if five_reset:
            five_parts.append(five_reset)
        # Weekly unlimited is now conveyed visually (∞ + violet bar in
        # _bar_metric_row, triggered by the +inf sentinel below) instead of
        # this caption's own "unlimited" word — keeping both said the same
        # thing twice.
        weekly_unlimited = status.get("minimax_weekly_status") == 3
        weekly_parts = []
        if weekly_remaining is not None and weekly_total:
            weekly_parts.append(f"{weekly_remaining}/{weekly_total} left")
        weekly_reset = _format_resets(weekly_resets_at)
        if weekly_reset:
            weekly_parts.append(weekly_reset)
        context = status.get("context_percent")
        context_tokens = status.get("context_tokens")
        context_window = status.get("context_window")
        context_caption = ""
        if context_tokens is not None and context_window:
            context_caption = (
                f"{_human_count(context_tokens)} / "
                f"{_human_count(context_window)} tok"
            )
        return [
            (
                "bar",
                "5-HOUR",
                five,
                " · ".join(five_parts) or ("no usage data" if five is None else ""),
                "minimax_five_hour_percent",
                five_resets_at,
            ),
            (
                "bar",
                "WEEKLY",
                float("inf") if weekly_unlimited else weekly,
                " · ".join(weekly_parts)
                or ("no usage data" if weekly is None else ""),
                "minimax_weekly_percent",
                weekly_resets_at,
            ),
            (
                "bar",
                "CONTEXT",
                context,
                context_caption
                or ("no session data" if context is None else ""),
                "context_percent",
                None,
            ),
            (
                "bar",
                "CACHE HIT",
                status.get("cache_hit_percent"),
                (
                    "no session data"
                    if status.get("cache_hit_percent") is None
                    else ""
                ),
                "cache_hit_percent",
                None,
            ),
        ]
    if tool == "Grok":
        cli_resets_at = status.get("grok_cli_resets_at")
        context = status.get("context_percent")
        context_tokens = status.get("context_tokens")
        context_window = status.get("context_window")
        context_caption = ""
        if context_tokens is not None and context_window:
            context_caption = (
                f"{_human_count(context_tokens)} / "
                f"{_human_count(context_window)} tok"
            )
        bot_resets_at = status.get("grok_bot_resets_at")
        return [
            (
                "bar",
                "CLI QUOTA",
                status.get("grok_cli_percent"),
                _format_resets(cli_resets_at)
                or ("no billing data" if status.get("grok_cli_percent") is None else ""),
                "grok_cli_percent",
                cli_resets_at,
            ),
            (
                "bar",
                "CLI CTX",
                context,
                context_caption or ("no active session" if context is None else ""),
                "context_percent",
                None,
            ),
            (
                "bar",
                "BOT QUOTA",
                status.get("grok_bot_percent"),
                _format_resets(bot_resets_at)
                or ("no usage data" if status.get("grok_bot_percent") is None else ""),
                "grok_bot_percent",
                bot_resets_at,
            ),
            (
                "bar",
                "CACHE HIT",
                status.get("cache_hit_percent"),
                "24H CLI" if status.get("cache_hit_percent") is not None else "no session data",
                "cache_hit_percent",
                None,
            ),
        ]
    five = status.get("zcode_five_hour_percent")
    five_resets_at = status.get("zcode_five_hour_resets_at")
    weekly = status.get("zcode_weekly_percent")
    weekly_resets_at = status.get("zcode_weekly_resets_at")
    req = status.get("zcode_request_percent")
    req_resets_at = status.get("zcode_request_resets_at")

    req_left, req_total = status.get("zcode_request_remaining"), status.get("zcode_request_total")
    req_parts = []
    if req_left is not None and req_total:
        req_parts.append(f"{req_left}/{req_total} left")
    req_reset = _format_resets(req_resets_at)
    if req_reset:
        req_parts.append(req_reset)
    # "top: <tool> <n>" is the least important of the three and the longest —
    # all three together overflow the row's width, so it only earns its place
    # when the quota numbers themselves aren't available to show.
    top_feature, top_usage = status.get("zcode_top_feature"), status.get("zcode_top_feature_usage")
    if top_feature and top_usage and not req_parts:
        req_parts.append(f"top: {top_feature} {top_usage}")
    req_caption = " · ".join(req_parts)

    rows = [
        ("bar", "5-HOUR", five,
         _format_resets(five_resets_at) or ("no usage data" if five is None else ""),
         "zcode_five_hour_percent", five_resets_at),
    ]
    # The weekly token cap is only emitted by the quota endpoint for plans that
    # actually carry one, so an absent weekly window is a real "this plan has
    # none" — not a fetch failure worth showing an empty bar for.
    if weekly is not None:
        rows.append(
            ("bar", "WEEKLY", weekly,
             _format_resets(weekly_resets_at) or "",
             "zcode_weekly_percent", weekly_resets_at)
        )
    rows.append(
        ("bar", "TOOLS", req, req_caption or ("no usage data" if req is None else ""),
         "zcode_request_percent", req_resets_at)
    )
    rows.append(
        ("bar", "CACHE HIT", status.get("cache_hit_percent"),
         "no session data" if status.get("cache_hit_percent") is None else "",
         "cache_hit_percent", None)
    )
    return rows


def _is_offline_status(status):
    health = status.get("health")
    if health is not None:
        return health == "offline"
    return status.get("state") in ("no session", "offline")


def _session_slot_count(metrics):
    return 2 if len(metrics) >= 4 else MAX_SESSION_ROWS


def _is_offline_status(status):
    health = status.get("health")
    if health is not None:
        return health == "offline"
    return status.get("state") in ("no session", "offline")


def _session_slot_count(metrics):
    return 2 if len(metrics) >= 4 else MAX_SESSION_ROWS


def _draw_agent_panel(img, x0, status, bg=None, bg_name=None):
    state = status.get("state", "no session")
    offline = _is_offline_status(status)
    stale = offline or state == "offline"
    active_count = status.get("active_count", 0)
    is_active = not offline and active_count > 0
    accent = STATE_COLORS.get("offline" if offline else state, FG_DIM)

    if is_active:
        top_c = tuple(min(255, c + 6) for c in CARD_TOP)
        bot_c = tuple(min(255, c + 6) for c in CARD_BOTTOM)
    else:
        top_c = CARD_TOP_STALE if stale else CARD_TOP
        bot_c = CARD_BOTTOM_STALE if stale else CARD_BOTTOM

    cx0, cy0 = x0 + CARD_MARGIN, CARD_MARGIN
    cx1, cy1 = x0 + PANEL_W - CARD_MARGIN, CANVAS_H - CARD_MARGIN
    # A busier background behind the card needs a slightly stronger outline
    # to still read as a contained panel rather than floating text.
    base_width = 4 if is_active else 2
    _rounded_card(img, (cx0, cy0, cx1, cy1), 22, top_c, bot_c, outline=accent,
                  outline_width=base_width + 1 if bg is not None else base_width,
                  bg=bg, bg_name=bg_name)
    draw = ImageDraw.Draw(img)

    ix0, ix1 = cx0 + PAD, cx1 - PAD

    display_name = _tool_display_name(status)
    badge_size = HEADER_BADGE_SIZE
    _badge(draw, ix0, cy0 + PAD, badge_size, display_name[0], accent)
    draw.text(
        (ix0 + badge_size + HEADER_GAP, cy0 + PAD + 2),
        display_name,
        font=_title_font(HEADER_TITLE_SIZE),
        fill=FG,
    )

    if stale:
        agg_label = "OFFLINE"
    elif active_count:
        agg_label = f"{active_count} ACTIVE"
    else:
        agg_label = "IDLE"
    af = _mono_font(14)
    aw = draw.textlength(agg_label, font=af)
    dot_r = 5
    draw.ellipse([ix1 - aw - 2 * dot_r - 10, cy0 + PAD + 8, ix1 - aw - 10, cy0 + PAD + 8 + 2 * dot_r], fill=accent)
    draw.text((ix1 - aw, cy0 + PAD + 6), agg_label, font=af, fill=FG_DIM)

    # Which provider/plan/model is actually active — the empty corner below
    # the ACTIVE/OFFLINE label has just enough room for this without costing
    # a dedicated row.
    identity = _identity_caption(status)
    if identity:
        idf = _mono_font(12)
        identity = _ellipsize(draw, identity, idf, ix1 - ix0)
        iw = draw.textlength(identity, font=idf)
        draw.text((ix1 - iw, cy0 + PAD + 27), identity, font=idf, fill=FG_FAINT)

    y = cy0 + PAD + badge_size + 14
    _hairline(draw, ix0, ix1, y)
    y += 20

    draw.text((ix0, y), "SESSIONS", font=_mono_font(12), fill=FG_FAINT)
    y += 22

    # 3 rows (panels with a CACHE HIT bar added) need a real 10px gap after
    # each bar, which in turn needs more room per row than 2-row panels —
    # computed here (rather than after the sessions block) so the session
    # rows and the SESSIONS->USAGE transition can also give up some of their
    # own space to it, instead of squeezing everything into the leftover
    # room at the bottom of the card.
    metrics = _usage_metrics(status)
    compact = len(metrics) > 2
    row_step = 54 if compact else 58
    session_slots = _session_slot_count(metrics)

    sessions = status.get("sessions", [])
    row_h = 27 if compact else 30
    if not sessions:
        draw.text((ix0, y + 4), "No active session", font=_title_font(16), fill=FG_FAINT)
        y += row_h * session_slots
    else:
        shown = sessions[:session_slots]
        for s in shown:
            _session_row(draw, ix0, ix1, y + 9, s)
            y += row_h
        remaining = len(sessions) - len(shown)
        slots_left = session_slots - len(shown)
        if remaining > 0 and slots_left > 0:
            draw.text((ix0, y + 2), f"+{remaining} more", font=_mono_font(13), fill=FG_FAINT)
        y += row_h * slots_left

    y += 0 if compact else 8
    _hairline(draw, ix0, ix1, y)
    y += 14 if compact else 20

    draw.text((ix0, y), "USAGE", font=_mono_font(12), fill=FG_FAINT)
    y += 16 if compact else 22
    for kind, label, value, caption, metric_key, resets_at in metrics:
        if kind == "bar":
            # +inf (uncapped tier, e.g. MiniMax weekly unlimited) has no real
            # trend/projection to compute against — a projection against
            # infinity is meaningless and would render a garbled "⚠ pace →
            # infl by reset" caption.
            unlimited = value == float("inf")
            trend = history.recent_values(status["tool"], metric_key) if metric_key and not unlimited else None
            warn = (
                _predict_warning(status["tool"], metric_key, value, resets_at)
                if metric_key and not unlimited else None
            )
            invert = metric_key == "cache_hit_percent"
            _bar_metric_row(draw, ix0, ix1, y, label, value, caption, trend=trend, warn=warn, invert=invert, compact=compact)
        else:
            _stat_metric_row(draw, ix0, ix1, y, label, value, caption)
        y += row_step

    # Floor comes from the CACHE HIT row (always last, see _usage_metrics)
    # own sparkline reach — the footer must never sit on top of it, even
    # when the bottom-edge anchor below would otherwise pull it up that far.
    last_bar_y = (y - row_step) + (18 if compact else 22)
    footer_min_top = last_bar_y + SPARK_BOT_OFFSET + FOOTER_SPARK_GAP
    footer_bottom_limit = cy1 - 10

    lifetime_tokens = status.get("lifetime_total_tokens")
    if lifetime_tokens is not None:
        sessions_n = status.get("lifetime_session_count")
        cost = _human_cost(status.get("lifetime_cost_usd"))
        segments = [f"{_human_count(sessions_n)} sessions", f"{_human_count(lifetime_tokens)} tok"]
        segments.append(f"~{cost} all-time" if cost else "all-time")
        if status.get("credits_unlimited"):
            segments.append("unlimited credits")
        elif status.get("credits_balance"):
            segments.append(f"${status['credits_balance']:,.0f} credits")
        lines, footer_font, line_h = _wrap_footer_lines(draw, segments, ix1 - ix0)
        _draw_wrapped_footer(draw, lines, ix0, y + 4, footer_bottom_limit, footer_font, FG_FAINT,
                line_h=line_h, min_top=footer_min_top)
    elif status["tool"] == "Grok":
        sessions_24h = status.get("grok_sessions_24h")
        tokens_24h = status.get("grok_tokens_24h")
        model_calls = status.get("grok_model_calls_24h")
        if sessions_24h or tokens_24h is not None:
            segments = [f"{_human_count(sessions_24h)} sessions 24h"]
            if tokens_24h is not None:
                segments.append(f"{_human_count(tokens_24h)} tok 24h")
            if model_calls is not None:
                segments.append(f"{_human_count(model_calls)} calls")
            lines, footer_font, line_h = _wrap_footer_lines(draw, segments, ix1 - ix0)
            _draw_wrapped_footer(
                draw, lines, ix0, y + 4, footer_bottom_limit, footer_font, FG_FAINT,
                line_h=line_h, min_top=footer_min_top,
            )
    elif status["tool"] == "zcode":
        sessions_today = status.get("sessions_today")
        session_tokens = status.get("session_tokens")
        if sessions_today or session_tokens is not None:
            segments = [f"{_human_count(sessions_today)} sessions today"]
            if session_tokens is not None:
                segments.append(f"{_human_count(session_tokens)} tok (latest)")
            lines, footer_font, line_h = _wrap_footer_lines(draw, segments, ix1 - ix0)
            _draw_wrapped_footer(draw, lines, ix0, y + 4, footer_bottom_limit, footer_font, FG_FAINT,
                                  line_h=line_h, min_top=footer_min_top)


def _draw_hardware_panel(img, x0, hw, bg=None, bg_name=None):
    accent = _temp_color(hw.get("cpu_temp"))
    cx0, cy0 = x0 + CARD_MARGIN, CARD_MARGIN
    cx1, cy1 = x0 + PANEL_W - CARD_MARGIN, CANVAS_H - CARD_MARGIN
    _rounded_card(img, (cx0, cy0, cx1, cy1), 22, CARD_TOP, CARD_BOTTOM, outline=accent,
                  outline_width=3 if bg is not None else 2, bg=bg, bg_name=bg_name)
    draw = ImageDraw.Draw(img)

    ix0, ix1 = cx0 + PAD, cx1 - PAD

    badge_size = HEADER_BADGE_SIZE
    _badge(draw, ix0, cy0 + PAD, badge_size, "HW", accent)
    draw.text(
        (ix0 + badge_size + HEADER_GAP, cy0 + PAD + 2),
        "Hardware",
        font=_title_font(HEADER_TITLE_SIZE),
        fill=FG,
    )

    clock = time.strftime("%H:%M:%S")
    cf = _mono_font(14)
    cw = draw.textlength(clock, font=cf)
    draw.text((ix1 - cw, cy0 + PAD + 8), clock, font=cf, fill=FG_DIM)

    y = cy0 + PAD + badge_size + 14
    _hairline(draw, ix0, ix1, y)
    y += 22

    mem_caption = f"{hw['mem_used_gb']:.1f} / {hw['mem_total_gb']:.0f} GB" if hw.get("mem_total_gb") else "—"
    disk_caption = f"{hw['disk_free_gb']:.0f} GB free" if hw.get("disk_free_gb") is not None else "—"

    side_w = 96
    side_gap = 14
    side_x0 = ix1 - side_w
    side_sep_x = side_x0 - side_gap
    main_x1 = side_sep_x - side_gap
    row_step = 58
    rows = (
        ("CPU", hw.get("cpu_usage"), ""),
        ("MEMORY", hw.get("mem_percent"), mem_caption),
        ("DISK", hw.get("disk_percent"), disk_caption),
    )
    side_values = _hardware_side_values(hw)
    draw.line([(side_sep_x, y - 2), (side_sep_x, y + row_step * len(rows) - 11)],
              fill=DIVIDER, width=1)

    for idx, (label, pct, caption) in enumerate(rows):
        _bar_metric_row(draw, ix0, main_x1, y, label, pct, caption)
        side = side_values[idx]
        _side_metric_row(draw, side_x0, ix1, y, side["label"], side["value"], side["color"])
        y += row_step

    y += 4
    _hairline(draw, ix0, ix1, y)
    y += 20

    load1 = hw.get("load1")
    _two_col_stat_row(
        draw, ix0, ix1, y,
        "LOAD (1m)", f"{load1:.2f}" if load1 is not None else "—",
        "UPTIME", _format_uptime(hw.get("uptime_sec")),
    )

    y += 44
    up, down = hw.get("net_up_kbps"), hw.get("net_down_kbps")
    draw.text((ix0, y), "NETWORK", font=_mono_font(12), fill=FG_FAINT)
    if up is not None and down is not None:
        net_text = f"↑ {up:.0f} KB/s   ↓ {down:.0f} KB/s"
    else:
        # first tick after (re)start — rate needs two samples, show cumulative instead of a bare dash
        total_up = hw.get("net_total_up_gb")
        total_down = hw.get("net_total_down_gb")
        net_text = (f"↑ {total_up:.1f} GB   ↓ {total_down:.1f} GB (total)"
                    if total_up is not None and total_down is not None else "—")
    nf = _mono_font(16)
    nw = draw.textlength(net_text, font=nf)
    net_y = y + 15 if nw > (ix1 - ix0) * 0.72 else y - 2
    draw.text((ix1 - nw, net_y), net_text, font=nf, fill=FG_DIM)


def render(agent_statuses, hw_status, background=None):
    bg = _load_background(background) if background else None
    if bg is not None:
        img = bg.copy()
    else:
        img = Image.new("RGB", (CANVAS_W, CANVAS_H))
        img.paste(_vertical_gradient(CANVAS_W, CANVAS_H, BG_TOP, BG_BOTTOM), (0, 0))
    for i, status in enumerate(agent_statuses):
        _draw_agent_panel(img, i * PANEL_W, status, bg=bg, bg_name=background)
    _draw_hardware_panel(img, len(agent_statuses) * PANEL_W, hw_status, bg=bg, bg_name=background)
    return img


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sources import claude_code, codex_cli, grok, kimi, minimax, zcode, hardware

    background = sys.argv[1] if len(sys.argv) > 1 else None
    statuses = [
        claude_code.read_status(),
        codex_cli.read_status(),
        kimi.read_status(),
        zcode.read_status(),
        minimax.read_status(),
        grok.read_status(),
    ]
    hw = hardware.read_status()
    out = render(statuses, hw, background=background)
    out_path = Path(__file__).resolve().parent.parent / "preview.png"
    out.save(out_path)
    print(f"wrote {out_path}")
