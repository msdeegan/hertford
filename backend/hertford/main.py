"""FastAPI entrypoint for the hertford control app."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets as secrets_mod
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from .atv import AppleTV, ATVError
from .atv import discover_lan as discover_atv
from .config import Config, Room, load_config
from .gtv import GoogleTV, GTVError
from .gtv import discover_lan as discover_gtv
from .keylight import KeyLight, KeyLightError
from .keylight import discover_lan as discover_keylight
from .matrix import MatrixClient, MatrixError
from .neewer import NeewerBridge, NeewerError
from .tapo import TapoConfig, TapoError, TapoPlug
from .tuya import TuyaBulb, TuyaBulbConfig, TuyaError

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# The Tapo plug ("TV System") is conceptually attached to this room's picker
# page — that's where the on/off toggle appears for guests.
TAPO_ROOM_ID = "red-room"

# Named Tapo plugs. Host comes from <host_env>; falls back to default_host so
# Matt's existing TAPO_HOST env keeps working.
TAPO_PLUGS = [
    {"id": "tv-system", "label": "TV System", "host_env": "TAPO_HOST"},
    {"id": "lounge", "label": "Lounge lamp", "host_env": "TAPO_LOUNGE_HOST"},
]

# Tuya/SmartLife bulbs (controlled via tinytuya local-LAN protocol).
TUYA_BULBS = [
    {"id": "lounge", "label": "Lounge bulb", "env_prefix": "TUYA_LOUNGE"},
]

# Which room shows the grouped Lounge-lights control (Tapo plug + Tuya bulb).
LOUNGE_ROOM_ID = "lounge"
LOUNGE_TAPO_PLUG_ID = "lounge"
LOUNGE_TUYA_BULB_ID = "lounge"

# Source IDs whose pickers show a "Remote" button (and which the remote
# controls). Keep in sync with config/rooms.yaml.
GTV_SOURCE_ID = "google-tv"
ATV_SOURCE_ID = "apple-tv"

# Where each remote's cert/credentials live (mounted from host in compose).
GTV_STATE_DIR = "/data/gtv"
ATV_STATE_DIR = "/data/atv"

# Elgato Key Light in the Office; persistent host on disk.
KEYLIGHT_ROOM_ID = "office"
KEYLIGHT_STATE_DIR = "/data/keylight"

# Neewer BLE lights in the Office (controlled via the Mac-side bridge).
NEEWER_ROOM_ID = "office"
NEEWER_LIGHTS = [
    {"id": "key1", "label": "Neewer 1"},
    {"id": "key2", "label": "Neewer 2"},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    app.state.config = cfg
    app.state.matrix = MatrixClient(cfg.matrix_host, cfg.matrix_port)

    # Multi-Tapo. Each plug shares the same TP-Link account creds (cfg.tapo_*),
    # but each has its own host.
    app.state.tapo_plugs = {}
    if cfg.tapo_email and cfg.tapo_password:
        for plug in TAPO_PLUGS:
            host = os.environ.get(plug["host_env"])
            if plug["id"] == "tv-system" and not host:
                host = cfg.tapo_host  # legacy TAPO_HOST default
            if host:
                app.state.tapo_plugs[plug["id"]] = TapoPlug(
                    TapoConfig(host=host, email=cfg.tapo_email, password=cfg.tapo_password)
                )
                log.info("tapo plug %s at %s", plug["id"], host)
    else:
        log.info("tapo not configured (TAPO_EMAIL/PASSWORD missing) — plug buttons disabled")
    # Backwards-compat: keep app.state.tapo pointing at the TV-System plug.
    app.state.tapo = app.state.tapo_plugs.get("tv-system")

    # Tuya/SmartLife bulbs.
    app.state.tuya_bulbs = {}
    for bulb in TUYA_BULBS:
        bcfg = TuyaBulbConfig.from_env(bulb["id"], bulb["label"], bulb["env_prefix"])
        if bcfg:
            app.state.tuya_bulbs[bulb["id"]] = TuyaBulb(bcfg)
            log.info("tuya bulb %s at %s", bulb["id"], bcfg.ip)

    app.state.gtv = GoogleTV(state_dir=os.environ.get("GTV_STATE_DIR", GTV_STATE_DIR))
    log.info(
        "google tv: %s (state at %s)",
        f"paired with {app.state.gtv.host}" if app.state.gtv.paired else "not paired",
        app.state.gtv.state_dir,
    )

    app.state.atv = AppleTV(state_dir=os.environ.get("ATV_STATE_DIR", ATV_STATE_DIR))
    log.info(
        "apple tv: %s (state at %s)",
        f"paired with {app.state.atv.host}" if app.state.atv.paired else "not paired",
        app.state.atv.state_dir,
    )

    app.state.keylight = KeyLight(
        host=os.environ.get("ELGATO_HOST") or None,
        state_dir=os.environ.get("ELGATO_STATE_DIR", KEYLIGHT_STATE_DIR),
    )
    log.info(
        "elgato key light: %s",
        f"host {app.state.keylight.host}" if app.state.keylight.host else "not configured",
    )

    app.state.neewer = NeewerBridge(os.environ.get("NEEWER_BRIDGE_URL") or None)
    log.info(
        "neewer bridge: %s",
        app.state.neewer.base_url or "not configured",
    )

    # Home public networks, used to auto-auth visitors on the same WAN.
    # Each entry is an ipaddress network (IPv4 /32, IPv6 /64).
    app.state.home_networks = _parse_home_networks_env(cfg.home_public_ip)
    refresh_task: asyncio.Task | None = None
    if app.state.home_networks:
        log.info(
            "home networks from HOME_PUBLIC_IP env: %s",
            [str(n) for n in app.state.home_networks],
        )
    else:
        refresh_task = asyncio.create_task(_refresh_home_ip_loop(app))

    log.info("hertford starting; matrix=%s:%s", cfg.matrix_host, cfg.matrix_port)
    try:
        yield
    finally:
        if refresh_task is not None:
            refresh_task.cancel()
        await app.state.matrix.close()
        await app.state.gtv.shutdown()
        await app.state.atv.shutdown()


async def _refresh_home_ip_loop(app: FastAPI) -> None:
    """Detect the home's public IPv4 + IPv6 networks from outbound traffic.

    The NAS lives on the home LAN, so its public-facing IPs ARE the home's.
    For IPv4 every device on the WAN looks like one /32; for IPv6 every
    device gets a unique address inside the ISP-assigned /64, so we match by
    prefix.

    Re-checks every hour so dynamic-IP changes self-heal.
    """
    import ipaddress

    import aiohttp

    while True:
        nets: list[ipaddress._BaseNetwork] = []
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as session:
                for url in ("https://api.ipify.org", "https://api64.ipify.org"):
                    try:
                        async with session.get(url) as resp:
                            ip_str = (await resp.text()).strip()
                        addr = ipaddress.ip_address(ip_str)
                        prefix = 32 if isinstance(addr, ipaddress.IPv4Address) else 64
                        net = ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
                        if net not in nets:
                            nets.append(net)
                    except Exception as inner:
                        log.warning("IP probe %s failed: %s", url, inner)
            if nets:
                old = getattr(app.state, "home_networks", []) or []
                if {str(n) for n in nets} != {str(n) for n in old}:
                    log.info(
                        "home networks %s: %s",
                        "detected" if not old else "updated",
                        [str(n) for n in nets],
                    )
                    app.state.home_networks = nets
        except Exception as e:
            log.warning("home network detection failed: %s", e)
        await asyncio.sleep(3600)


def _parse_home_networks_env(value: str | None) -> list:
    """Parse comma-separated CIDRs / bare IPs from the HOME_PUBLIC_IP env var."""
    import ipaddress

    if not value:
        return []
    out = []
    for piece in value.split(","):
        s = piece.strip()
        if not s:
            continue
        try:
            if "/" in s:
                out.append(ipaddress.ip_network(s, strict=False))
            else:
                addr = ipaddress.ip_address(s)
                prefix = 32 if isinstance(addr, ipaddress.IPv4Address) else 64
                out.append(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))
        except ValueError as e:
            log.warning("ignoring bad HOME_PUBLIC_IP entry %r: %s", s, e)
    return out


app = FastAPI(title="Hertford", lifespan=lifespan)

_secret = os.environ.get("SECRET_KEY") or secrets_mod.token_hex(32)
if not os.environ.get("SECRET_KEY"):
    log.warning("SECRET_KEY not set; using a random one — guest sessions won't survive restarts")
app.add_middleware(
    SessionMiddleware,
    secret_key=_secret,
    same_site="lax",
    https_only=False,  # cloudflared terminates TLS; the container sees HTTP
    max_age=60 * 60 * 24 * 30,
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["is_admin"] = auth.is_admin
templates.env.globals["is_guest_authed"] = auth.is_guest_authed


# ====================================================================== helpers


def _cfg(request: Request) -> Config:
    return request.app.state.config


def _matrix(request: Request) -> MatrixClient:
    return request.app.state.matrix


def _tapo(request: Request) -> TapoPlug | None:
    return request.app.state.tapo


async def _routing(request: Request) -> tuple[dict[int, int], str | None]:
    """Return (output→input map, error). Never raises."""
    try:
        return await _matrix(request).status(), None
    except MatrixError as e:
        log.warning("matrix status failed: %s", e)
        return {}, str(e)


async def _tapo_state(request: Request) -> tuple[bool | None, str | None]:
    """Return (is_on, error). is_on=None if no Tapo configured."""
    plug = _tapo(request)
    if plug is None:
        return None, None
    try:
        return await plug.is_on(), None
    except TapoError as e:
        log.warning("tapo state failed: %s", e)
        return None, str(e)


def _require_guest(request: Request) -> RedirectResponse | None:
    """Returns a redirect if the request isn't guest-authed; None otherwise."""
    if auth.is_guest_authed(request) or auth.is_admin(request):
        return None
    return auth.login_redirect(request.url.path)


