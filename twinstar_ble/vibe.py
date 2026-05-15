"""Ambient day-cycle animation + ANSI preview for the Twinstar fixture.

Runs a slow `night -> sunrise -> noon -> sunset -> night` color cycle, sends
matching commands to the fixture, and paints a synced ANSI tank in the
terminal so you can see (approximately) what the light is currently trying
to be. Ctrl-C exits and turns the light off.

    twinstar-vibe <address>                # 30s/cycle, looping forever
    twinstar-vibe <address> --speed 0.5    # half speed (60s/cycle)
    twinstar-vibe <address> --speed 4      # blast through; useful for screencasts
    twinstar-vibe <address> --cycles 4     # run 4 full cycles then stop
    twinstar-vibe --no-send                # preview only, no BLE connection

Why two tasks instead of one loop: BLE writes are slow (~250ms each, and we
emit up to 5 per scene change). Decoupling render (snappy) from send (paced)
keeps the preview at ~15 fps while the radio stays well-mannered.

Hardware kindness
-----------------
The send task is intentionally conservative:

  * Pacing defaults to 0.6s between scene-push attempts (~5 channel-writes/s
    peak during transitions, ~1-2/s during plateaus).
  * Channels that haven't moved by >=3 since the last push are skipped.
  * Power is toggled once on entry and once on exit, never per scene.

Net result is similar to gently dragging an app slider, which is well within
what the official app produces during normal use. For all-night ambient
runs, that's fine; if you want even friendlier numbers, raise `--pacing`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from dataclasses import dataclass

from bleak import BleakClient

from . import TwinstarClient, resolve_address

# ---------------------------------------------------------------------------
# Scene model + day cycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Scene:
    """A single light state (channel intensities, all 0-100)."""

    r: int
    g: int
    b: int
    w: int
    a: int  # master brightness


# Day-cycle keyframes. Each row is (phase, R, G, B, W, A, label).
# Phase is monotonic in [0, 1) and wraps. The labels are a vibe, not science.
# Hand-tuned so the *mixed* preview color reads roughly like time-of-day on a
# planted tank: amber sunrise, white-ish noon, red sunset, dim blue at night.
KEYFRAMES: list[tuple[float, int, int, int, int, int, str]] = [
    (0.00, 0, 0, 30, 0, 8, "night"),
    (0.10, 40, 10, 20, 10, 25, "dawn"),
    (0.20, 90, 30, 10, 60, 60, "sunrise"),
    (0.35, 60, 60, 20, 100, 85, "morning"),
    (0.50, 50, 65, 55, 100, 100, "noon"),
    (0.65, 60, 60, 30, 100, 90, "afternoon"),
    (0.78, 100, 30, 10, 50, 70, "sunset"),
    (0.88, 60, 10, 10, 10, 30, "dusk"),
    (0.95, 10, 0, 30, 0, 12, "twilight"),
    (1.00, 0, 0, 30, 0, 8, "night"),
]


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def palette(phase: float) -> tuple[Scene, str]:
    """Linearly interpolate the keyframe table at ``phase`` in [0, 1)."""
    phase = phase % 1.0
    for p0, p1 in zip(KEYFRAMES, KEYFRAMES[1:], strict=False):
        if p0[0] <= phase <= p1[0]:
            span = p1[0] - p0[0]
            t = 0.0 if span == 0 else (phase - p0[0]) / span
            scene = Scene(
                int(_lerp(p0[1], p1[1], t)),
                int(_lerp(p0[2], p1[2], t)),
                int(_lerp(p0[3], p1[3], t)),
                int(_lerp(p0[4], p1[4], t)),
                int(_lerp(p0[5], p1[5], t)),
            )
            return scene, (p0[6] if t < 0.5 else p1[6])
    return Scene(0, 0, 0, 0, 0), "night"


# ---------------------------------------------------------------------------
# Color mixing for the preview
# ---------------------------------------------------------------------------


def mix(scene: Scene) -> tuple[int, int, int]:
    """Approximate the fixture's emitted color as 8-bit sRGB.

    The W channel is a warm white LED; weight it heavier toward R/G than B
    so the preview reads as warm rather than neutral. Master brightness ``a``
    multiplies everything (matches what the firmware actually does).
    """
    a = scene.a / 100.0
    r = (scene.r * 2.55 + scene.w * 2.0) * a
    g = (scene.g * 2.55 + scene.w * 1.7) * a
    b = (scene.b * 2.55 + scene.w * 1.0) * a
    return tuple(max(0, min(255, int(c))) for c in (r, g, b))  # type: ignore[return-value]


def _scale(rgb: tuple[int, int, int], k: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(c * k))) for c in rgb)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# ANSI rendering
# ---------------------------------------------------------------------------

RESET = "\x1b[0m"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
WIDTH = 60  # interior width of the tank


def _bg(rgb: tuple[int, int, int]) -> str:
    return f"\x1b[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def _fg(rgb: tuple[int, int, int]) -> str:
    return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def _wallclock(phase: float) -> str:
    """Phase [0,1) -> a 24h clock string."""
    minutes = int(phase * 24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _water_row(
    water_rgb: tuple[int, int, int], plant_rgb: tuple[int, int, int], plant_cols: list[int]
) -> str:
    """Build one bg-tinted water row with `}`-shaped plants at fixed columns."""
    cells = [" "] * WIDTH
    for c in plant_cols:
        if 0 <= c < WIDTH:
            cells[c] = f"{_fg(plant_rgb)}}}{_bg(water_rgb)}"
    return "  \u2502" + _bg(water_rgb) + "".join(cells) + RESET + "\u2502"


def render(scene: Scene, label: str, phase: float, speed: float, sending: bool) -> list[str]:
    """Build the per-frame lines. Caller positions the cursor."""
    light_rgb = mix(scene)
    water_rgb = _scale(light_rgb, 0.45)
    plant_rgb = _scale(light_rgb, 0.55)

    title = (
        f"  TWINSTAR 450S V    {label:<10}  \u00b7  {_wallclock(phase)}  \u00b7  {speed:g}x speed"
    )
    values = (
        f"  R{scene.r:>4}    G{scene.g:>4}    B{scene.b:>4}    W{scene.w:>4}"
        f"    \u00b7    master {scene.a:>3}"
    )

    box_top = "  \u256d" + "\u2500" * WIDTH + "\u256e"
    box_bot = "  \u2570" + "\u2500" * WIDTH + "\u256f"

    bar = "  " + _bg(light_rgb) + " " * WIDTH + RESET  # the light fixture itself

    mode = "\x1b[32msending\x1b[0m" if sending else "\x1b[33mpreview only (--no-send)\x1b[0m"
    footer = f"    Ctrl-C to exit  \u00b7  {mode}"

    return [
        "",
        box_top,
        "  \u2502" + title.ljust(WIDTH) + "\u2502",
        "  \u2502" + values.ljust(WIDTH) + "\u2502",
        box_bot,
        bar,
        box_top,
        _water_row(water_rgb, plant_rgb, [9, 23, 38, 51]),
        _water_row(water_rgb, plant_rgb, [4, 17, 31, 44, 56]),
        _water_row(water_rgb, plant_rgb, [12, 28, 41, 53]),
        box_bot,
        footer,
        "",
    ]


# ---------------------------------------------------------------------------
# Animation loop
# ---------------------------------------------------------------------------


@dataclass
class _State:
    """Shared between the clock, render, and send tasks."""

    scene: Scene = Scene(0, 0, 0, 0, 0)
    label: str = "night"
    phase: float = 0.0


async def _clock_task(state: _State, period: float, cycles: int) -> None:
    """Walk `phase` forward in real time and update the shared scene.

    If ``cycles`` > 0, exits cleanly after that many full day cycles by
    raising CancelledError so the parent gather() unwinds the other tasks.
    """
    start = time.monotonic()
    deadline = start + period * cycles if cycles > 0 else None
    while True:
        elapsed = time.monotonic() - start
        if deadline is not None and time.monotonic() >= deadline:
            raise asyncio.CancelledError
        state.phase = (elapsed / period) % 1.0
        state.scene, state.label = palette(state.phase)
        await asyncio.sleep(1 / 30)


async def _render_task(state: _State, speed: float, sending: bool) -> None:
    """Repaint the tank in place at ~15 fps using ANSI cursor moves."""
    sys.stdout.write(HIDE_CURSOR)
    drawn = 0
    try:
        while True:
            lines = render(state.scene, state.label, state.phase, speed, sending)
            # First frame: just print. Subsequent frames: jump back up over
            # what we drew last time, then overwrite each line + clear-to-EOL.
            if drawn:
                sys.stdout.write(f"\x1b[{drawn}F")  # cursor up `drawn` lines, col 0
            for line in lines:
                sys.stdout.write(line + "\x1b[K\n")  # \x1b[K clears to end of line
            drawn = len(lines)
            sys.stdout.flush()
            await asyncio.sleep(1 / 15)
    finally:
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.flush()


async def _send_task(ts: TwinstarClient, state: _State, pacing: float) -> None:
    """Push current scene to the fixture, only emitting verbs that changed."""
    last: Scene | None = None
    # Power-on once at the start; we modulate brightness via A from there.
    await ts.power(on=True)
    while True:
        s = state.scene
        if last is None:
            # First send: every channel.
            for ch, val in (("r", s.r), ("g", s.g), ("b", s.b), ("w", s.w), ("a", s.a)):
                await ts.set_channel(ch, val)
        else:
            # Only send channels that moved by >=3 (smaller deltas aren't
            # worth a full BLE round-trip; the firmware bumps A1->A2 anyway,
            # and being miserly here keeps the long-run write rate friendly
            # in case the firmware persists state to flash on every command).
            for ch, cur, prev in (
                ("r", s.r, last.r),
                ("g", s.g, last.g),
                ("b", s.b, last.b),
                ("w", s.w, last.w),
                ("a", s.a, last.a),
            ):
                if abs(cur - prev) >= 3:
                    await ts.set_channel(ch, cur)
        last = s
        await asyncio.sleep(pacing)


async def _vibe(
    address: str | None, speed: float, no_send: bool, pacing: float, cycles: int
) -> None:
    """Wire up the three tasks and run them until cancelled or cycles done."""
    period = 30.0 / speed  # one full day cycle in `period` seconds
    state = _State()

    if no_send:
        # Pure preview mode; skip BLE entirely.
        await _run_until_cancel(
            state, period, speed, sending=False, ts=None, pacing=pacing, cycles=cycles
        )
        return

    if address is None:
        sys.exit("a device address or name is required unless --no-send")

    address = await resolve_address(address)
    async with BleakClient(address, timeout=20.0) as client:
        ts = TwinstarClient(client, verbose=False)  # don't dump wire frames over the art
        try:
            await _run_until_cancel(
                state, period, speed, sending=True, ts=ts, pacing=pacing, cycles=cycles
            )
        finally:
            # Be a polite guest: turn it off when we're done. Already exiting,
            # so a write failure here isn't worth surfacing.
            with contextlib.suppress(Exception):
                await ts.power(on=False)


async def _run_until_cancel(
    state: _State,
    period: float,
    speed: float,
    sending: bool,
    ts: TwinstarClient | None,
    pacing: float,
    cycles: int,
) -> None:
    tasks = [
        asyncio.create_task(_clock_task(state, period, cycles)),
        asyncio.create_task(_render_task(state, speed, sending)),
    ]
    if ts is not None:
        tasks.append(asyncio.create_task(_send_task(ts, state, pacing)))
    try:
        await asyncio.gather(*tasks)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        for t in tasks:
            t.cancel()
        # Let the render task's `finally` run so the cursor comes back.
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vibe",
        description="Animated day-cycle preview for a Twinstar fixture.",
    )
    p.add_argument(
        "address",
        nargs="?",
        metavar="ADDR_OR_NAME",
        help="BLE address or a name substring like 'twinstar' (omit only with --no-send)",
    )
    p.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="cycle speed multiplier (1.0 = 30s/day, 2.0 = 15s, 0.5 = 60s)",
    )
    p.add_argument(
        "--no-send", action="store_true", help="render only; do not connect to the fixture"
    )
    p.add_argument(
        "--pacing",
        type=float,
        default=0.6,
        help="seconds between scene-push attempts (default 0.6, conservative)",
    )
    p.add_argument(
        "--cycles",
        type=int,
        default=0,
        help="stop after N full day cycles (default 0 = loop until Ctrl-C)",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    # asyncio.run cancels tasks on Ctrl-C; the render task's `finally` restores
    # the cursor. Catching here just suppresses the traceback at the very end.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_vibe(args.address, args.speed, args.no_send, args.pacing, args.cycles))


if __name__ == "__main__":
    main()
