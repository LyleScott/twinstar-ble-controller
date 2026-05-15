"""Twinstar Light Pro / Dimmer BLE controller.

A small library plus CLI for driving a Twinstar S-Series aquarium fixture
(BLE name `Twinstar Light Pro` / `Twinstar_Dimmer`) over Bluetooth Low Energy.

Protocol summary
----------------
The fixture exposes one custom GATT service:

    Service        00000180-0000-1000-8000-00805f9b34fb  (vendor specific)
    Write char     0000dead-...                          (commands go here)
    Read char      0000fef4-...                          (responses live here)
    RTC char       0000fef5-...                          (clock get/set only)
    OTA char       0000feed-...                          (firmware update; do not touch)

Commands are plain ASCII written to the WRITE characteristic, no framing,
no length prefix, no checksum. Responses are read from the READ characteristic
on demand (the firmware does not push notifications by default).

Verbs
-----
    on / off                            power
    A<n>                                master brightness 0-100 (1 -> 2)
    R<n> G<n> B<n> W<n>                 per-channel intensity 0-100
    version                             query firmware version
    powerstatus / brightlevel /         state queries
        colorvalues
    schedulelist / scheduleday /        schedule queries
        schedulestate
    timerstate / timer2state /          timer/boost queries
        booststate / boost2state
    username

The RTC characteristic accepts a 14-byte ASCII timestamp YYYYMMDDHHMMSS and
returns the same shape on read. The fixture has no persistent clock, so it
counts up from the firmware epoch (unix, 1970-01-01 00:00:00) on each power
cycle. Re-sync with ``set-time`` after every power-up.

Usage as CLI
------------
After ``pip install -e .`` the package exposes ``twinstar-ble`` as a console
script. The first positional argument is either a literal BLE address (macOS
UUID or classic MAC) or a name substring like ``twinstar`` that we'll briefly
scan for; the two are interchangeable, pick whichever feels right.

    twinstar-ble twinstar on
    twinstar-ble twinstar a 50
    twinstar-ble twinstar r 100
    twinstar-ble twinstar query color
    twinstar-ble twinstar set-time
    twinstar-ble twinstar repl
    twinstar-ble B0223E4B-CDCF-ECEC-2817-AED323FA1090 on   # bypass scan

You can also invoke the module directly without the console script:

    python -m twinstar_ble twinstar on
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from bleak import BleakClient, BleakScanner

__all__ = [
    "WRITE_CHAR",
    "READ_CHAR",
    "RTC_CHAR",
    "CHANNEL_VERBS",
    "QUERY_VERBS",
    "TwinstarClient",
    "format_response",
    "format_timestamp",
    "resolve_address",
]


# GATT characteristic UUIDs. The fixture splits responsibilities across
# three characteristics rather than multiplexing everything onto one;
# writing the wrong verb to the wrong char silently does nothing.
WRITE_CHAR = "0000dead-0000-1000-8000-00805f9b34fb"  # ASCII verbs go here
READ_CHAR = "0000fef4-0000-1000-8000-00805f9b34fb"  # poll here for replies
RTC_CHAR = "0000fef5-0000-1000-8000-00805f9b34fb"  # clock get/set ONLY

# Letter prefixes the firmware uses for each adjustable channel.
CHANNEL_VERBS: dict[str, str] = {
    "a": "A",  # master brightness ("All")
    "r": "R",
    "g": "G",
    "b": "B",
    "w": "W",  # white / grow channel
}

# Friendly query name -> wire verb. The friendly names are what the CLI
# accepts; the right-hand strings are exactly what the firmware expects.
QUERY_VERBS: dict[str, str] = {
    "power": "powerstatus",
    "brightness": "brightlevel",
    "color": "colorvalues",
    "version": "version",
    "schedule": "schedulestate",
    "schedulelist": "schedulelist",
    "scheduleday": "scheduleday",
    "timer": "timerstate",
    "timer2": "timer2state",
    "boost": "booststate",
    "boost2": "boost2state",
    "username": "username",
}

# Firmware accepts brightness levels 0-100 inclusive; level 1 is silently
# bumped to 2 by the firmware (likely a minimum-flicker guard).
BRIGHTNESS_MIN = 0
BRIGHTNESS_MAX = 100

# RTC payload format expected by the fixture.
RTC_FORMAT = "%Y%m%d%H%M%S"


def format_timestamp(when: datetime | None = None) -> bytes:
    """Render a datetime as the 14-byte ASCII string the RTC characteristic expects.

    Args:
        when: Time to encode. Defaults to ``datetime.now()``.

    Returns:
        ASCII-encoded ``YYYYMMDDHHMMSS``.
    """
    return (when or datetime.now()).strftime(RTC_FORMAT).encode("ascii")


def _hex(data: bytes) -> str:
    """Render bytes as space-separated lowercase hex pairs."""
    return " ".join(f"{b:02x}" for b in data)


def _ascii(data: bytes) -> str:
    """Render bytes as ASCII, replacing non-printable bytes with ``.``."""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


@dataclass(frozen=True, slots=True)
class WireFrame:
    """A single ASCII payload sent or received on the bus."""

    direction: str  # ">>" for write, "<<" for read
    target: str  # short label of the characteristic, e.g. "dead"
    data: bytes

    def __str__(self) -> str:
        return (
            f"  {self.direction} {self.target:4} ({len(self.data):>3}B) "
            f"ascii={_ascii(self.data)!r} hex={_hex(self.data)}"
        )


def clamp_level(value: int) -> int:
    """Clamp a brightness/channel level into the firmware's accepted range."""
    return max(BRIGHTNESS_MIN, min(BRIGHTNESS_MAX, value))


