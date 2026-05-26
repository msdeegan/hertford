# Neewer BLE → HTTP bridge

Tiny FastAPI service that exposes nearby Neewer Bluetooth lights over HTTP.
Runs on a host with a Bluetooth radio (your Mac, a Raspberry Pi, etc.).

The Hertford container then calls this service over the LAN to drive the
Office key lights.

## Install (Mac)

```bash
cd bridge/neewer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

First time on macOS, you'll be asked to grant Bluetooth access to whatever
Python interpreter is running this — accept the permission prompt that
appears.

## Run

```bash
python neewer_bridge.py
```

It listens on `0.0.0.0:8765` by default (override with `PORT=...` env).
Leave the terminal window open for now — we'll wire it up to launch on
login once it's working.

## Discover the lights

With both Neewer RGB190s powered on and within ~10m of the Mac:

```bash
curl -X POST http://localhost:8765/discover
```

You should get something like:

```json
{
  "devices": [
    {"address": "AA:BB:CC:DD:EE:01", "name": "NEEWER-RGB190"},
    {"address": "AA:BB:CC:DD:EE:02", "name": "NEEWER-RGB190"}
  ]
}
```

## Register them with stable ids

Pick short ids (used by the Hertford UI). E.g. `key1` and `key2`:

```bash
curl -X POST http://localhost:8765/lights/register \
  -d 'id=key1' -d 'address=AA:BB:CC:DD:EE:01' -d 'name=Key 1'

curl -X POST http://localhost:8765/lights/register \
  -d 'id=key2' -d 'address=AA:BB:CC:DD:EE:02' -d 'name=Key 2'
```

Registered lights persist at `~/.hertford-neewer.json`.

## Test control

```bash
# Power on / off
curl -X POST http://localhost:8765/lights/key1/power -d 'on=true'
curl -X POST http://localhost:8765/lights/key1/power -d 'on=false'

# White light (CCT mode): brightness 0-100, cct 32-56 ≈ 3200K-5600K
curl -X POST http://localhost:8765/lights/key1/cct -d 'brightness=80' -d 'cct=44'

# Full colour: hue 0-360, sat 0-100, brightness 0-100 — soft pink at ~30%
curl -X POST http://localhost:8765/lights/key1/color \
  -d 'hue=340' -d 'saturation=40' -d 'brightness=30'
```

If any of these don't behave as expected, the Neewer protocol may need
tweaking for your specific model — see `cmd_*` functions in
`neewer_bridge.py` and cross-reference with
[NeewerLite-Python](https://github.com/taburineagle/NeewerLite-Python).

## Auto-start on login (later)

Once the manual `python neewer_bridge.py` workflow proves reliable, we'll
add a `launchd` plist (under `~/Library/LaunchAgents/`) that starts it on
login so you don't have to remember.
