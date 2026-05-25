# Hertford

Home media control system for the house. Lets guests pick TV sources in their
room (Apple TV, Freesat, Google TV, CNN HD) and turn TVs on/off; admins get
full control of the matrix plus smart plug/light control.

## Status

Phase B — FastAPI skeleton + Docker stack ready to deploy to Synology behind
a Cloudflare Tunnel.

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

1. **A — Hardware clients.** ✅ `hertford.matrix` and `hertford.tapo` as
   runnable modules with small CLIs.
2. **B — Infra.** ✅ FastAPI skeleton, Dockerfile, docker-compose with
   cloudflared. Routes: `/` (live status), `/admin` (CF Access user echoed),
   `/api/status` (JSON), `/healthz`.
3. **C — Guest UI.** Room selector → source buttons → TV power → Wi-Fi info,
   shared password auth.
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

Canonical mapping lives in [config/rooms.yaml](config/rooms.yaml); each room
also has a `visibility` of `guest` or `admin` which controls who sees it.

## Phase B — deploying to Synology

The stack is two containers behind a Cloudflare Tunnel:
[infra/docker-compose.yml](infra/docker-compose.yml).

### 1. Create the Cloudflare Tunnel

In the [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com/):

1. **Networks → Tunnels → Create a tunnel.** Pick "Cloudflared" as the
   connector type. Name it `hertford`.
2. On the "Install and run connector" step, select the **Docker** tab. Copy
   the `TUNNEL_TOKEN` value (the long string after `--token`) — you'll paste
   it into `.env`. Ignore the suggested `docker run` command; compose handles
   that.
3. **Public hostnames → Add a public hostname.**
   - Subdomain: blank (or whatever you want — e.g. `house`)
   - Domain: `hertford.info`
   - Service: `HTTP` → `hertford:8000`

   (Compose puts the FastAPI container on a network where it's reachable as
   the hostname `hertford` on port 8000.)

### 2. Add a Cloudflare Access policy for `/admin`

In Zero Trust:

1. **Access → Applications → Add an application → Self-hosted.**
2. Application domain: `hertford.info`, path: `/admin*`.
3. Add a policy that allows your email (and any other admins) — e.g.
   "Emails: matt.deegan@gmail.com".
4. Leave everything else default. Save.

Now `https://hertford.info/admin` will require a one-time email login through
Cloudflare; `https://hertford.info/` stays public (will be guest-password
gated in Phase C).

### 3. Set up the Synology

On the NAS, install **Container Manager** from Package Center if you haven't
already, then enable SSH (Control Panel → Terminal & SNMP → Enable SSH).

```bash
ssh msdeegan@192.168.8.237
sudo -i

# Pick a folder; convention on Synology is /volume1/docker/<app>
mkdir -p /volume1/docker
cd /volume1/docker
git clone https://github.com/msdeegan/hertford.git
cd hertford/infra
cp .env.example .env
nano .env   # paste TUNNEL_TOKEN, set GUEST_PASSWORD, TAPO_EMAIL, TAPO_PASSWORD
docker compose up -d --build
```

### 4. Verify

```bash
docker compose ps                              # both containers Up + healthy
docker compose logs -f hertford                # FastAPI logs
docker compose logs -f cloudflared             # tunnel connection logs
curl http://localhost/healthz                  # not exposed by compose; use docker exec
docker exec hertford curl -s http://127.0.0.1:8000/api/status
```

Then in a browser: `https://hertford.info/` should show the status table,
and `https://hertford.info/admin` should prompt for Cloudflare Access login
then echo your email.

### Updating

```bash
cd /volume1/docker/hertford
git pull
cd infra
docker compose up -d --build
```