def _require_admin(request: Request) -> None:
    if not auth.is_admin(request):
        raise HTTPException(status_code=403, detail="admin required")


def _room_visible(room: Room, request: Request) -> bool:
    """Guest rooms always visible to authed users; admin rooms only to admins."""
    if room.visibility == "guest":
        return True
    return auth.is_admin(request)


# ====================================================================== health


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/manifest.webmanifest")
async def manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": "Hertford Road",
            "short_name": "Hertford",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#f8fafc",
            "theme_color": "#0f172a",
            "icons": [
                {
                    "src": "/icon.svg",
                    "type": "image/svg+xml",
                    "sizes": "any",
                    "purpose": "any maskable",
                },
            ],
        },
        media_type="application/manifest+json",
    )


@app.get("/icon.svg")
async def icon() -> "Response":
    from fastapi.responses import Response

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
        '<rect width="512" height="512" fill="#0f172a" rx="112"/>'
        '<text x="50%" y="50%" text-anchor="middle" dominant-baseline="central" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif" '
        'font-size="320" font-weight="900" fill="white">H</text>'
        "</svg>"
    )
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/whoami")
async def whoami(request: Request) -> dict:
    """Debug: shows what the app thinks the request looks like."""
    nets = getattr(request.app.state, "home_networks", []) or []
    return {
        "cf_connecting_ip": request.headers.get("CF-Connecting-IP"),
        "x_forwarded_for": request.headers.get("X-Forwarded-For"),
        "home_networks_cached": [str(n) for n in nets],
        "on_home_network": auth.is_on_home_network(request),
        "guest_authed": auth.is_guest_authed(request),
    }


