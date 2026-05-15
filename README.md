# twinstar-ble-controller

A reverse-engineered Bluetooth controller for the [Twinstar Light S-Series Pro / Dimmer](https://twinstareu.com/twinstar-light/twinstar-s-line-v/) aquarium LED, because the official app is bad and the fixture deserves better.

A small Python package and four console scripts that speak the fixture's BLE protocol directly: power, master dim, and per-channel R/G/B/W.

![twinstar-vibe demo](docs/vibe.gif)

A demo script exercising the full feature set, with a terminal preview that tracks the real fixture in lockstep.

Built and tested against a Twinstar Light S-Series Version V, 450S V (GAP name `Twinstar_Dimmer` / `Twinstar Light Pro`). Other S-Series V sizes almost certainly speak the same protocol; older S-Series and the C-Series were not tested.

## Why

The fixture is lovely; the TWINSTAR LightControl app is sluggish, two-tap-to-do-anything, and ships an inflexible scheduler. The protocol turns out to be plain ASCII over BLE GATT with no auth, so this PoC took an evening. End goal is an ESP32 + rotary-encoder + presence-sensor controller next to the tank that speaks the same verbs from C++; this Python package is the protocol scaffolding for that.

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/LyleScott/twinstar-ble-controller.git
cd twinstar-ble-controller
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                # add `[dev]` for ruff + pytest
```

Installs four console scripts: `twinstar-ble`, `twinstar-vibe`, `twinstar-scan`, `twinstar-enumerate`. Each is also runnable as `python -m twinstar_ble[.vibe|.scan|.enumerate]`.

First BLE call on macOS will trigger a Bluetooth permission prompt for the host app (Terminal, iTerm, Cursor, etc.); approve it in System Settings.

## Usage

> **One BLE client at a time.** Force-quit the official app first (on iOS, swipe it away; backgrounding isn't enough), or this CLI will hang trying to connect.

The first positional arg accepts either a literal BLE address or a name substring (e.g. `twinstar`); names trigger a ~5s scan.

```bash
twinstar-ble twinstar on
twinstar-ble twinstar a 50               # master 50%
twinstar-ble twinstar r 100              # red full
twinstar-ble twinstar g 75               # green 75%
twinstar-ble twinstar b 25               # blue 25%
twinstar-ble twinstar w 50               # grow / white 50%
twinstar-ble twinstar query color        # → color: R=  0%  G= 68%  B= 68%  W=  0%
twinstar-ble twinstar query power        # → power: on
twinstar-ble twinstar query brightness   # → brightness: 100%
twinstar-ble twinstar query schedule     # → schedule: off
twinstar-ble twinstar query timer        # → timer: off
twinstar-ble twinstar query boost        # → boost: OFF,0000,1
twinstar-ble twinstar rtc                # → clock: 1970-01-01 01:19:37  (host drift: +…s)
twinstar-ble twinstar set-time           # sync to host clock
twinstar-ble twinstar repl               # interactive

twinstar-ble twinstar raw BoostOn        # known mutator not (yet) a first-class subcommand
twinstar-ble twinstar raw BoostOff       # see DEVELOPERS.md for the verb catalogue

A=B0223E4B-CDCF-ECEC-2817-AED323FA1090   # skip scan with literal address
twinstar-ble $A on
twinstar-ble -v twinstar query color     # add -v / --verbose for raw GATT wire dumps

twinstar-scan twinstar                   # filter by name
twinstar-enumerate $A                    # full GATT dump
```

## Vibe mode

The day-cycle animation at the top of this README. Walks `night → sunrise → noon → sunset → night` and drives the fixture in step.

```bash
twinstar-vibe twinstar              # ~30s/cycle, loops until Ctrl-C
twinstar-vibe twinstar --cycles 4   # finite
twinstar-vibe twinstar --speed 0.25 # slower
twinstar-vibe --no-send             # preview only, no fixture
```

Defaults are tuned to be gentle on the fixture: 0.6s pacing between writes and a `≥3` channel-change threshold, so steady-state write rate is comparable to slowly dragging a slider in the official app. Raise `--pacing` and prefer `--cycles N` over looping forever if you want to be extra cautious.

## Examples

Run directly or copy-paste apart:

```bash
python examples/walkdown.py $A   # master 100 → 75 → 50 → 25 → off
python examples/demo.py $A       # full vibe-check sequence (~30s)
```

[`examples/demo.py`](examples/demo.py) uses a raw-write helper for the fade so it's smooth (skipping the per-write read-back that `set_channel` does for safety on discrete commands).

## Status

| Capability                                              | Status                                              |
| ------------------------------------------------------- | --------------------------------------------------- |
| Connect, GATT read/write, RTC                           | Working                                             |
| Power on/off, master and R/G/B/W channels               | Working                                             |
| Pretty-printed queries (power, brightness, color, RTC)  | Working                                             |
| Pretty-printed schedule, timer, boost state             | Working (read only)                                 |
| Boost overlay on/off via `raw BoostOn` / `raw BoostOff` | Working; not yet a first-class subcommand           |
| Schedule **write** verbs                                | Not decoded; drive scheduling from the host instead |
| OTA firmware update                                     | Out of scope; use the official mobile app           |
| ESP32 firmware port                                     | dreaming of the time ;)                             |

## Hacking on it

Tests, protocol reference, and other developer notes are in [`DEVELOPERS.md`](DEVELOPERS.md).

## License and disclaimers

[MIT](LICENSE). The license text is the legally operative bit; the rest is context.

Hobby project for my own tank, shared in case it helps. Not a product, not supported, not affiliated with Twinstar; "Twinstar," "S-Series," "TWINSTAR LightControl," and related marks belong to their respective owners. Built independently from over-the-air observation of a fixture I purchased, for interoperating with hardware I own.

Talking to consumer BLE hardware in unsupported ways can in principle brick the device, void your warranty, or anger your fish. I haven't observed any of that on my unit; I make no promises about yours. In particular: do not write to the OTA characteristic, leave the on-device scheduler alone, and don't fight the firmware over the link in production setups. The MIT "as is" / no-liability clauses apply in full.

If you're a Twinstar employee and you'd prefer this not exist (or document the protocol differently), open a GitHub issue and we can talk.
