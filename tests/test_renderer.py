import unittest
from unittest import mock

from renderer import render as renderer
from scripts import theme


class SixPanelLayoutTests(unittest.TestCase):
    def test_explicit_online_health_keeps_empty_session_idle(self):
        self.assertFalse(
            renderer._is_offline_status({"state": "no session", "health": "online"})
        )

    def test_legacy_empty_session_remains_offline_without_health_probe(self):
        self.assertTrue(renderer._is_offline_status({"state": "no session"}))

    def test_layout_reserves_five_agents_and_hardware(self):
        self.assertEqual(renderer.PANEL_COUNT, 7)
        self.assertEqual(renderer.PANEL_W, renderer.CANVAS_W // 7)

    def test_background_card_opacity_is_twenty_percent(self):
        self.assertEqual(renderer.CARD_ALPHA_ON_BG, round(255 * 0.20))

    def test_root_background_is_available_and_brightness_normalized(self):
        self.assertIn("Background", theme._available_backgrounds())

        image = renderer._load_background("Background")

        self.assertIsNotNone(image)
        self.assertEqual(image.size, (renderer.CANVAS_W, renderer.CANVAS_H))
        mean_pixel = image.resize((1, 1)).getpixel((0, 0))
        self.assertLess(max(mean_pixel), 80)

    def test_kimi_usage_rows_use_subscription_windows(self):
        rows = renderer._usage_metrics({
            "tool": "Kimi Code",
            "kimi_monthly_percent": 14.62,
            "kimi_monthly_resets_at": "2030-02-01T00:00:00Z",
            "kimi_five_hour_percent": 12.5,
            "kimi_five_hour_resets_at": "2030-01-01T00:00:00Z",
            "kimi_weekly_percent": 37.0,
            "kimi_weekly_resets_at": "2030-01-02T00:00:00Z",
        })

        self.assertEqual([row[1] for row in rows], ["MONTHLY", "5-HOUR", "WEEKLY"])
        self.assertEqual([row[4] for row in rows],
                         ["kimi_monthly_percent", "kimi_five_hour_percent",
                          "kimi_weekly_percent"])

    def test_kimi_identity_caption_shows_plan_only(self):
        self.assertEqual(
            renderer._identity_caption({
                "tool": "Kimi Code",
                "identity": "K2.7 Coding",
                "plan_type": "Standard",
            }),
            "Standard",
        )

    def test_minimax_identity_caption_shows_token_plan_only(self):
        self.assertEqual(
            renderer._identity_caption({
                "tool": "MiniMax",
                "identity": "MiniMax-M3",
                "plan_type": "Token Plan",
            }),
            "Token Plan",
        )

    def test_display_name_changes_title_without_changing_internal_tool_key(self):
        status = {"tool": "zcode", "display_name": "GLM"}

        self.assertEqual(renderer._tool_display_name(status), "GLM")
        self.assertEqual(status["tool"], "zcode")

    def test_minimax_usage_rows_show_real_token_plan_windows(self):
        rows = renderer._usage_metrics({
            "tool": "MiniMax",
            "minimax_token_plan": True,
            "minimax_model_count": 8,
            "minimax_five_hour_percent": 25.0,
            "minimax_five_hour_remaining": 75,
            "minimax_five_hour_total": 100,
            "minimax_five_hour_resets_at": "2030-01-01T00:00:00Z",
            "minimax_weekly_percent": 75.0,
            "minimax_weekly_remaining": 100,
            "minimax_weekly_total": 400,
            "minimax_weekly_resets_at": "2030-01-02T00:00:00Z",
            "context_percent": 12.5,
            "context_tokens": 125_000,
            "context_window": 1_000_000,
            "cache_hit_percent": 80.0,
        })

        self.assertEqual(
            [row[1] for row in rows],
            ["5-HOUR", "WEEKLY", "CONTEXT", "CACHE HIT"],
        )
        self.assertEqual(
            [row[4] for row in rows],
            [
                "minimax_five_hour_percent",
                "minimax_weekly_percent",
                "context_percent",
                "cache_hit_percent",
            ],
        )
        self.assertIn("125.0K / 1.0M tok", rows[2][3])

    def test_minimax_usage_rows_never_fall_back_to_access_or_quota(self):
        rows = renderer._usage_metrics({
            "tool": "MiniMax",
            "minimax_token_plan": False,
            "context_percent": None,
            "cache_hit_percent": None,
        })

        self.assertEqual(
            [row[1] for row in rows],
            ["5-HOUR", "WEEKLY", "CONTEXT", "CACHE HIT"],
        )
        self.assertNotIn("ACCESS", [row[1] for row in rows])
        self.assertNotIn("QUOTA", [row[1] for row in rows])

    def test_minimax_unlimited_weekly_uses_infinity_sentinel(self):
        rows = renderer._usage_metrics({
            "tool": "MiniMax",
            "minimax_token_plan": True,
            "minimax_five_hour_percent": 6.0,
            "minimax_five_hour_status": 1,
            "minimax_weekly_percent": 0.0,
            "minimax_weekly_remaining": 0,
            "minimax_weekly_total": 0,
            "minimax_weekly_status": 3,
            "minimax_weekly_resets_at": "2030-01-02T00:00:00Z",
        })

        weekly_row = next(row for row in rows if row[1] == "WEEKLY")
        self.assertEqual(weekly_row[2], float("inf"))
        self.assertNotIn("unlimited", weekly_row[3])

    def test_bar_metric_row_renders_infinity_symbol_in_violet(self):
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (400, 80), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        renderer._bar_metric_row(draw, 10, 390, 10, "WEEKLY", float("inf"), "")

        pixels = list(img.get_flattened_data())
        self.assertIn(renderer.VIOLET, pixels)
        self.assertNotIn((255, 122, 92), pixels)  # BAD — must not read as an error state

    def test_pace_warning_keeps_the_reset_countdown(self):
        # The warning used to replace the caption outright, dropping the
        # "resets in …" countdown that makes the warning actionable.
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (400, 80), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        captured = []
        original = draw.text

        def spy(xy, text, *args, **kwargs):
            captured.append(text)
            return original(xy, text, *args, **kwargs)

        with mock.patch.object(draw, "text", side_effect=spy):
            renderer._bar_metric_row(
                draw, 10, 390, 10, "5-HOUR", 88.0, "resets in 2h 4m", warn=169.0
            )

        caption = next(t for t in captured if "resets in 2h 4m" in t)
        self.assertIn("169%", caption)

    def test_overlong_caption_is_ellipsized_to_the_row_width(self):
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (400, 80), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        x0, x1 = 10, 200
        captured = []
        original = draw.text

        def spy(xy, text, *args, **kwargs):
            captured.append((xy, text, kwargs.get("font")))
            return original(xy, text, *args, **kwargs)

        long_caption = "887/1000 left · resets in 15d · top: search-prime 69"
        with mock.patch.object(draw, "text", side_effect=spy):
            renderer._bar_metric_row(draw, x0, x1, 10, "TOOLS", 11.0, long_caption)

        xy, text, font = next(
            (xy, t, f) for xy, t, f in captured if "887/1000" in t
        )
        self.assertLessEqual(draw.textlength(text, font=font), x1 - x0)
        self.assertGreaterEqual(xy[0], x0)

    def test_zcode_rows_expose_five_hour_weekly_and_tools(self):
        rows = renderer._usage_metrics({
            "tool": "zcode",
            "zcode_five_hour_percent": 100,
            "zcode_five_hour_resets_at": 4102444800,
            "zcode_weekly_percent": 42,
            "zcode_weekly_resets_at": 4102444800,
            "zcode_request_percent": 11,
            "zcode_request_remaining": 887,
            "zcode_request_total": 1000,
            "cache_hit_percent": 99.8,
        })

        self.assertEqual(
            [row[1] for row in rows], ["5-HOUR", "WEEKLY", "TOOLS", "CACHE HIT"]
        )
        self.assertEqual(rows[0][4], "zcode_five_hour_percent")
        self.assertEqual(rows[1][4], "zcode_weekly_percent")

    def test_zcode_omits_weekly_row_when_plan_has_no_weekly_cap(self):
        rows = renderer._usage_metrics({
            "tool": "zcode",
            "zcode_five_hour_percent": 100,
            "zcode_weekly_percent": None,
            "zcode_request_percent": 11,
        })

        self.assertNotIn("WEEKLY", [row[1] for row in rows])

    def test_grok_combines_cli_and_bot_usage_in_one_panel(self):
        rows = renderer._usage_metrics(
            {
                "tool": "Grok",
                "context_percent": 61.0,
                "context_tokens": 309_830,
                "context_window": 500_000,
                "grok_bot_percent": 1.748973,
                "grok_bot_resets_at": 1_893_456_000,
                "grok_bot_period_percent": 3.0,
                "grok_bot_period_resets_at": 1_893_456_000,
                "cache_hit_percent": 92.8,
            }
        )

        self.assertEqual(
            [row[1] for row in rows],
            ["CLI CTX", "BOT INC", "BOT TOT", "CACHE HIT"],
        )
        self.assertEqual(
            [row[4] for row in rows],
            [
                "context_percent",
                "grok_bot_percent",
                "grok_bot_period_percent",
                "cache_hit_percent",
            ],
        )
        self.assertIn("309.8K / 500.0K tok", rows[0][3])

    def test_four_usage_rows_reserve_two_session_slots(self):
        metrics = renderer._usage_metrics({
            "tool": "MiniMax",
            "minimax_token_plan": True,
        })

        self.assertEqual(renderer._session_slot_count(metrics), 2)

    def test_hardware_side_values_map_fan_temp_and_swap(self):
        side = renderer._hardware_side_values({
            "fan_rpm": 2388,
            "cpu_temp": 72.4,
            "swap_used_gb": 1.25,
            "swap_total_gb": 8.0,
        })

        self.assertEqual([item["label"] for item in side], ["FAN", "TEMP", "SWAP"])
        self.assertEqual([item["value"] for item in side], ["2388 RPM", "72°C", "1.2/8 GB"])
        self.assertEqual(side[1]["color"], renderer.WARN)

    def test_hardware_side_values_show_zero_swap_total_as_zero_gb(self):
        side = renderer._hardware_side_values({
            "fan_rpm": None,
            "cpu_temp": None,
            "swap_used_gb": 0,
            "swap_total_gb": 0,
        })

        self.assertEqual(side[2]["value"], "0 GB")

    def test_seven_panel_render_keeps_expected_canvas_size(self):
        statuses = [
            {
                "tool": tool,
                "state": "no session",
                "sessions": [],
                "active_count": 0,
            }
            for tool in ("Claude Code", "Codex", "Kimi Code", "zcode", "MiniMax", "Grok")
        ]
        hw = {
            "cpu_temp": None,
            "cpu_usage": None,
            "mem_percent": None,
            "mem_used_gb": None,
            "mem_total_gb": None,
            "disk_percent": None,
            "disk_free_gb": None,
            "fan_rpm": None,
            "load1": None,
            "net_up_kbps": None,
            "net_down_kbps": None,
            "net_total_up_gb": None,
            "net_total_down_gb": None,
            "uptime_sec": None,
            "swap_used_gb": None,
            "swap_total_gb": None,
        }

        image = renderer.render(statuses, hw)

        self.assertEqual(image.size, (renderer.CANVAS_W, renderer.CANVAS_H))

    def test_seven_panel_render_with_minimax_unlimited_weekly(self):
        statuses = [
            {
                "tool": tool,
                "state": "no session",
                "sessions": [],
                "active_count": 0,
            }
            for tool in ("Claude Code", "Codex", "Kimi Code", "zcode", "Grok")
        ]
        statuses.append({
            "tool": "MiniMax",
            "state": "no session",
            "sessions": [],
            "active_count": 0,
            "minimax_token_plan": True,
            "minimax_five_hour_percent": 6.0,
            "minimax_five_hour_status": 1,
            "minimax_weekly_percent": 0.0,
            "minimax_weekly_remaining": 0,
            "minimax_weekly_total": 0,
            "minimax_weekly_status": 3,
            "minimax_weekly_resets_at": "2030-01-02T00:00:00Z",
        })
        hw = {
            "cpu_temp": None,
            "cpu_usage": None,
            "mem_percent": None,
            "mem_used_gb": None,
            "mem_total_gb": None,
            "disk_percent": None,
            "disk_free_gb": None,
            "fan_rpm": None,
            "load1": None,
            "net_up_kbps": None,
            "net_down_kbps": None,
            "net_total_up_gb": None,
            "net_total_down_gb": None,
            "uptime_sec": None,
            "swap_used_gb": None,
            "swap_total_gb": None,
        }

        image = renderer.render(statuses, hw)

        self.assertEqual(image.size, (renderer.CANVAS_W, renderer.CANVAS_H))
        pixels = list(image.get_flattened_data())
        self.assertIn(renderer.VIOLET, pixels)


if __name__ == "__main__":
    unittest.main()