@app.get("/api/status")
async def api_status(request: Request) -> JSONResponse:
    cfg = _cfg(request)
    routing, error = await _routing(request)
    return JSONResponse(
        {
            "matrix": {
                "host": cfg.matrix_host,
                "ok": error is None,
                "error": error,
                "routing": {
                    (cfg.room_by_output(out).id if cfg.room_by_output(out) else f"out{out}"): (
                        cfg.source_by_input(inp).id if cfg.source_by_input(inp) else f"in{inp}"
                    )
                    for out, inp in routing.items()
                },
            }
        }
    )


# ======================================================================= login


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/", error: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next, "error": error, "config": _cfg(request)},
    )


@app.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(...),
    next: str = Form("/"),
) -> RedirectResponse:
    cfg = _cfg(request)
    if not auth.check_guest_password(password, cfg.guest_password):
        return RedirectResponse(f"/login?error=1&next={next}", status_code=303)
    auth.grant_guest(request)
    # only redirect to internal paths
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
    return RedirectResponse(safe_next, status_code=303)


@app.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    auth.revoke_guest(request)
    return RedirectResponse("/login", status_code=303)


# ===================================================================== guest UI


@app.get("/", response_class=HTMLResponse, response_model=None)
async def home(request: Request):
    if redir := _require_guest(request):
        return redir

    cfg = _cfg(request)
    routing, matrix_error = await _routing(request)
    is_admin = auth.is_admin(request)

    tiles = []
    for room in cfg.rooms:
        if not _room_visible(room, request):
            continue
        inp = routing.get(room.output)
        src = cfg.source_by_input(inp) if inp is not None else None
        tiles.append({"room": room, "source": src, "raw_input": inp})

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "config": cfg,
            "tiles": tiles,
            "matrix_error": matrix_error,
            # When admin's signed in we mark the view as admin so tile links
            # use /admin/room/{id} (which works for any room, including the
            # admin-visibility Office one).
            "is_admin_view": is_admin,
        },
    )