# --- response pretty-printing -----------------------------------------------
#
# Query responses on the READ characteristic are ASCII strings whose shape
# depends on the verb. We parse the well-known ones into something a human
# can scan; unknown / malformed payloads fall through to a cleaned-up ASCII
# form so we never lose information.

_CHANNEL_RE = re.compile(r"([RGBW])\s*(\d+)", re.IGNORECASE)

# Verbs the firmware acknowledges with just "OK" instead of returning data;
# treated as a no-op rather than misrendered as `version: OK`.
_ACK_ONLY_PAYLOADS = {"OK", "Ok", "ok"}


def _strip_payload(raw: bytes) -> str:
    """Decode bytes, drop nulls and surrounding whitespace."""
    return raw.decode("ascii", errors="replace").replace("\x00", "").strip()


def _strip_prefix(text: str, *prefixes: str) -> str:
    """Strip the first matching ASCII prefix from `text` (case-sensitive)."""
    for prefix in prefixes:
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def _try_int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def _power_label(text: str) -> str:
    """Map various truthy/falsy power encodings to ``on`` / ``off``."""
    lowered = text.lower()
    if lowered in {"1", "on", "true"}:
        return "on"
    if lowered in {"0", "off", "false"}:
        return "off"
    return text


def _parse_channels(text: str) -> dict[str, int]:
    """Best-effort parse of a colorvalues-style payload.

    The fixture sends ``CL:<r>,<g>,<b>,<w>`` (e.g. ``CL:0,68,68,0``); we strip
    the prefix and split on commas. Also tolerates the labelled forms a future
    firmware revision might use (``R100G50B0W25`` or ``R 100 G 50 ...``).
    """
    body = _strip_prefix(text, "CL:", "cl:")
    matches = _CHANNEL_RE.findall(body)
    if matches:
        return {ch.upper(): int(v) for ch, v in matches}
    parts = [p.strip() for p in body.split(",")]
    if len(parts) == 4 and all(_try_int(p) is not None for p in parts):
        return dict(zip("RGBW", (int(p) for p in parts), strict=True))
    return {}


def _parse_rtc(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, RTC_FORMAT)
    except ValueError:
        return None


