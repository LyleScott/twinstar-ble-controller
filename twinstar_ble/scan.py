"""Scan for nearby BLE devices and print them sorted by signal strength.

Run with the Twinstar fixture powered on and the iPhone's Bluetooth off (or
the Twinstar app force-quit). BLE peripherals usually only allow one central
connection at a time.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from bleak import BleakScanner

DEFAULT_SCAN_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class ScanRow:
    """Single row of scan output."""

    rssi: int
    address: str
    name: str
    local_name: str
    service_uuids: str

    def render(self) -> str:
        return (
            f"{self.rssi:>5}  {self.address:<40}  {self.name:<25}  "
            f"{self.local_name:<25}  {self.service_uuids}"
        )


async def scan(seconds: float) -> list[ScanRow]:
    """Discover BLE peripherals advertising during ``seconds``."""
    discovered = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows: list[ScanRow] = []
    for address, (device, adv) in discovered.items():
        rows.append(
            ScanRow(
                rssi=adv.rssi if adv else -999,
                address=address,
                name=device.name or "(no name)",
                local_name=(adv.local_name if adv else "") or "",
                service_uuids=",".join(adv.service_uuids) if adv and adv.service_uuids else "",
            )
        )
    rows.sort(key=lambda r: r.rssi, reverse=True)
    return rows


def _print_table(rows: list[ScanRow]) -> None:
    """Pretty-print scan rows as a fixed-width table."""
    print(f"{'RSSI':>5}  {'ADDRESS':<40}  {'NAME':<25}  {'LOCAL_NAME':<25}  ADV_SERVICE_UUIDS")
    print("-" * 140)
    for row in rows:
        print(row.render())


def _filter_rows(rows: list[ScanRow], name_filter: str | None) -> list[ScanRow]:
    """Keep only rows whose name or local_name contains `name_filter` (case-insensitive)."""
    if not name_filter:
        return rows
    q = name_filter.lower()
    return [r for r in rows if q in r.name.lower() or q in r.local_name.lower()]


async def _main_async(seconds: float, name_filter: str | None) -> None:
    label = f" matching {name_filter!r}" if name_filter else ""
    print(f"Scanning for {seconds:.0f}s{label}...\n")
    rows = _filter_rows(await scan(seconds), name_filter)
    _print_table(rows)
    print(f"\n{len(rows)} device(s) found.")
    if not name_filter:
        print(
            "\nTip: most commands accept a name substring directly, e.g."
            "\n  twinstar-ble twinstar on"
            "\nNo need to copy the long address unless you want to."
        )


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "name_filter",
        nargs="?",
        help="optional name substring to filter results (case-insensitive)",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=DEFAULT_SCAN_SECONDS,
        help=f"scan duration in seconds (default {DEFAULT_SCAN_SECONDS:.0f})",
    )
    args = parser.parse_args()
    asyncio.run(_main_async(args.seconds, args.name_filter))


if __name__ == "__main__":
    main()