async def _render_picker(request: Request, room: Room, *, is_admin_view: bool):
    """Shared picker render — used by both guest and admin routes."""
    cfg = _cfg(request)
    routing, matrix_error = await _routing(request)
    current = cfg.source_by_input(routing.get(room.output)) if routing else None

    input_status: dict[int, dict] = {}
    if not matrix_error:
        try:
            input_status = await _matrix(request).input_status()
        except MatrixError as e:
            log.warning("input status failed: %s", e)

    tapo_on = None
    tapo_error = None
    if room.id == TAPO_ROOM_ID:
        tapo_on, tapo_error = await _tapo_state(request)

    # Lounge: combined plug + bulb panel.
    show_lounge_lights = room.id == LOUNGE_ROOM_ID
    lounge_tapo_configured = LOUNGE_TAPO_PLUG_ID in request.app.state.tapo_plugs
    lounge_tuya_configured = LOUNGE_TUYA_BULB_ID in request.app.state.tuya_bulbs
    lounge_tuya_state = None
    lounge_tuya_error = None
    if show_lounge_lights and lounge_tuya_configured:
        try:
            lounge_tuya_state = await request.app.state.tuya_bulbs[LOUNGE_TUYA_BULB_ID].status()
        except TuyaError as e:
            lounge_tuya_error = str(e)
            log.warning("tuya status failed: %s", e)

    # Key light state only fetched for the Office picker, admin view only.
    keylight_state = None
    keylight_error = None
    show_keylight = is_admin_view and room.id == KEYLIGHT_ROOM_ID
    keylight: KeyLight = request.app.state.keylight
    if show_keylight and keylight.host:
        try:
            keylight_state = await keylight.state()
        except KeyLightError as e:
            keylight_error = str(e)
            log.warning("keylight state failed: %s", e)

    # Neewer lights (via Mac bridge) — same admin/office gating.
    show_neewer = is_admin_view and room.id == NEEWER_ROOM_ID
    neewer: NeewerBridge = request.app.state.neewer
    neewer_healthy = False
    if show_neewer:
        neewer_healthy = await neewer.healthy()

    return templates.TemplateResponse(
        request,
        "picker.html",
        {
            "config": cfg,
            "room": room,
            "current": current,
            "matrix_error": matrix_error,
            "input_status": input_status,
            "show_tapo": room.id == TAPO_ROOM_ID,
            "tapo_configured": _tapo(request) is not None,
            "tapo_on": tapo_on,
            "tapo_error": tapo_error,
            "show_gtv_remote": current is not None and current.id == GTV_SOURCE_ID,
            "show_atv_remote": current is not None and current.id == ATV_SOURCE_ID,
            "show_keylight": show_keylight,
            "keylight_host": keylight.host,
            "keylight_state": keylight_state,
            "keylight_error": keylight_error,
            "show_neewer": show_neewer,
            "neewer_configured": neewer.configured,
            "neewer_healthy": neewer_healthy,
            "neewer_lights": NEEWER_LIGHTS,
            "show_lounge_lights": show_lounge_lights,
            "lounge_tapo_configured": lounge_tapo_configured,
            "lounge_tuya_configured": lounge_tuya_configured,
            "lounge_tuya_id": LOUNGE_TUYA_BULB_ID,
            "lounge_tapo_id": LOUNGE_TAPO_PLUG_ID,
            "lounge_tuya_state": lounge_tuya_state,
            "lounge_tuya_error": lounge_tuya_error,
            "tapo_room_plug_id": TAPO_ROOM_ID and "tv-system",
            "is_admin_view": is_admin_view,
            "back_url": "/admin" if is_admin_view else "/",
        },
    )


