"""Shared pytest fixtures.

These tests don't talk to real BLE hardware; they exercise the encoding /
routing / interpolation logic with a stub BleakClient that records calls.

Assumes the package is installed (``pip install -e ".[dev]"``); CI does this
automatically. No sys.path shimming required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_bleak_client():
    """A MagicMock that satisfies the methods TwinstarClient calls.

    `read_gatt_char` returns ``b"OK"`` by default; tests that need a
    specific reply override `mock.read_gatt_char.return_value`.
    """
    client = MagicMock()
    client.write_gatt_char = AsyncMock(return_value=None)
    client.read_gatt_char = AsyncMock(return_value=b"OK")
    client.is_connected = True
    client.mtu_size = 256
    return client
