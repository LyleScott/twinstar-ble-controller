"""Full vibe-check demo. Snapshots state, walks through a show, restores.

The show:

    1. off
    2. on, all channels at 100
    3. step the master down: 75 -> 50 -> 25
    4. solo each channel at 50: R, G, B, grow (W)
    5. smooth fade everything from 0 up to 20

About 30 seconds end-to-end. The fixture is returned to its pre-demo state
in a ``finally`` block, so Ctrl-C also restores cleanly.

    python examples/demo.py <address>

Requires ``pip install -e .`` from the project root first.
"""

from __future__ import annotations

import asyncio
import re
import sys

from bleak import BleakClient

from twinstar_ble import WRITE_CHAR, TwinstarClient

HOLD = 1.5  # seconds to hold each step so you can actually see it
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
        brightness = 100
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
    print(f"\n[restore] power={'on' if power_on else 'off'} master={brightness} {channels}")
    for ch in "rgbw":
        await ts.set_channel(ch, channels.get(ch, 100))
    await ts.set_channel("a", brightness)
    await ts.power(on=power_on)


async def fast_set(client: BleakClient, channel: str, level: int) -> None:
    """Fire a channel verb without waiting for / reading the reply.

    The library's `set_channel` does an ack'd write + read-back (~250ms).
    Fine for discrete steps, too slow for a smooth fade. For fade frames we
    skip the read entirely; the write is still ATT-acked, just not echoed.
    """
    payload = f"{channel.upper()}{max(0, min(100, level))}".encode("ascii")
    await client.write_gatt_char(WRITE_CHAR, payload, response=True)


async def fade(
    client: BleakClient, *, target: int, frames: int = 12, duration_s: float = 4.0
) -> None:
    """Linearly walk all four colour channels from 0 to ``target``."""
    sleep_per = duration_s / frames
    for i in range(1, frames + 1):
        level = int(target * i / frames)
        for ch in ("r", "g", "b", "w"):
            await fast_set(client, ch, level)
        await asyncio.sleep(sleep_per)


async def show(ts: TwinstarClient, client: BleakClient) -> None:
    """Run the five-step demo. Caller is responsible for snapshot/restore."""
    print("\n[1/5] off")
    await ts.power(on=False)
    await asyncio.sleep(HOLD)

    print("\n[2/5] on  ·  all channels at 100  ·  master 100")
    await ts.power(on=True)
    for ch in ("r", "g", "b", "w"):
        await ts.set_channel(ch, 100)
    await ts.set_channel("a", 100)
    await asyncio.sleep(HOLD)

    print("\n[3/5] master walkdown  ·  75 -> 50 -> 25")
    for level in (75, 50, 25):
        print(f"      master {level}")
        await ts.set_channel("a", level)
        await asyncio.sleep(HOLD)

    print("\n[4/5] solo each channel at 50")
    # Reset master so the solo levels read at face value.
    await ts.set_channel("a", 100)
    for solo, label in (("r", "red"), ("g", "green"), ("b", "blue"), ("w", "grow")):
        print(f"      {label} only")
        for ch in ("r", "g", "b", "w"):
            await ts.set_channel(ch, 50 if ch == solo else 0)
        await asyncio.sleep(HOLD)

    print("\n[5/5] fade all channels 0 -> 20")
    for ch in ("r", "g", "b", "w"):
        await ts.set_channel(ch, 0)
    await asyncio.sleep(0.4)
    await fade(client, target=20)


async def main(address: str) -> None:
    async with BleakClient(address, timeout=20.0) as client:
        print(f"connected mtu={client.mtu_size}")
        ts = TwinstarClient(client, verbose=False)  # quiet so the banners stand out

        saved = await snapshot(ts)
        print(f"saved: power={'on' if saved[0] else 'off'} master={saved[1]} {saved[2]}")
        try:
            await show(ts, client)
        finally:
            await restore(ts, *saved)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <address>")
    asyncio.run(main(sys.argv[1]))
