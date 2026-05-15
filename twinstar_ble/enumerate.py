"""Connect to a BLE device by address and dump its GATT service tree.

The address comes from ``scan.py``. On macOS it's a UUID like
``XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX`` because Apple hides hardware MAC
addresses; on Linux/Windows it's the typical ``AA:BB:CC:DD:EE:FF`` form.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field

from bleak import BleakClient

NORDIC_UART_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"


@dataclass(slots=True)
class CharacteristicSummary:
    """A single characteristic and its capabilities for the closing summary."""

    service_uuid: str
    char_uuid: str
    properties: tuple[str, ...]


@dataclass(slots=True)
class EnumerationResult:
    """Aggregate result of enumerating a device's GATT tree."""

    found_nordic_uart: bool = False
    writeable: list[CharacteristicSummary] = field(default_factory=list)
    notifying: list[CharacteristicSummary] = field(default_factory=list)


def _classify(
    service_uuid: str,
    char_uuid: str,
    properties: list[str],
    result: EnumerationResult,
) -> None:
    """Update ``result`` with notable facts about a single characteristic."""
    if service_uuid.lower() == NORDIC_UART_SERVICE:
        result.found_nordic_uart = True
    summary = CharacteristicSummary(service_uuid, char_uuid, tuple(properties))
    if {"write", "write-without-response"} & set(properties):
        result.writeable.append(summary)
    if {"notify", "indicate"} & set(properties):
        result.notifying.append(summary)


async def enumerate_device(address: str) -> EnumerationResult:
    """Connect, walk the GATT tree, and print/collect findings.

    Args:
        address: Peripheral address as accepted by Bleak.

    Returns:
        Summary of writeable + notifying characteristics and whether the
        well-known Nordic UART service is present.
    """
    print(f"Connecting to {address} ...")
    result = EnumerationResult()
    async with BleakClient(address, timeout=20.0) as client:
        print(f"  connected: {client.is_connected}")
        print(f"  MTU: {client.mtu_size} bytes\n")

        for service in client.services:
            print(f"[Service] {service.uuid}")
            if service.description and service.description != "Unknown":
                print(f"           {service.description}")

            for char in service.characteristics:
                props = list(char.properties)
                desc = (
                    char.description if char.description and char.description != "Unknown" else ""
                )
                tail = f"  {desc}" if desc else ""
                print(f"  [Char]  {char.uuid}  ({','.join(props)}){tail}")
                _classify(service.uuid, char.uuid, props, result)

                for descriptor in char.descriptors:
                    print(f"    [Desc] {descriptor.uuid}  {descriptor.description or ''}")
            print()

    return result


def _print_summary(result: EnumerationResult) -> None:
    """Print the human-readable summary block at the end of enumeration."""
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Nordic UART service present: {result.found_nordic_uart}")
    if result.found_nordic_uart:
        print("  -> Try the Chihiros open-source protocol; high chance of compatibility.")

    print(f"\nWriteable characteristics ({len(result.writeable)}):")
    for w in result.writeable:
        print(f"  service={w.service_uuid}  char={w.char_uuid}  ({','.join(w.properties)})")

    print(f"\nNotifying characteristics ({len(result.notifying)}):")
    for n in result.notifying:
        print(f"  service={n.service_uuid}  char={n.char_uuid}  ({','.join(n.properties)})")


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address", help="BLE address from scan.py")
    args = parser.parse_args()
    result = asyncio.run(enumerate_device(args.address))
    _print_summary(result)


if __name__ == "__main__":
    main()
