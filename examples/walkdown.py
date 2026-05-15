"""Polite cookbook example: snapshot, demo, restore.

Saves the fixture's current state (power, master brightness, R/G/B/W),
runs a quick brightness walkdown demo at full color, then puts everything
back the way it was; the user shouldn't be able to tell the script ran
once the script exits.

Demo shape: power off briefly, bring up to max master + max R/G/B/W, walk
master 100 -> 75 -> 50 -> 25, off. Restore happens in a ``finally`` block
so Ctrl-C still leaves the fixture in its original state.

    python examples/walkdown.py <address>

Requires ``pip install -e .`` from the project root first.
"""

from __future__ import annotations

import asyncio
import re
import sys

from bleak import BleakClient

from twinstar_ble import TwinstarClient

# colorvalues comes back as `CL:R,G,B,W`; reuse the same shape the library
# pretty-printer parses, but keep this script self-contained.
_COLOR_RE = re.compile(r"CL:(\d+),(\d+),(\d+),(\d+)", re.IGNORECASE)


async def snapshot(ts: TwinstarClient) -> tuple[bool, int, dict[str, int]]:
    """Read current power, master brightness, and R/G/B/W levels."""
    power_raw = (await ts.query("power") or b"").decode("ascii", errors="replace").strip()
    bright_raw = (await ts.query("brightness") or b"").decode("ascii", errors="replace").strip()
    color_raw = (await ts.query("color") or b"").decode("ascii", errors="replace").strip()

    power_on = power_raw.upper() in {"ON", "1", "TRUE"}
    try:
        brightness = int(bright_raw)
    except ValueError:
        brightness = 100  # safe fallback if the firmware ever returns nonsense
    m = _COLOR_RE.search(color_raw)
    channels = (
        dict(zip("rgbw", (int(v) for v in m.groups()), strict=True))
        if m
        else {"r": 100, "g": 100, "b": 100, "w": 100}
    )
    return power_on, brightness, channels


async def restore(
    ts: TwinstarClient, power_on: bool, brightness: int, channels: dict[str, int]
) -> None:
    """Re-apply a snapshot: channels first, then master, then power state."""
    print(f"restoring: power={'on' if power_on else 'off'} master={brightness} {channels}")
    for ch in "rgbw":
        await ts.set_channel(ch, channels.get(ch, 100))
    await ts.set_channel("a", brightness)
    await ts.power(on=power_on)


async def demo(ts: TwinstarClient) -> None:
    """Off briefly, full max, master walkdown 100 -> 75 -> 50 -> 25, off."""
    await ts.power(on=False)
    await asyncio.sleep(0.6)

    for ch in "rgbw":
        await ts.set_channel(ch, 100)
    await ts.set_channel("a", 100)
    await ts.power(on=True)
    await asyncio.sleep(1.5)

    for level in (100, 75, 50, 25):
        print(f"master {level}")
        await ts.set_channel("a", level)
        await asyncio.sleep(2.0)

    await ts.power(on=False)
    await asyncio.sleep(0.6)


async def main(address: str) -> None:
    async with BleakClient(address, timeout=20.0) as client:
        ts = TwinstarClient(client, verbose=False)
        saved = await snapshot(ts)
        print(f"saved: power={'on' if saved[0] else 'off'} master={saved[1]} {saved[2]}")
        try:
            await demo(ts)
        finally:
            await restore(ts, *saved)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <address>")
    asyncio.run(main(sys.argv[1]))