async def _do_switch(request: Request, room: Room, source_id: str, *, redirect_to: str):
    cfg = _cfg(request)
    try:
        source = cfg.source(source_id)
    except KeyError:
        raise HTTPException(404)
    try:
        await _matrix(request).route(room.output, source.input)
    except (MatrixError, ValueError) as e:
        log.error("route %s→%s failed: %s", room.id, source.id, e)
    return RedirectResponse(redirect_to, status_code=303)


@app.get("/room/{room_id}", response_class=HTMLResponse, response_model=None)
async def room_picker(request: Request, room_id: str):
    if redir := _require_guest(request):
        return redir
    cfg = _cfg(request)
    try:
        room = cfg.room(room_id)
    except KeyError:
        raise HTTPException(404)
    if not _room_visible(room, request):
        raise HTTPException(404)  # don't leak existence of admin rooms
    return await _render_picker(request, room, is_admin_view=False)


@app.post("/room/{room_id}/source/{source_id}")
async def room_switch(
    request: Request, room_id: str, source_id: str
) -> RedirectResponse:
    if redir := _require_guest(request):
        return redir
    cfg = _cfg(request)
    try:
        room = cfg.room(room_id)
    except KeyError:
        raise HTTPException(404)
    if not _room_visible(room, request):
        raise HTTPException(403)
    return await _do_switch(request, room, source_id, redirect_to=f"/room/{room.id}")


@app.post("/tapo/{plug_id}/toggle")
async def tapo_toggle(request: Request, plug_id: str) -> RedirectResponse:
    if redir := _require_guest(request):
        return redir
    plug = request.app.state.tapo_plugs.get(plug_id)
    if plug is None:
        raise HTTPException(404, detail=f"no plug {plug_id!r}")
    redirect = request.headers.get("Referer") or f"/room/{TAPO_ROOM_ID}"
    try:
        if await plug.is_on():
            await plug.off()
        else:
            await plug.on()
        # Tapo's get_device_info() lags the relay command by a few hundred ms.
        await asyncio.sleep(0.6)
    except TapoError as e:
        log.error("tapo toggle %s failed: %s", plug_id, e)
    return RedirectResponse(redirect, status_code=303)


# ============================================================ tuya (smartlife)


