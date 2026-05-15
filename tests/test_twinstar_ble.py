"""Unit tests for twinstar_ble: protocol encoding, helpers, TwinstarClient."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

import twinstar_ble as tb

# --- pure helpers ---------------------------------------------------------


class TestFormatTimestamp:
    def test_specific_datetime(self):
        assert tb.format_timestamp(datetime(2024, 7, 15, 13, 4, 5)) == b"20240715130405"

    def test_returns_bytes(self):
        assert isinstance(tb.format_timestamp(datetime(2026, 1, 1)), bytes)

    def test_default_uses_now(self):
        out = tb.format_timestamp()
        assert len(out) == 14
        assert out.isdigit()

    def test_year_2099_still_14_bytes(self):
        # Sanity check: format width matches the firmware's expectation.
        assert len(tb.format_timestamp(datetime(2099, 12, 31, 23, 59, 59))) == 14


class TestHexAndAscii:
    def test_hex_simple(self):
        assert tb._hex(b"\x00\xff\x10") == "00 ff 10"

    def test_hex_empty(self):
        assert tb._hex(b"") == ""

    def test_hex_lowercase(self):
        # Expectation: lowercase hex pairs (matches what firmware logs use).
        assert tb._hex(b"\xab\xcd") == "ab cd"

    def test_ascii_printable(self):
        assert tb._ascii(b"hello") == "hello"

    def test_ascii_replaces_nonprintable_with_dot(self):
        assert tb._ascii(b"\x00ABC\xff") == ".ABC."

    def test_ascii_handles_high_bytes(self):
        assert tb._ascii(b"\x80\x81") == ".."


class TestClampLevel:
    @pytest.mark.parametrize(
        "inp,expected",
        [
            (-100, 0),
            (-1, 0),
            (0, 0),
            (1, 1),
            (50, 50),
            (100, 100),
            (101, 100),
            (9999, 100),
        ],
    )
    def test_clamp(self, inp, expected):
        assert tb.clamp_level(inp) == expected


class TestWireFrame:
    def test_str_includes_direction_target_and_payload(self):
        s = str(tb.WireFrame(">>", "dead", b"on"))
        assert ">>" in s
        assert "dead" in s
        assert "ascii=" in s
        assert "hex=" in s

    def test_str_renders_bytes_in_hex(self):
        s = str(tb.WireFrame("<<", "fef4", b"AB"))
        assert "41 42" in s  # 'A' 'B' in hex

    def test_is_immutable(self):
        wf = tb.WireFrame(">>", "dead", b"on")
        with pytest.raises((AttributeError, Exception)):
            wf.direction = "<<"  # frozen=True


# --- protocol tables ------------------------------------------------------


class TestVerbsTables:
    def test_channel_verbs_keys_are_lowercase(self):
        assert all(k.islower() for k in tb.CHANNEL_VERBS)

    def test_channel_verbs_complete(self):
        assert set(tb.CHANNEL_VERBS) == {"a", "r", "g", "b", "w"}

    def test_channel_verbs_uppercase_letters(self):
        for letter, verb in tb.CHANNEL_VERBS.items():
            assert verb == letter.upper()

    def test_query_verbs_known_aliases(self):
        assert tb.QUERY_VERBS["power"] == "powerstatus"
        assert tb.QUERY_VERBS["color"] == "colorvalues"
        assert tb.QUERY_VERBS["brightness"] == "brightlevel"

    def test_query_verbs_keys_are_friendly(self):
        # Friendly keys are lowercase; values can be anything the firmware accepts.
        for key in tb.QUERY_VERBS:
            assert key.islower()


# --- TwinstarClient (with mock BleakClient) -------------------------------


class TestSendRaw:
    async def test_writes_to_dead_and_reads_fef4(self, mock_bleak_client):
        mock_bleak_client.read_gatt_char.return_value = b"reply"
        ts = tb.TwinstarClient(mock_bleak_client, verbose=False)

        result = await ts.send_raw("on")

        assert result == b"reply"
        mock_bleak_client.write_gatt_char.assert_awaited_once_with(
            tb.WRITE_CHAR, b"on", response=True
        )
        mock_bleak_client.read_gatt_char.assert_awaited_once_with(tb.READ_CHAR)

    async def test_returns_none_if_read_fails(self, mock_bleak_client):
        mock_bleak_client.read_gatt_char.side_effect = RuntimeError("transport gone")
        ts = tb.TwinstarClient(mock_bleak_client, verbose=False)

        assert await ts.send_raw("on") is None

    async def test_payload_is_bytes_of_verb(self, mock_bleak_client):
        ts = tb.TwinstarClient(mock_bleak_client, verbose=False)
        await ts.send_raw("colorvalues")
        args, kwargs = mock_bleak_client.write_gatt_char.await_args
        assert args == (tb.WRITE_CHAR, b"colorvalues")
        assert kwargs == {"response": True}


class TestPower:
    async def test_on(self, mock_bleak_client):
        ts = tb.TwinstarClient(mock_bleak_client, verbose=False)
        await ts.power(on=True)
        mock_bleak_client.write_gatt_char.assert_awaited_with(tb.WRITE_CHAR, b"on", response=True)

    async def test_off(self, mock_bleak_client):
        ts = tb.TwinstarClient(mock_bleak_client, verbose=False)
        await ts.power(on=False)
        mock_bleak_client.write_gatt_char.assert_awaited_with(tb.WRITE_CHAR, b"off", response=True)


class TestSetChannel:
    @pytest.mark.parametrize(
        "ch,expected",
        [
            ("a", b"A75"),
            ("r", b"R75"),
            ("g", b"G75"),
            ("b", b"B75"),
            ("w", b"W75"),
            ("A", b"A75"),  # case insensitive
            ("R", b"R75"),
        ],
    )
    async def test_emits_correct_verb(self, mock_bleak_client, ch, expected):
        ts = tb.TwinstarClient(mock_bleak_client, verbose=False)
        await ts.set_channel(ch, 75)
        mock_bleak_client.write_gatt_char.assert_awaited_with(
            tb.WRITE_CHAR, expected, response=True
        )

    async def test_clamps_above(self, mock_bleak_client):
        ts = tb.TwinstarClient(mock_bleak_client, verbose=False)
        await ts.set_channel("r", 999)
        mock_bleak_client.write_gatt_char.assert_awaited_with(tb.WRITE_CHAR, b"R100", response=True)

    async def test_clamps_below(self, mock_bleak_client):
        ts = tb.TwinstarClient(mock_bleak_client, verbose=False)
        await ts.set_channel("r", -50)
        mock_bleak_client.write_gatt_char.assert_awaited_with(tb.WRITE_CHAR, b"R0", response=True)

    async def test_zero_is_valid(self, mock_bleak_client):
        ts = tb.TwinstarClient(mock_bleak_client, verbose=False)
        await ts.set_channel("g", 0)
        mock_bleak_client.write_gatt_char.assert_awaited_with(tb.WRITE_CHAR, b"G0", response=True)

    async def test_unknown_channel_raises(self, mock_bleak_client):
        ts = tb.TwinstarClient(mock_bleak_client, verbose=False)
        with pytest.raises(ValueError, match="unknown channel"):
            await ts.set_channel("x", 50)


class TestQuery:
    async def test_translates_friendly_alias(self, mock_bleak_client):
        ts = tb.TwinstarClient(mock_bleak_client, verbose=False)
        await ts.query("color")
        mock_bleak_client.write_gatt_char.assert_awaited_with(
            tb.WRITE_CHAR, b"colorvalues", response=True
        )

    async def test_passes_unknown_through_verbatim(self, mock_bleak_client):
        # Unknown keys pass through so callers can hit niche verbs without
        # a library update. The firmware ignores garbage cleanly.
        ts = tb.TwinstarClient(mock_bleak_client, verbose=False)
        await ts.query("madeupverb")
        mock_bleak_client.write_gatt_char.assert_awaited_with(
            tb.WRITE_CHAR, b"madeupverb", response=True
        )

    async def test_case_insensitive(self, mock_bleak_client):
        ts = tb.TwinstarClient(mock_bleak_client, verbose=False)
        await ts.query("POWER")
        mock_bleak_client.write_gatt_char.assert_awaited_with(
            tb.WRITE_CHAR, b"powerstatus", response=True
        )


class TestRtc:
    async def test_write_rtc_targets_rtc_char_not_write_char(self, mock_bleak_client):
        # The single most important RTC invariant: don't route through WRITE_CHAR.
        ts = tb.TwinstarClient(mock_bleak_client, verbose=False)
        await ts.write_rtc(datetime(2024, 1, 2, 3, 4, 5))
        mock_bleak_client.write_gatt_char.assert_awaited_with(
            tb.RTC_CHAR, b"20240102030405", response=True
        )

    async def test_read_rtc_targets_rtc_char(self, mock_bleak_client):
        mock_bleak_client.read_gatt_char.return_value = b"20240102030405"
        ts = tb.TwinstarClient(mock_bleak_client, verbose=False)
        result = await ts.read_rtc()
        assert result == b"20240102030405"
        mock_bleak_client.read_gatt_char.assert_awaited_with(tb.RTC_CHAR)


# --- CLI argparse ---------------------------------------------------------


class TestCli:
    def test_requires_address_and_command(self):
        parser = tb._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])
        with pytest.raises(SystemExit):
            parser.parse_args(["addr"])

    def test_simple_commands(self):
        parser = tb._build_parser()
        for cmd in ("on", "off", "set-time", "rtc", "repl"):
            args = parser.parse_args(["ADDR", cmd])
            assert args.device == "ADDR"
            assert args.cmd == cmd

    def test_channel_command_with_level(self):
        parser = tb._build_parser()
        args = parser.parse_args(["ADDR", "r", "50"])
        assert args.cmd == "r"
        assert args.level == 50

    def test_channel_command_requires_level(self):
        parser = tb._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["ADDR", "r"])

    def test_query_validates_target(self):
        parser = tb._build_parser()
        args = parser.parse_args(["ADDR", "query", "color"])
        assert args.target == "color"
        with pytest.raises(SystemExit):
            parser.parse_args(["ADDR", "query", "nonsense"])

    def test_raw_takes_payload(self):
        parser = tb._build_parser()
        args = parser.parse_args(["ADDR", "raw", "boost"])
        assert args.payload == "boost"

    def test_verbose_flag_defaults_off(self):
        parser = tb._build_parser()
        args = parser.parse_args(["ADDR", "on"])
        assert args.verbose is False

    def test_verbose_flag_short_and_long(self):
        parser = tb._build_parser()
        for flag in ("-v", "--verbose"):
            args = parser.parse_args(["ADDR", flag, "on"])
            assert args.verbose is True


# --- response pretty-printing --------------------------------------------


class TestFormatResponse:
    def test_none_response(self):
        assert tb.format_response("power", None) == "power: (no response)"

    def test_empty_payload(self):
        assert tb.format_response("power", b"") == "power: (empty)"
        assert tb.format_response("power", b"   \x00 ") == "power: (empty)"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (b"1", "power: on"),
            (b"on", "power: on"),
            (b"0", "power: off"),
            (b"off", "power: off"),
        ],
    )
    def test_power(self, raw, expected):
        assert tb.format_response("powerstatus", raw) == expected
        assert tb.format_response("power", raw) == expected

    def test_brightness_int(self):
        assert tb.format_response("brightlevel", b"50") == "brightness: 50%"
        assert tb.format_response("brightness", b"100") == "brightness: 100%"

    def test_brightness_unparseable_falls_through(self):
        assert tb.format_response("brightness", b"weird") == "brightness: weird"

    def test_color_real_firmware_shape(self):
        # Confirmed real response from a 450S V: `CL:R,G,B,W`.
        out = tb.format_response("color", b"CL:0,68,68,0")
        assert out == "color: R=  0%  G= 68%  B= 68%  W=  0%"

    def test_color_labelled_concatenated(self):
        # Forward-compat shape; firmware doesn't currently use this but the
        # parser handles it.
        out = tb.format_response("color", b"R100G50B0W25")
        assert "R=100%" in out
        assert "G= 50%" in out
        assert "B=  0%" in out
        assert "W= 25%" in out

    def test_color_comma_separated_no_prefix(self):
        out = tb.format_response("color", b"100,50,0,25")
        assert "R=100%" in out and "W= 25%" in out

    def test_color_unparseable_shows_raw_text(self):
        assert tb.format_response("color", b"???") == "color: ???"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (b"SCH:ON", "schedule: on"),
            (b"SCH:OFF", "schedule: off"),
            (b"sch:on", "schedule: on"),
        ],
    )
    def test_schedule(self, raw, expected):
        assert tb.format_response("schedulestate", raw) == expected
        assert tb.format_response("schedule", raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (b"TOn", "timer: on"),
            (b"TOff", "timer: off"),
            (b"ton", "timer: on"),
        ],
    )
    def test_timer(self, raw, expected):
        assert tb.format_response("timerstate", raw) == expected

    def test_boost_strips_prefix(self):
        out = tb.format_response("booststate", b"Boost:OFF,0000,1")
        assert out == "boost: OFF,0000,1"

    @pytest.mark.parametrize("verb", ["version", "username", "schedulelist", "timer2state"])
    def test_ack_only_verbs(self, verb):
        # Real firmware behavior: these acknowledge with "OK" rather than
        # returning a payload. Don't render that as if "OK" were the value.
        out = tb.format_response(verb, b"OK")
        assert out == f"{verb}: ack (no data returned)"

    def test_rtc_parsed(self):
        out = tb.format_response("rtc", b"20260514130405")
        assert out.startswith("clock: 2026-05-14 13:04:05")
        assert "host drift" in out

    def test_rtc_unix_epoch_shape(self):
        # Real bytes captured after a power cycle: firmware counts from unix
        # epoch (1970), not 2021.
        out = tb.format_response("rtc", b"19700101011937")
        assert out.startswith("clock: 1970-01-01 01:19:37")

    def test_rtc_garbage_falls_through(self):
        assert tb.format_response("rtc", b"not a date") == "clock: not a date"

    def test_unknown_verb_shows_lowercase_and_text(self):
        assert tb.format_response("WhatEver", b"hello") == "whatever: hello"


# --- address resolution --------------------------------------------------


class TestLooksLikeAddress:
    @pytest.mark.parametrize(
        "value",
        [
            "B0223E4B-CDCF-ECEC-2817-AED323FA1090",  # macOS UUID
            "b0223e4b-cdcf-ecec-2817-aed323fa1090",  # lowercase
            "AA:BB:CC:DD:EE:FF",  # classic MAC
            "aa:bb:cc:dd:ee:ff",
        ],
    )
    def test_recognises_addresses(self, value):
        assert tb._looks_like_address(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "twinstar",
            "twinstar light pro",
            "Twinstar_Dimmer",
            "ABC",
            "",
            "B0223E4B-CDCF-ECEC-2817-AED323FA109",  # short by one
            "GG:HH:II:JJ:KK:LL",  # not hex
            "B0223E4B-CDCF-ECEC-2817-AED323FA1090-extra",
        ],
    )
    def test_rejects_non_addresses(self, value):
        assert tb._looks_like_address(value) is False


def _fake_discover(devices: dict[str, tuple[object, object]]):
    """Build a stand-in for `BleakScanner.discover(return_adv=True)`."""

    async def _discover(*, timeout: float, return_adv: bool):
        return devices

    return _discover


def _fake_device(
    address: str, name: str | None = None, local_name: str | None = None, rssi: int = -60
):
    device = SimpleNamespace(address=address, name=name)
    adv = SimpleNamespace(local_name=local_name, rssi=rssi, service_uuids=[])
    return address, (device, adv)


class TestResolveAddress:
    async def test_passthrough_for_uuid(self, monkeypatch):
        # An address-shaped target should NOT trigger a scan at all.
        called = False

        async def boom(*a, **kw):
            nonlocal called
            called = True
            return {}

        monkeypatch.setattr(tb.BleakScanner, "discover", boom)
        result = await tb.resolve_address("B0223E4B-CDCF-ECEC-2817-AED323FA1090")
        assert result == "B0223E4B-CDCF-ECEC-2817-AED323FA1090"
        assert called is False

    async def test_passthrough_for_mac(self, monkeypatch):
        monkeypatch.setattr(tb.BleakScanner, "discover", _fake_discover({}))
        result = await tb.resolve_address("AA:BB:CC:DD:EE:FF")
        assert result == "AA:BB:CC:DD:EE:FF"

    async def test_finds_single_match_by_name(self, monkeypatch, capsys):
        addr_kv = _fake_device("DEAD-BEEF", name="Twinstar Light Pro", rssi=-55)
        other_kv = _fake_device("CAFE-BABE", name="Some Other Light", rssi=-70)
        monkeypatch.setattr(tb.BleakScanner, "discover", _fake_discover(dict([addr_kv, other_kv])))
        result = await tb.resolve_address("twinstar", scan_seconds=0.0)
        assert result == "DEAD-BEEF"
        # Diagnostic chatter goes to stderr so it doesn't pollute pipelines.
        err = capsys.readouterr().err
        assert "found" in err and "DEAD-BEEF" in err

    async def test_matches_local_name_too(self, monkeypatch):
        # GAP `name` may be empty; `local_name` from advertisement data may carry it.
        kv = _fake_device("DEAD-BEEF", name=None, local_name="Twinstar_Dimmer")
        monkeypatch.setattr(tb.BleakScanner, "discover", _fake_discover(dict([kv])))
        result = await tb.resolve_address("twinstar", scan_seconds=0.0)
        assert result == "DEAD-BEEF"

    async def test_case_insensitive(self, monkeypatch):
        kv = _fake_device("DEAD-BEEF", name="TWINSTAR LIGHT PRO")
        monkeypatch.setattr(tb.BleakScanner, "discover", _fake_discover(dict([kv])))
        assert await tb.resolve_address("twinstar", scan_seconds=0.0) == "DEAD-BEEF"

    async def test_no_match_exits(self, monkeypatch):
        monkeypatch.setattr(tb.BleakScanner, "discover", _fake_discover({}))
        with pytest.raises(SystemExit, match="no BLE device matching"):
            await tb.resolve_address("twinstar", scan_seconds=0.0)

    async def test_multiple_matches_lists_then_exits(self, monkeypatch, capsys):
        kvs = [
            _fake_device("DEAD-BEEF", name="Twinstar Light Pro", rssi=-55),
            _fake_device("CAFE-BABE", name="Twinstar_Dimmer", rssi=-72),
        ]
        monkeypatch.setattr(tb.BleakScanner, "discover", _fake_discover(dict(kvs)))
        with pytest.raises(SystemExit, match="be more specific"):
            await tb.resolve_address("twinstar", scan_seconds=0.0)
        err = capsys.readouterr().err
        # Both matches should be listed so the user can pick a more specific name.
        assert "DEAD-BEEF" in err
        assert "CAFE-BABE" in err
