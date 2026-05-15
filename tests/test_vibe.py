"""Unit tests for vibe: keyframe interpolation, color mixing, render, CLI."""

from __future__ import annotations

import asyncio

import pytest

from twinstar_ble import vibe

# --- palette / interpolation ---------------------------------------------


class TestPalette:
    def test_phase_zero_is_night(self):
        scene, label = vibe.palette(0.0)
        assert label == "night"
        assert scene.r == 0
        assert scene.g == 0
        assert scene.b > 0  # dim blue, not pure black

    def test_phase_wraps(self):
        # 0.0 and 1.0 are the same scene (the keyframe table closes the loop).
        s1, _ = vibe.palette(0.0)
        s2, _ = vibe.palette(1.0)
        assert s1 == s2

    def test_phase_above_one_wraps(self):
        s1, _ = vibe.palette(0.25)
        s2, _ = vibe.palette(1.25)
        assert s1 == s2

    def test_keyframe_value_returned_at_phase(self):
        # Noon keyframe: phase=0.50, scene=(50, 65, 55, 100, 100), label='noon'.
        scene, label = vibe.palette(0.50)
        assert label == "noon"
        assert scene == vibe.Scene(50, 65, 55, 100, 100)

    def test_interpolation_midway(self):
        # Halfway between night (b=30 at 0.00) and dawn (b=20 at 0.10) is phase 0.05.
        # Linear interp: b should land at 25 +/- rounding.
        scene, _ = vibe.palette(0.05)
        assert 24 <= scene.b <= 26

    def test_label_picks_nearer_keyframe(self):
        # Just past sunrise (0.20) but before morning (0.35), at 0.21:
        # closer to sunrise, so label should be "sunrise".
        _, near_sunrise = vibe.palette(0.21)
        _, near_morning = vibe.palette(0.34)
        assert near_sunrise == "sunrise"
        assert near_morning == "morning"

    def test_full_cycle_does_not_crash(self):
        # Sweep 100 phases across [0, 1) and confirm every result is a sane Scene.
        for i in range(100):
            scene, label = vibe.palette(i / 100)
            assert isinstance(scene, vibe.Scene)
            assert isinstance(label, str)
            for v in (scene.r, scene.g, scene.b, scene.w, scene.a):
                assert 0 <= v <= 100


# --- color mixing ---------------------------------------------------------


class TestMix:
    def test_off_scene_is_black(self):
        assert vibe.mix(vibe.Scene(0, 0, 0, 0, 0)) == (0, 0, 0)

    def test_master_zero_kills_output(self):
        # A=0 must zero everything regardless of R/G/B/W.
        scene = vibe.Scene(100, 100, 100, 100, 0)
        assert vibe.mix(scene) == (0, 0, 0)

    def test_red_only(self):
        r, g, b = vibe.mix(vibe.Scene(100, 0, 0, 0, 100))
        assert r > 200
        assert g == 0
        assert b == 0

    def test_white_warms_red_more_than_blue(self):
        # W is a warm-white LED; preview weighting should reflect that
        # so the eye reads it as warm rather than neutral.
        r, g, b = vibe.mix(vibe.Scene(0, 0, 0, 100, 100))
        assert r > g > b

    def test_clamps_at_255(self):
        # All channels at 100 + W contribution can sum > 255 per channel;
        # output must be clamped to 8-bit sRGB.
        r, g, b = vibe.mix(vibe.Scene(100, 100, 100, 100, 100))
        assert r <= 255 and g <= 255 and b <= 255
        assert r >= 0 and g >= 0 and b >= 0


# --- ANSI / rendering helpers --------------------------------------------


class TestAnsiHelpers:
    def test_bg_format(self):
        assert vibe._bg((100, 200, 50)) == "\x1b[48;2;100;200;50m"

    def test_fg_format(self):
        assert vibe._fg((100, 200, 50)) == "\x1b[38;2;100;200;50m"

    def test_scale_zero(self):
        assert vibe._scale((100, 200, 50), 0) == (0, 0, 0)

    def test_scale_unity(self):
        assert vibe._scale((100, 200, 50), 1.0) == (100, 200, 50)

    def test_scale_clamps_at_255(self):
        assert vibe._scale((100, 100, 100), 10) == (255, 255, 255)


class TestWallclock:
    @pytest.mark.parametrize(
        "phase,expected",
        [
            (0.0, "00:00"),
            (0.25, "06:00"),
            (0.5, "12:00"),
            (0.75, "18:00"),
            # 23:59 ~ phase = (23*60+59)/1440 ~ 0.99931
            (0.99931, "23:59"),
        ],
    )
    def test_phase_to_clock(self, phase, expected):
        assert vibe._wallclock(phase) == expected


class TestRender:
    def test_returns_lines(self):
        scene = vibe.Scene(50, 50, 50, 50, 50)
        lines = vibe.render(scene, "noon", 0.5, 1.0, sending=True)
        assert isinstance(lines, list)
        assert len(lines) > 5

    def test_includes_title_and_clock(self):
        scene = vibe.Scene(50, 50, 50, 50, 50)
        joined = "\n".join(vibe.render(scene, "noon", 0.5, 1.0, sending=True))
        assert "TWINSTAR" in joined
        assert "12:00" in joined
        assert "noon" in joined

    def test_indicates_send_mode_in_footer(self):
        joined_send = "\n".join(
            vibe.render(vibe.Scene(0, 0, 0, 0, 0), "night", 0.0, 1.0, sending=True)
        )
        joined_preview = "\n".join(
            vibe.render(vibe.Scene(0, 0, 0, 0, 0), "night", 0.0, 1.0, sending=False)
        )
        assert "sending" in joined_send
        assert "preview" in joined_preview


# --- async loops ---------------------------------------------------------


class TestClockTask:
    async def test_zero_cycles_loops_forever(self):
        # cycles=0 means "loop until cancelled". Verify it doesn't self-exit.
        state = vibe._State()
        task = asyncio.create_task(vibe._clock_task(state, period=0.1, cycles=0))
        await asyncio.sleep(0.15)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_finite_cycles_exits_cleanly(self):
        # cycles=2, period=0.1 -> exits in ~0.2s by raising CancelledError.
        state = vibe._State()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                vibe._clock_task(state, period=0.1, cycles=2),
                timeout=1.0,
            )


# --- CLI argparse --------------------------------------------------------


class TestCli:
    def test_defaults(self):
        args = vibe._build_parser().parse_args(["ADDR"])
        assert args.address == "ADDR"
        assert args.speed == 1.0
        assert args.pacing == 0.6
        assert args.cycles == 0
        assert args.no_send is False

    def test_no_send_omits_address(self):
        args = vibe._build_parser().parse_args(["--no-send"])
        assert args.no_send is True
        assert args.address is None

    def test_cycles_flag(self):
        args = vibe._build_parser().parse_args(["ADDR", "--cycles", "4"])
        assert args.cycles == 4

    def test_pacing_override(self):
        args = vibe._build_parser().parse_args(["ADDR", "--pacing", "1.0"])
        assert args.pacing == 1.0

    def test_speed_override(self):
        args = vibe._build_parser().parse_args(["ADDR", "--speed", "0.25"])
        assert args.speed == 0.25
