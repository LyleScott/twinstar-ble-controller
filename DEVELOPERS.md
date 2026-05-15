# Developer notes

In-the-weeds details for hacking on this package or porting the protocol elsewhere (e.g. the planned ESP32 firmware). User-facing install / usage lives in [`README.md`](README.md).

## Tests

```bash
pip install -e ".[dev]" && pytest -q
```

`BleakClient` is mocked, so the suite covers protocol encoding, channel routing, RTC formatting, palette interpolation, color mixing, and CLI parsing without touching hardware. CI runs ruff + pytest on Python 3.10 and 3.13 for every PR ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

## Protocol reference

One custom GATT service, `00000180-0000-1000-8000-00805f9b34fb`:

| Char  | UUID suffix  | Purpose                                            |
| ----- | ------------ | -------------------------------------------------- |
| Write | `0000dead-…` | ASCII commands                                     |
| Read  | `0000fef4-…` | Response, polled after each write                  |
| RTC   | `0000fef5-…` | 14-byte ASCII timestamp `YYYYMMDDHHMMSS` (set/get) |
| OTA   | `0000feed-…` | Firmware updates; **do not write**                 |

ASCII command verbs (no framing, no checksum):

| Verb                                                                        | Effect                                                        |
| --------------------------------------------------------------------------- | ------------------------------------------------------------- |
| `on`, `off`                                                                 | Power                                                         |
| `A<n>`                                                                      | Master brightness 0-100 (firmware silently bumps `A1` → `A2`) |
| `R<n>`, `G<n>`, `B<n>`, `W<n>`                                              | Per-channel intensity 0-100                                   |
| `version`, `powerstatus`, `brightlevel`, `colorvalues`                      | Queries                                                       |
| `schedulelist`, `scheduleday`, `schedulestate`, `timerstate`, `username`, … | More queries                                                  |

The fixture has no battery-backed clock, so it counts up from `19700101000000` on every power cycle (unix epoch) and must be re-synced on connect. Schedule **write** verbs are not yet decoded; drive scheduling from the host instead.

Confirmed query response shapes from a 450S V on firmware as of 2026-05:

| Verb                                                                               | Response                           | Notes                                                                                                                                                                     |
| ---------------------------------------------------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `powerstatus`                                                                      | `ON` / `OFF`                       | uppercase                                                                                                                                                                 |
| `brightlevel`                                                                      | `100`                              | plain int 0-100                                                                                                                                                           |
| `colorvalues`                                                                      | `CL:R,G,B,W` (e.g. `CL:0,68,68,0`) | comma-separated, after `CL:` prefix                                                                                                                                       |
| `schedulestate`                                                                    | `SCH:ON` / `SCH:OFF`               |                                                                                                                                                                           |
| `timerstate`                                                                       | `TOn` / `TOff`                     | camelCase, leading `T`                                                                                                                                                    |
| `booststate`                                                                       | `Boost:OFF,0000,1`                 | structured but semantics unclear                                                                                                                                          |
| `version`, `username`, `schedulelist`, `scheduleday`, `timer2state`, `boost2state` | `OK`                               | firmware just acks; no payload returned on `0000fef4-…`. Likely either action verbs misclassified as queries, or responses staged on a notify path we don't subscribe to. |

### Write verbs (mutating, beyond the documented CLI subcommands)

The CLI exposes `on` / `off` / `a|r|g|b|w <0-100>` / `set-time` directly. Other mutators discovered by experiment, sendable today via `twinstar-ble <addr> raw <verb>`:

| Verb                   | Effect                                                                                    | Verified                                                       |
| ---------------------- | ----------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `BoostOn` / `BoostOff` | Toggle the boost overlay (sticky; doesn't auto-revert in the few-seconds window I tested) | Yes; `booststate` flips `Boost:OFF,0000,1` ↔ `Boost:ON,0000,1` |

Verb naming is **case-sensitive in unexpected ways**: `BoostOn` works, `BoostON` and `boost:on` are silently no-op'd (acked but ignored). The convention so far is `<TitleCase><On|Off>`, the same shape the read responses use (`TOn`, `TOff`).

The two trailing fields in `Boost:ON,0000,1` (`0000` and `1`) didn't change during the on/off trial; they're plausibly duration and channel/slot. Toggling boost from the official app with a non-zero duration would show what those fields become.