@app.post("/tuya/{bulb_id}/power")
async def tuya_power(request: Request, bulb_id: str, on: bool = Form(...)):
    if redir := _require_guest(request):
        return redir
    bulb: TuyaBulb | None = request.app.state.tuya_bulbs.get(bulb_id)
    if bulb is None:
        raise HTTPException(404, detail=f"no tuya bulb {bulb_id!r}")
    try:
        await bulb.power(on)
    except TuyaError as e:
        log.warning("tuya %s power failed: %s", bulb_id, e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse({"ok": True})


@app.post("/tuya/{bulb_id}/brightness")
async def tuya_brightness(request: Request, bulb_id: str, value: int = Form(...)):
    if redir := _require_guest(request):
        return redir
    bulb: TuyaBulb | None = request.app.state.tuya_bulbs.get(bulb_id)
    if bulb is None:
        raise HTTPException(404)
    try:
        await bulb.set_brightness(value)
    except TuyaError as e:
        log.warning("tuya %s brightness failed: %s", bulb_id, e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse({"ok": True})


@app.post("/tuya/{bulb_id}/kelvin")
async def tuya_kelvin(request: Request, bulb_id: str, value: int = Form(...)):
    if redir := _require_guest(request):
        return redir
    bulb: TuyaBulb | None = request.app.state.tuya_bulbs.get(bulb_id)
    if bulb is None:
        raise HTTPException(404)
    try:
        await bulb.set_kelvin(value)
    except TuyaError as e:
        log.warning("tuya %s kelvin failed: %s", bulb_id, e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse({"ok": True})


# ============================================================ lounge group


@app.post("/lounge-lights/toggle")
async def lounge_lights_toggle(request: Request, on: bool = Form(...)):
    """Set both Lounge lights to the same state (plug + Tuya bulb)."""
    if redir := _require_guest(request):
        return redir
    errors = []
    plug = request.app.state.tapo_plugs.get(LOUNGE_TAPO_PLUG_ID)
    if plug:
        try:
            if on:
                await plug.on()
            else:
                await plug.off()
        except Exception as e:
            errors.append(f"plug: {e}")
    bulb = request.app.state.tuya_bulbs.get(LOUNGE_TUYA_BULB_ID)
    if bulb:
        try:
            await bulb.power(on)
        except Exception as e:
            errors.append(f"bulb: {e}")
    return JSONResponse({"ok": not errors, "errors": errors})


@app.get("/wifi", response_class=HTMLResponse, response_model=None)
async def wifi(request: Request):
    if redir := _require_guest(request):
        return redir
    cfg = _cfg(request)
    qr_svg = _wifi_qr_svg(cfg) if cfg.wifi_ssid and cfg.wifi_password else None
    return templates.TemplateResponse(
        request,
        "wifi.html",
        {
            "config": cfg,
            "qr_svg": qr_svg,
            "wifi_ssid": cfg.wifi_ssid,
            "wifi_password": cfg.wifi_password,
        },
    )


def _wifi_qr_svg(cfg: Config) -> str:
    """Generate an inline SVG QR code that a phone camera can scan to join WiFi."""
    import io

    import segno

    # Standard WiFi QR payload per https://en.wikipedia.org/wiki/QR_code#Joining_a_Wi-Fi_network
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace('"', '\\"').replace(":", "\\:")

    payload = f"WIFI:T:{cfg.wifi_auth};S:{esc(cfg.wifi_ssid)};P:{esc(cfg.wifi_password)};;"
    qr = segno.make(payload, error="h")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=10, border=2, dark="#0f172a", xmldecl=False, svgns=False, omitsize=False)
    return buf.getvalue().decode("utf-8")


# ===================================================================== admin UI


# ============================================================ google tv remote


# Allowed key names — keep tight so /remote/key/<x> can't send arbitrary stuff.
_GTV_ALLOWED_KEYS = {
    "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "DPAD_CENTER",
    "BACK", "HOME", "VOLUME_UP", "VOLUME_DOWN", "VOLUME_MUTE",
}


@app.get("/remote", response_class=HTMLResponse, response_model=None)
async def remote_page(request: Request):
    if redir := _require_guest(request):
        return redir
    gtv: GoogleTV = request.app.state.gtv
    return templates.TemplateResponse(
        request,
        "remote.html",
        {
            "config": _cfg(request),
            "paired": gtv.paired,
            "host": gtv.host,
            "pairing_active": gtv._pairing is not None,
        },
    )


@app.post("/remote/discover")
async def remote_discover(request: Request):
    if redir := _require_guest(request):
        return redir
    try:
        candidates = await discover_lan()
    except Exception as e:
        log.warning("gtv discover failed: %s", e)
        candidates = []
    return JSONResponse({"candidates": candidates})


@app.post("/remote/pair/start")
async def remote_pair_start(request: Request, host: str = Form(...)):
    if redir := _require_guest(request):
        return redir
    gtv: GoogleTV = request.app.state.gtv
    try:
        await gtv.start_pairing(host)
    except Exception as e:
        log.error("pair start failed: %s", e)
        return RedirectResponse(f"/remote?error=pair_start&msg={e}", status_code=303)
    return RedirectResponse("/remote", status_code=303)


@app.post("/remote/pair/complete")
async def remote_pair_complete(request: Request, pin: str = Form(...)):
    if redir := _require_guest(request):
        return redir
    gtv: GoogleTV = request.app.state.gtv
    try:
        await gtv.finish_pairing(pin)
    except GTVError as e:
        log.error("pair complete failed: %s", e)
        return RedirectResponse(f"/remote?error=pair_complete&msg={e}", status_code=303)
    return RedirectResponse("/remote", status_code=303)


@app.post("/remote/forget")
async def remote_forget(request: Request):
    if redir := _require_guest(request):
        return redir
    request.app.state.gtv.forget()
    return RedirectResponse("/remote", status_code=303)


@app.post("/remote/key/{key_name}")
async def remote_key(request: Request, key_name: str):
    if redir := _require_guest(request):
        return redir
    if key_name not in _GTV_ALLOWED_KEYS:
        raise HTTPException(400, detail=f"key not allowed: {key_name}")
    gtv: GoogleTV = request.app.state.gtv
    try:
        await gtv.send_key(key_name)
    except GTVError as e:
        log.warning("gtv key %s failed: %s", key_name, e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse({"ok": True})


# ============================================================ apple tv remote


# pyatv RemoteControl has lots of methods; whitelist the ones we expose.
# (Apple TV has no mute key — volume_up/down only.)
_ATV_ALLOWED_KEYS = {
    "up", "down", "left", "right", "select",
    "menu", "home", "play_pause", "volume_up", "volume_down",
}


@app.get("/atv", response_class=HTMLResponse, response_model=None)
async def atv_page(request: Request):
    if redir := _require_guest(request):
        return redir
    atv: AppleTV = request.app.state.atv
    return templates.TemplateResponse(
        request,
        "atv.html",
        {
            "config": _cfg(request),
            "paired": atv.paired,
            "host": atv.host,
            "name": atv.name,
            "pairing_active": atv._pairing is not None,
        },
    )


@app.post("/atv/discover")
async def atv_discover(request: Request):
    if redir := _require_guest(request):
        return redir
    try:
        candidates = await discover_atv()
    except Exception as e:
        log.warning("atv discover failed: %s", e)
        candidates = []
    return JSONResponse({"candidates": candidates})


@app.post("/atv/pair/start")
async def atv_pair_start(request: Request, host: str = Form(...)):
    if redir := _require_guest(request):
        return redir
    atv: AppleTV = request.app.state.atv
    try:
        await atv.start_pairing(host)
    except Exception as e:
        log.error("atv pair start failed: %s", e)
        return RedirectResponse(f"/atv?error=pair_start&msg={e}", status_code=303)
    return RedirectResponse("/atv", status_code=303)


@app.post("/atv/pair/complete")
async def atv_pair_complete(request: Request, pin: str = Form(...)):
    if redir := _require_guest(request):
        return redir
    atv: AppleTV = request.app.state.atv
    try:
        await atv.finish_pairing(pin)
    except ATVError as e:
        log.error("atv pair complete failed: %s", e)
        return RedirectResponse(f"/atv?error=pair_complete&msg={e}", status_code=303)
    return RedirectResponse("/atv", status_code=303)


@app.post("/atv/forget")
async def atv_forget(request: Request):
    if redir := _require_guest(request):
        return redir
    request.app.state.atv.forget()
    return RedirectResponse("/atv", status_code=303)


@app.post("/atv/key/{key_name}")
async def atv_key(request: Request, key_name: str):
    if redir := _require_guest(request):
        return redir
    if key_name not in _ATV_ALLOWED_KEYS:
        raise HTTPException(400, detail=f"key not allowed: {key_name}")
    atv: AppleTV = request.app.state.atv
    try:
        await atv.send_key(key_name)
    except ATVError as e:
        log.warning("atv key %s failed: %s", key_name, e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse({"ok": True})


@app.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request) -> HTMLResponse:
    _require_admin(request)

    cfg = _cfg(request)
    routing, matrix_error = await _routing(request)

    tiles = []
    for room in cfg.rooms:
        inp = routing.get(room.output)
        src = cfg.source_by_input(inp) if inp is not None else None
        tiles.append({"room": room, "source": src, "raw_input": inp})

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "config": cfg,
            "tiles": tiles,
            "matrix_error": matrix_error,
            "is_admin_view": True,
            "cf_user": auth.cf_access_user(request) or "(no header)",
        },
    )


@app.get("/admin/room/{room_id}", response_class=HTMLResponse, response_model=None)
async def admin_room_picker(request: Request, room_id: str):
    """Admin picker — same render as /room/{id} but under CF Access; admins can
    see any room regardless of visibility."""
    _require_admin(request)
    cfg = _cfg(request)
    try:
        room = cfg.room(room_id)
    except KeyError:
        raise HTTPException(404)
    return await _render_picker(request, room, is_admin_view=True)


@app.post("/admin/room/{room_id}/source/{source_id}")
async def admin_room_switch(
    request: Request, room_id: str, source_id: str
) -> RedirectResponse:
    _require_admin(request)
    cfg = _cfg(request)
    try:
        room = cfg.room(room_id)
    except KeyError:
        raise HTTPException(404)
    return await _do_switch(request, room, source_id, redirect_to=f"/admin/room/{room.id}")


# ============================================================ key light


_KEYLIGHT_REDIRECT = f"/admin/room/{KEYLIGHT_ROOM_ID}"


@app.post("/admin/keylight/discover")
async def keylight_discover(request: Request):
    _require_admin(request)
    try:
        candidates = await discover_keylight()
    except Exception as e:
        log.warning("keylight discover failed: %s", e)
        candidates = []
    return JSONResponse({"candidates": candidates})


@app.post("/admin/keylight/set-host")
async def keylight_set_host(request: Request, host: str = Form(...)):
    _require_admin(request)
    request.app.state.keylight.set_host(host.strip())
    return RedirectResponse(_KEYLIGHT_REDIRECT, status_code=303)


@app.post("/admin/keylight/forget")
async def keylight_forget(request: Request):
    _require_admin(request)
    request.app.state.keylight.forget()
    return RedirectResponse(_KEYLIGHT_REDIRECT, status_code=303)


@app.post("/admin/keylight/toggle")
async def keylight_toggle(request: Request):
    _require_admin(request)
    kl: KeyLight = request.app.state.keylight
    try:
        current = await kl.state()
        await kl.set(on=not current["on"])
    except KeyLightError as e:
        log.warning("keylight toggle failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse({"ok": True})


@app.post("/admin/keylight/set")
async def keylight_set(
    request: Request,
    brightness: int | None = Form(None),
    kelvin: int | None = Form(None),
):
    """Atomic set of brightness and/or colour temperature."""
    _require_admin(request)
    kl: KeyLight = request.app.state.keylight
    try:
        await kl.set(brightness=brightness, kelvin=kelvin)
    except KeyLightError as e:
        log.warning("keylight set failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse({"ok": True})


# ============================================================ neewer lights


def _valid_neewer_id(light_id: str) -> bool:
    return any(l["id"] == light_id for l in NEEWER_LIGHTS)


@app.post("/admin/neewer/{light_id}/power")
async def neewer_power(request: Request, light_id: str, on: bool = Form(...)):
    _require_admin(request)
    if not _valid_neewer_id(light_id):
        raise HTTPException(404)
    try:
        await request.app.state.neewer.power(light_id, on)
    except NeewerError as e:
        log.warning("neewer %s power failed: %s", light_id, e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse({"ok": True})


@app.post("/admin/neewer/{light_id}/cct")
async def neewer_cct(
    request: Request,
    light_id: str,
    brightness: int = Form(...),
    cct: int = Form(...),
):
    _require_admin(request)
    if not _valid_neewer_id(light_id):
        raise HTTPException(404)
    try:
        await request.app.state.neewer.set_cct(light_id, brightness, cct)
    except NeewerError as e:
        log.warning("neewer %s cct failed: %s", light_id, e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return JSONResponse({"ok": True})
