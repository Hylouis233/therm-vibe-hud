import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageStat

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources import history  # noqa: E402

CANVAS_W, CANVAS_H = 1920, 462
BACKGROUNDS_DIR = Path(__file__).resolve().parent.parent / "assets" / "backgrounds"
# How much a card's tint shifts toward the artwork behind it, and how
# see-through the card is over that artwork.
BG_TINT_STRENGTH = 0.48
CARD_ALPHA_ON_BG = 226
# Cap how much brightness a sampled backdrop region can contribute before
# blending — a bright sky/mist patch behind a card would otherwise wash the
# tint out toward white and kill text contrast; darker regions (below the
# cap) pass through unclamped so panels still visibly differ from each other.
BG_TINT_MAX_CHANNEL = 118
PANEL_COUNT = 4
PANEL_W = CANVAS_W // PANEL_COUNT
CARD_MARGIN = 14
PAD = 22
MAX_SESSION_ROWS = 4

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
NEUTRAL = (100, 106, 124)

STATE_COLORS = {"running": BAD, "thinking": WARN, "idle": GOOD, "no session": NEUTRAL}

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


def _predict_warning(tool, metric, current_pct, resets_at):
    """None, or the projected % this metric will reach by its own reset time
    if it keeps climbing at its trailing rate — only when that projection
    would blow past 100%, i.e. actually worth flagging."""
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
    projected = current_pct + rate * hours_left
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


def _load_background(name):
    if name not in _bg_cache:
        path = BACKGROUNDS_DIR / f"{name}.png"
        img = None
        if path.exists():
            img = Image.open(path).convert("RGB")
            if img.size != (CANVAS_W, CANVAS_H):
                img = img.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)
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

    pct_text = f"{pct:.0f}%"
    color = BAD if warn else _severity_color(pct, invert=invert)
    pw = draw.textlength(pct_text, font=lf)
    draw.text((x1 - pw, y), pct_text, font=lf, fill=color)

    # compact=True packs 3 rows into the space 2 normally use (panels that gained
    # a CACHE HIT row) — same bar thickness for visual consistency, tighter gaps.
    bar_y = y + (18 if compact else 22)
    _capsule_bar(draw, x0, bar_y, x1, 8, pct, color)

    caption_text = f"⚠ pace → {warn:.0f}% by reset" if warn else caption
    spark_x1 = x1
    # cap_gap is measured from bar_y, not from the bar's bottom edge (bar_y +
    # thickness=8) — must clear that plus the font's own ascent (11px at size
    # 11) or the caption's ink sits on top of the bar fill. 12 leaves a real
    # 4px gap after the bar; the previous value of 9 left only 1px and was
    # visibly overlapping the bar on any row with a caption (e.g. the pace
    # warning), independent of caption content.
    cap_gap = 12 if compact else 15
    cap_size = 11 if compact else 12
    if caption_text:
        cf = _mono_font(cap_size)
        cw = draw.textlength(caption_text, font=cf)
        draw.text((x1 - cw, bar_y + cap_gap), caption_text, font=cf, fill=BAD if warn else FG_FAINT)
        spark_x1 = x1 - cw - 14

    # Sparkline fills whatever horizontal space the caption doesn't use —
    # no dedicated row, so it never costs the panel extra height. Deliberately
    # NOT `color`: when warn overrides the bar+caption to BAD, a same-color
    # sparkline sitting a few px below reads as a smear of the bar rather than
    # a separate element — its own severity color keeps the row legible even
    # with the tight compact-mode gap.
    if trend:
        spark_top, spark_bot = (12, 26) if compact else (13, 27)
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