def format_response(name: str, raw: bytes | None) -> str:
    """Render a known query / RTC response as a single human-readable line.

    Args:
        name: The verb (or friendly query alias) the response is for, e.g.
            ``"powerstatus"``, ``"color"``, ``"rtc"``. Used to pick the parser.
        raw: Bytes from the READ (or RTC) characteristic, or ``None``.

    Returns:
        Something like ``"power: on"`` or ``"color: R=100  G= 50  B=  0  W= 25"``.
        Falls back to ``"<verb>: <ascii>"`` for unknown verbs or unparseable
        payloads, so you always see *something* you can act on.
    """
    if raw is None:
        return f"{name}: (no response)"
    text = _strip_payload(raw)
    if not text:
        return f"{name}: (empty)"

    key = name.lower()

    # Several verbs the firmware just acks with "OK" rather than returning a
    # payload (version, username, schedulelist, scheduleday, timer2state,
    # boost2state). Surface that as an ACK rather than misrendering "OK" as
    # the actual value (e.g. "version: OK" reads like the version IS "OK").
    if text in _ACK_ONLY_PAYLOADS:
        return f"{key}: ack (no data returned)"

    if key in {"powerstatus", "power"}:
        return f"power: {_power_label(text)}"
    if key in {"brightlevel", "brightness"}:
        n = _try_int(text)
        return f"brightness: {n}%" if n is not None else f"brightness: {text}"
    if key in {"colorvalues", "color"}:
        channels = _parse_channels(text)
        if channels:
            return "color: " + "  ".join(f"{c}={channels.get(c, 0):>3}%" for c in "RGBW")
        return f"color: {text}"
    if key in {"schedulestate", "schedule"}:
        return f"schedule: {_power_label(_strip_prefix(text, 'SCH:', 'sch:'))}"
    if key in {"timerstate", "timer"}:
        # Firmware uses `TOn` / `TOff` (camelCase, "T" prefix).
        body = _strip_prefix(text, "T", "t")
        return f"timer: {_power_label(body)}"
    if key in {"booststate", "boost"}:
        # `Boost:OFF,0000,1` — strip the prefix; remaining shape is unclear,
        # so pass through unchanged.
        return f"boost: {_strip_prefix(text, 'Boost:', 'boost:')}"
    if key == "rtc":
        dt = _parse_rtc(text)
        if dt is None:
            return f"clock: {text}"
        drift = (datetime.now() - dt).total_seconds()
        return f"clock: {dt.isoformat(sep=' ', timespec='seconds')}  (host drift: {drift:+.0f}s)"
    return f"{key}: {text}"


# Match macOS BLE UUIDs (8-4-4-4-12 hex, what CoreBluetooth hands out instead
# of the real MAC) or classic MACs (AA:BB:CC:DD:EE:FF).
_ADDRESS_RE = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[0-9a-f]{2}(?::[0-9a-f]{2}){5})$",
    re.IGNORECASE,
)


def _looks_like_address(s: str) -> bool:
    """True if `s` is a plausible BLE address (macOS UUID or classic MAC)."""
    return bool(_ADDRESS_RE.match(s))


async def resolve_address(target: str, scan_seconds: float = 5.0) -> str:
    """Return a BLE address for `target`.

    If `target` already looks like an address, it's returned unchanged. Otherwise
    we scan briefly and find devices whose advertised name (either GAP `name`
    or local-name from the advertisement payload) contains `target` as a
    case-insensitive substring. Exits via SystemExit on zero or multiple matches.

    `target` of "twinstar" is enough on most setups; pass something more
    specific (e.g. "twinstar light pro") only if you have multiple in range.
    """
    if _looks_like_address(target):
        return target

    print(f"scanning for {target!r}...", file=sys.stderr)
    query = target.lower()
    discovered = await BleakScanner.discover(timeout=scan_seconds, return_adv=True)

    matches: list[tuple[str, list[str], int]] = []
    for address, (device, adv) in discovered.items():
        names = [n for n in (device.name, adv.local_name if adv else None) if n]
        if any(query in n.lower() for n in names):
            matches.append((address, names, adv.rssi if adv else -999))

    if not matches:
        raise SystemExit(
            f"no BLE device matching {target!r} found in {scan_seconds:g}s scan; "
            f"try `./scan.py` to see what's around"
        )
    if len(matches) > 1:
        print(f"multiple matches for {target!r}:", file=sys.stderr)
        for address, names, rssi in matches:
            label = " / ".join(dict.fromkeys(names))  # dedupe, preserve order
            print(f"  {rssi:>5}  {address}  {label}", file=sys.stderr)
        raise SystemExit("be more specific (longer name) or pass the full address")

    address, names, _ = matches[0]
    label = " / ".join(dict.fromkeys(names))
    print(f"found: {address}  {label}", file=sys.stderr)
    return address


