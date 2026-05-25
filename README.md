# Hertford

Home media control system for the house. Lets guests pick TV sources in their
room (Apple TV, Freesat, Google TV, CNN HD) and turn TVs on/off; admins get
full control of the matrix plus smart plug/light control.

## Status

Phase A — standalone matrix + Tapo clients you can run against the LAN to
validate hardware before any web stack is built.

## Architecture (target)

```
guests/admin
     │  https://hertford.info
     ▼
Cloudflare Edge ── Cloudflare Access (only on /admin/*)
     │
     │  Cloudflare Tunnel (outbound from Synology)
     ▼
Synology NAS (Docker)
 ├── cloudflared
 └── hertford (FastAPI on :8000)
          ├── matrix.py  ── TCP 192.168.8.241:23 (Blustream C66CS)
          └── tapo.py    ── plugp100 → 192.168.8.109 (Tapo P100 "TV System")
```

Phases:

1. **A — Hardware clients.** *(this commit)* `hertford.matrix` and
   `hertford.tapo` as runnable modules with small CLIs.
2. **B — Infra.** FastAPI skeleton, Dockerfile, docker-compose with
   cloudflared, deployed to Synology.
3. **C — Guest UI.** Room selector → source buttons → TV power → Wi-Fi info.
4. **D — Admin UI.** Matrix grid, presets, command log, password rotation.

## Phase A — running locally

You need to be on the home LAN (or have a Tailscale subnet router exposing
`192.168.8.0/24`).

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Matrix

```bash
python -m hertford.matrix status                       # show OUTSTA routing table
python -m hertford.matrix fw                           # firmware version
python -m hertford.matrix route 1 3                    # output 1 ← input 3
python -m hertford.matrix route 0 4                    # all outputs ← input 4
python -m hertford.matrix tv-on 4                      # CEC PON on output 4
python -m hertford.matrix tv-off 0                     # CEC POFF on all outputs
python -m hertford.matrix raw "FWVERSION"              # escape hatch
python -m hertford.matrix --host 192.168.8.241 -v status   # verbose, custom host
```

The matrix is at `192.168.8.241:23` by default. `GUEST ON`, `RESET`, and
`NET RB` are refused at the client layer — they kick active telnet sessions
or wipe config.

### Tapo

```bash
export TAPO_EMAIL="your-tapo-account@example.com"
export TAPO_PASSWORD="..."
# optional: export TAPO_HOST=192.168.8.109

python -m hertford.tapo info       # full device_info dump
python -m hertford.tapo status     # "on" | "off"
python -m hertford.tapo on
python -m hertford.tapo off
python -m hertford.tapo toggle
```

Use a dedicated TP-Link account that owns only the house's automation devices,
not your personal one.

## Hardware

| Device     | Model                    | LAN address           | Protocol            |
|------------|--------------------------|-----------------------|---------------------|
| Matrix     | Blustream C66CS 6x6      | `192.168.8.241:23`    | Telnet, CR/LF       |
| Plug       | Tapo P100 ("TV System")  | `192.168.8.109`       | plugp100 (Python)   |

Full matrix command reference: [C66CS.txt](C66CS.txt).

### Room and source mapping

Outputs (rooms):

| Room       | Output |
|------------|--------|
| lounge     | 1      |
| office-tv  | 2      |
| office-cnn | 3      |
| red-room   | 4      |
| grey-room  | 5      |
| output6    | 6      |

Inputs (sources):

| Source    | Input |
|-----------|-------|
| cnn-hd    | 1     |
| freesat   | 2     |
| apple-tv  | 3     |
| google-tv | 4     |

These will move to `config/rooms.yaml` in Phase B; for now the CLI takes raw
numbers.
