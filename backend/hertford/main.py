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
from .config import Config, Room, load_config
from .matrix import MatrixClient, MatrixError
from .tapo import TapoConfig, TapoError, TapoPlug

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# The Tapo plug ("TV System") is conceptually attached to this room's picker
# page — that's where the on/off toggle appears for guests.
TAPO_ROOM_ID = "red-room"


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    app.state.config = cfg
    app.state.matrix = MatrixClient(cfg.matrix_host, cfg.matrix_port)

    if cfg.tapo_email and cfg.tapo_password and cfg.tapo_host:
        app.state.tapo = TapoPlug(
            TapoConfig(host=cfg.tapo_host, email=cfg.tapo_email, password=cfg.tapo_password)
        )
        log.info("tapo plug configured at %s", cfg.tapo_host)
    else:
        app.state.tapo = None
        log.info("tapo not configured (TAPO_EMAIL/PASSWORD missing) — TV System button will be disabled")

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
            "is_admin_view": False,
        },
    )


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
            "back_url": "/admin" if room.visibility == "admin" else "/",
        },
    )


@app.post("/room/{room_id}/source/{source_id}")
async def room_switch(
    request: Request, room_id: str, source_id: str
) -> RedirectResponse:
    if redir := _require_guest(request):
        return redir
    cfg = _cfg(request)
    try:
        room = cfg.room(room_id)
        source = cfg.source(source_id)
    except KeyError:
        raise HTTPException(404)
    if not _room_visible(room, request):
        raise HTTPException(403)

    try:
        await _matrix(request).route(room.output, source.input)
    except (MatrixError, ValueError) as e:
        log.error("route %s→%s failed: %s", room.id, source.id, e)
        # fall through to redirect; the picker will show the (unchanged) state
    return RedirectResponse(f"/room/{room.id}", status_code=303)


@app.post("/tapo/tv-system/toggle")
async def tapo_toggle(request: Request) -> RedirectResponse:
    if redir := _require_guest(request):
        return redir
    plug = _tapo(request)
    if plug is None:
        raise HTTPException(503, detail="tapo not configured")
    try:
        if await plug.is_on():
            await plug.off()
        else:
            await plug.on()
        # Tapo's get_device_info() lags the relay command by a few hundred ms,
        # so without this the next page render still reads the old state.
        await asyncio.sleep(0.6)
    except TapoError as e:
        log.error("tapo toggle failed: %s", e)
    return RedirectResponse(f"/room/{TAPO_ROOM_ID}", status_code=303)


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