class TwinstarClient:
    """Protocol-aware wrapper around a connected :class:`bleak.BleakClient`.

    The client does not own the BLE connection lifecycle. Construct it inside
    a ``BleakClient`` async context manager and pass that client in. Methods
    are coroutines because every BLE operation is.

    Example::

        async with BleakClient(address) as client:
            ts = TwinstarClient(client)
            await ts.power(on=True)
            await ts.set_channel("r", 100)
            await ts.set_time()
    """

    def __init__(self, client: BleakClient, *, verbose: bool = True) -> None:
        """Wrap a connected Bleak client.

        Args:
            client: A connected :class:`bleak.BleakClient`.
            verbose: When True, print each wire-level write and read.
        """
        self._client = client
        self._verbose = verbose

    # --- low-level wire ops -------------------------------------------------

    async def send_raw(self, verb: str) -> bytes | None:
        """Write an arbitrary ASCII verb to the WRITE characteristic.

        After the write, the READ characteristic is polled once for any
        response the firmware staged in reply.

        Args:
            verb: Plain ASCII command. No framing is added.

        Returns:
            The bytes returned from the READ characteristic, or ``None`` if
            the read failed (e.g. transport error).
        """
        payload = verb.encode("ascii")
        if self._verbose:
            print(WireFrame(">>", "dead", payload))
        # response=True forces an ATT acked write; the firmware processes
        # writes serially and the ack guarantees ordering with the read below.
        await self._client.write_gatt_char(WRITE_CHAR, payload, response=True)
        # Brief gap so the firmware has time to stage a reply on fef4 before
        # we read; without this, reads come back stale (the previous reply).
        await asyncio.sleep(0.15)
        try:
            value = bytes(await self._client.read_gatt_char(READ_CHAR))
        except Exception as exc:  # noqa: BLE001 - surface, don't swallow
            if self._verbose:
                print(f"     (read fef4 failed: {exc!r})")
            return None
        if self._verbose:
            print(WireFrame("<<", "fef4", value))
        return value

    async def read_rtc(self) -> bytes:
        """Read the device's current real-time clock as a raw 14-byte string."""
        value = bytes(await self._client.read_gatt_char(RTC_CHAR))
        if self._verbose:
            print(WireFrame("<<", "fef5", value))
        return value

    async def write_rtc(self, when: datetime | None = None) -> None:
        """Set the device's real-time clock.

        Args:
            when: Time to set. Defaults to host wallclock.
        """
        payload = format_timestamp(when)
        if self._verbose:
            print(WireFrame(">>", "fef5", payload) + "  (set RTC)")
        # RTC lives on its own characteristic; do NOT route this through
        # send_raw / WRITE_CHAR or the firmware will reject it.
        await self._client.write_gatt_char(RTC_CHAR, payload, response=True)

    # --- high-level commands ------------------------------------------------

    async def power(self, *, on: bool) -> bytes | None:
        """Turn the fixture on or off."""
        return await self.send_raw("on" if on else "off")

    async def set_channel(self, channel: str, level: int) -> bytes | None:
        """Set a channel intensity.

        Args:
            channel: One of ``a`` (master), ``r``, ``g``, ``b``, ``w``
                (case insensitive).
            level: 0-100. Values outside the range are clamped.

        Raises:
            ValueError: If ``channel`` isn't a known channel letter.
        """
        key = channel.lower()
        if key not in CHANNEL_VERBS:
            raise ValueError(
                f"unknown channel {channel!r}; expected one of {sorted(CHANNEL_VERBS)}"
            )
        # Verb is one letter + decimal digits, no separator (e.g. "R100", "A50").
        return await self.send_raw(f"{CHANNEL_VERBS[key]}{clamp_level(level)}")

    async def query(self, name: str) -> bytes | None:
        """Send a known query verb and return its raw response.

        Args:
            name: Friendly query key (see :data:`QUERY_VERBS` for the list)
                or a literal wire verb. Friendly keys are translated.
        """
        verb = QUERY_VERBS.get(name.lower(), name)
        return await self.send_raw(verb)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def _cmd_repl(ts: TwinstarClient) -> None:
    """Run an interactive command loop until EOF or ``quit``."""
    print(
        "Twinstar REPL. Commands:\n"
        "  on / off                  power\n"
        "  a|r|g|b|w <0-100>         set channel level\n"
        "  q <key>                   query (power, color, brightness, version, ...)\n"
        "  raw <text>                send arbitrary ASCII to the write char\n"
        "  rtc                       read clock\n"
        "  set-time                  write current wallclock to clock\n"
        "  quit                      exit"
    )
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = (await loop.run_in_executor(None, input, ">>> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line or line in {"quit", "exit", "q!"}:
            return

        parts = line.split()
        head = parts[0].lower()
        try:
            match (head, parts):
                case ("on", _):
                    await ts.power(on=True)
                case ("off", _):
                    await ts.power(on=False)
                case (h, [_, level]) if h in CHANNEL_VERBS:
                    await ts.set_channel(h, int(level))
                case ("q", [_, target]):
                    print(format_response(target, await ts.query(target)))
                case ("rtc", _):
                    print(format_response("rtc", await ts.read_rtc()))
                case ("set-time", _):
                    await ts.write_rtc()
                    print(format_response("rtc", await ts.read_rtc()))
                case ("raw", [_, *rest]) if rest:
                    raw = await ts.send_raw(" ".join(rest))
                    if raw is not None:
                        print(format_response(" ".join(rest), raw))
                case _:
                    await ts.send_raw(line)
        except Exception as exc:  # noqa: BLE001
            print(f"  error: {exc!r}")


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse for the CLI."""
    parser = argparse.ArgumentParser(
        prog="twinstar",
        description="Control a Twinstar Light Pro / Dimmer fixture over BLE.",
    )
    parser.add_argument(
        "device",
        metavar="ADDR_OR_NAME",
        help=(
            "BLE address of the fixture, OR a name substring like 'twinstar' "
            "(we'll scan for ~5s and find it)"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show wire-level GATT writes and reads (hex + ascii)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="command")

    sub.add_parser("on", help="power on")
    sub.add_parser("off", help="power off")

    for letter in CHANNEL_VERBS:
        p = sub.add_parser(letter, help=f"set {letter.upper()} channel level (0-100)")
        p.add_argument("level", type=int)

    sub.add_parser("set-time", help="sync RTC to host wallclock")
    sub.add_parser("rtc", help="read RTC")

    p_query = sub.add_parser("query", help="run a state query")
    p_query.add_argument(
        "target",
        choices=sorted(QUERY_VERBS),
        help="what to query",
    )

    p_raw = sub.add_parser("raw", help="send a raw ASCII payload to the write char")
    p_raw.add_argument("payload")

    sub.add_parser("repl", help="interactive command session")

    return parser


async def _dispatch(args: argparse.Namespace) -> None:
    """Connect and run the chosen subcommand.

    Default output is one clean line per response (or silent for fire-and-forget
    writes). ``-v`` adds the wire-level dumps that are useful when poking at
    the protocol.
    """
    address = await resolve_address(args.device)
    async with BleakClient(address, timeout=20.0) as client:
        if args.verbose:
            print(f"connected={client.is_connected} mtu={client.mtu_size}")
        ts = TwinstarClient(client, verbose=args.verbose)

        match args.cmd:
            case "on" | "off" as cmd:
                await ts.power(on=cmd == "on")
            case letter if letter in CHANNEL_VERBS:
                await ts.set_channel(letter, args.level)
            case "set-time":
                await ts.write_rtc()
                print(format_response("rtc", await ts.read_rtc()))
            case "rtc":
                print(format_response("rtc", await ts.read_rtc()))
            case "query":
                raw = await ts.query(args.target)
                print(format_response(args.target, raw))
            case "raw":
                raw = await ts.send_raw(args.payload)
                if raw is not None:
                    # Unknown verb; format_response will just print the ASCII.
                    print(format_response(args.payload, raw))
            case "repl":
                await _cmd_repl(ts)
            case other:
                raise SystemExit(f"unhandled command: {other!r}")


def main(argv: Iterable[str] | None = None) -> None:
    """CLI entrypoint."""
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    asyncio.run(_dispatch(args))


if __name__ == "__main__":
    main()