def _usage_metrics(status):
    # 6-tuple: (kind, label, value, caption, metric_key, resets_at) — metric_key
    # names the field this row's history is stored under (for trend/prediction),
    # None for rows that don't have a meaningful trend (e.g. a raw stat).
    tool = status["tool"]
    if tool == "Claude Code":
        five, seven = status.get("usage_percent"), status.get("usage_seven_day_percent")
        if five is None and seven is None:
            # No Anthropic quota API reachable (e.g. a custom ANTHROPIC_BASE_URL) —
            # fall back to real, locally-derived context stats instead of a dead bar.
            ctx_tok = status.get("context_tokens")
            hit = status.get("cache_hit_percent")
            return [
                ("stat", "CONTEXT", _human_count(ctx_tok) + " tok" if ctx_tok else "—", "", None, None),
                ("bar", "CACHE HIT", hit, "", "cache_hit_percent", None),
            ]
        resets = _format_resets(status.get("usage_resets_at"))
        return [
            ("bar", "5-HOUR", five, resets or ("no usage data" if five is None else ""), "usage_percent", status.get("usage_resets_at")),
            ("bar", "7-DAY", seven, "", "usage_seven_day_percent", None),
        ]
    if tool == "Codex":
        limit = status.get("usage_percent")
        resets = _format_resets(status.get("usage_resets_at"))
        first_row = ("bar", "RATE LIMIT", limit, resets or ("no usage data" if limit is None else ""), "usage_percent", status.get("usage_resets_at"))
        ctx = status.get("context_percent")
        if ctx is not None:
            second_row = ("bar", "CONTEXT", ctx, "", "context_percent", None)
        else:
            secondary = status.get("secondary_percent")
            sec_resets = _format_resets(status.get("secondary_resets_at"))
            second_row = ("bar", "WEEKLY", secondary, sec_resets or ("no usage data" if secondary is None else ""), "secondary_percent", status.get("secondary_resets_at"))
        third_row = ("bar", "CACHE HIT", status.get("cache_hit_percent"), "", "cache_hit_percent", None)
        return [first_row, second_row, third_row]
    tok, req = status.get("zcode_token_percent"), status.get("zcode_request_percent")
    tok_resets = _format_resets(status.get("zcode_token_resets_at"))
    req_left, req_total = status.get("zcode_request_remaining"), status.get("zcode_request_total")
    req_caption = f"{req_left}/{req_total} left" if req_left is not None and req_total else _format_resets(status.get("zcode_request_resets_at"))
    top_feature, top_usage = status.get("zcode_top_feature"), status.get("zcode_top_feature_usage")
    if top_feature and top_usage:
        req_caption = f"{req_caption} · top: {top_feature} {top_usage}" if req_caption else f"top: {top_feature} {top_usage}"
    return [
        ("bar", "TOKENS", tok, tok_resets or ("no usage data" if tok is None else ""), "zcode_token_percent", status.get("zcode_token_resets_at")),
        ("bar", "REQUESTS", req, req_caption or "", "zcode_request_percent", status.get("zcode_request_resets_at")),
        ("bar", "CACHE HIT", status.get("cache_hit_percent"), "", "cache_hit_percent", None),
    ]


def _draw_agent_panel(img, x0, status, bg=None, bg_name=None):
    state = status.get("state", "no session")
    stale = state == "no session"
    active_count = status.get("active_count", 0)
    is_active = not stale and active_count > 0
    accent = STATE_COLORS.get(state, FG_DIM)

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

    badge_size = 36
    _badge(draw, ix0, cy0 + PAD, badge_size, status["tool"][0], accent)
    draw.text((ix0 + badge_size + 14, cy0 + PAD + 2), status["tool"], font=_title_font(26), fill=FG)

    agg_label = "OFFLINE" if stale else (f"{active_count} ACTIVE" if active_count else "IDLE")
    af = _mono_font(14)
    aw = draw.textlength(agg_label, font=af)
    dot_r = 5
    draw.ellipse([ix1 - aw - 2 * dot_r - 10, cy0 + PAD + 8, ix1 - aw - 10, cy0 + PAD + 8 + 2 * dot_r], fill=accent)
    draw.text((ix1 - aw, cy0 + PAD + 6), agg_label, font=af, fill=FG_DIM)

    # Which provider/plan/model is actually active — the empty corner below
    # the ACTIVE/OFFLINE label has just enough room for this without costing
    # a dedicated row.
    identity = status.get("identity")
    plan_type = status.get("plan_type")
    if plan_type:
        identity = f"{identity} · {plan_type.replace('_', ' ').title()}" if identity else plan_type.replace("_", " ").title()
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

    sessions = status.get("sessions", [])
    row_h = 30
    if not sessions:
        draw.text((ix0, y + 4), "No active session", font=_title_font(16), fill=FG_FAINT)
        y += row_h * MAX_SESSION_ROWS
    else:
        shown = sessions[:MAX_SESSION_ROWS]
        for s in shown:
            _session_row(draw, ix0, ix1, y + 9, s)
            y += row_h
        remaining = len(sessions) - len(shown)
        slots_left = MAX_SESSION_ROWS - len(shown)
        if remaining > 0 and slots_left > 0:
            draw.text((ix0, y + 2), f"+{remaining} more", font=_mono_font(13), fill=FG_FAINT)
        y += row_h * slots_left

    metrics = _usage_metrics(status)
    # 3 rows (panels with a CACHE HIT bar added) need tighter spacing to still
    # fit above the footer line within the fixed card height — 2-row panels
    # keep the original roomier layout untouched. Decided before the sessions
    # divider so the reclaimed gap below can feed the USAGE rows their room.
    compact = len(metrics) > 2
    row_step = 47 if compact else 58

    y += 0 if compact else 8
    _hairline(draw, ix0, ix1, y)
    y += 20

    draw.text((ix0, y), "USAGE", font=_mono_font(12), fill=FG_FAINT)
    y += 22
    for kind, label, value, caption, metric_key, resets_at in metrics:
        if kind == "bar":
            trend = history.recent_values(status["tool"], metric_key) if metric_key else None
            warn = _predict_warning(status["tool"], metric_key, value, resets_at) if metric_key else None
            invert = metric_key == "cache_hit_percent"
            _bar_metric_row(draw, ix0, ix1, y, label, value, caption, trend=trend, warn=warn, invert=invert, compact=compact)
        else:
            _stat_metric_row(draw, ix0, ix1, y, label, value, caption)
        y += row_step

    lifetime_tokens = status.get("lifetime_total_tokens")
    if lifetime_tokens is not None:
        sessions_n = status.get("lifetime_session_count")
        cost = _human_cost(status.get("lifetime_cost_usd"))
        text = f"{_human_count(sessions_n)} sessions · {_human_count(lifetime_tokens)} tok"
        text += f" · ~{cost} all-time" if cost else " all-time"
        if status.get("credits_unlimited"):
            text += " · unlimited credits"
        elif status.get("credits_balance"):
            text += f" · ${status['credits_balance']:,.0f} credits"
        draw.text((ix0, y - 4), text, font=_mono_font(13), fill=FG_FAINT)
    elif status["tool"] == "zcode":
        sessions_today = status.get("sessions_today")
        session_tokens = status.get("session_tokens")
        if sessions_today or session_tokens is not None:
            text = f"{_human_count(sessions_today)} sessions today"
            if session_tokens is not None:
                text += f" · {_human_count(session_tokens)} tok (latest)"
            draw.text((ix0, y - 4), text, font=_mono_font(13), fill=FG_FAINT)


def _draw_hardware_panel(img, x0, hw, bg=None, bg_name=None):
    accent = _temp_color(hw.get("cpu_temp"))
    cx0, cy0 = x0 + CARD_MARGIN, CARD_MARGIN
    cx1, cy1 = x0 + PANEL_W - CARD_MARGIN, CANVAS_H - CARD_MARGIN
    _rounded_card(img, (cx0, cy0, cx1, cy1), 22, CARD_TOP, CARD_BOTTOM, outline=accent,
                  outline_width=3 if bg is not None else 2, bg=bg, bg_name=bg_name)
    draw = ImageDraw.Draw(img)

    ix0, ix1 = cx0 + PAD, cx1 - PAD

    badge_size = 36
    _badge(draw, ix0, cy0 + PAD, badge_size, "HW", accent)
    draw.text((ix0 + badge_size + 14, cy0 + PAD + 2), "Hardware", font=_title_font(26), fill=FG)

    clock = time.strftime("%H:%M:%S")
    cf = _mono_font(14)
    cw = draw.textlength(clock, font=cf)
    draw.text((ix1 - cw, cy0 + PAD + 8), clock, font=cf, fill=FG_DIM)

    y = cy0 + PAD + badge_size + 14
    _hairline(draw, ix0, ix1, y)
    y += 22

    mem_caption = f"{hw['mem_used_gb']:.1f} / {hw['mem_total_gb']:.0f} GB" if hw.get("mem_total_gb") else "—"
    disk_caption = f"{hw['disk_free_gb']:.0f} GB free" if hw.get("disk_free_gb") is not None else "—"
    temp_caption = f"{hw['cpu_temp']:.0f}°C" if hw.get("cpu_temp") is not None else "—"

    for label, pct, caption in (
        ("CPU", hw.get("cpu_usage"), temp_caption),
        ("MEMORY", hw.get("mem_percent"), mem_caption),
        ("DISK", hw.get("disk_percent"), disk_caption),
    ):
        _bar_metric_row(draw, ix0, ix1, y, label, pct, caption)
        y += 58

    y += 4
    _hairline(draw, ix0, ix1, y)
    y += 20

    fan, load1 = hw.get("fan_rpm"), hw.get("load1")
    _two_col_stat_row(
        draw, ix0, ix1, y,
        "FAN", f"{fan:.0f} RPM" if fan is not None else "—",
        "LOAD (1m)", f"{load1:.2f}" if load1 is not None else "—",
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
    draw.text((ix1 - nw, y - 2), net_text, font=nf, fill=FG_DIM)

    y += 30
    swap_used, swap_total = hw.get("swap_used_gb"), hw.get("swap_total_gb")
    swap_text = f"{swap_used:.1f}/{swap_total:.0f} GB" if swap_used is not None and swap_total else "—"
    _two_col_stat_row(
        draw, ix0, ix1, y,
        "UPTIME", _format_uptime(hw.get("uptime_sec")),
        "SWAP", swap_text,
    )


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
    from sources import claude_code, codex_cli, zcode, hardware

    background = sys.argv[1] if len(sys.argv) > 1 else None
    statuses = [claude_code.read_status(), codex_cli.read_status(), zcode.read_status()]
    hw = hardware.read_status()
    out = render(statuses, hw, background=background)
    out_path = Path(__file__).resolve().parent.parent / "preview.png"
    out.save(out_path)
    print(f"wrote {out_path}")
