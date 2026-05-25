"""FastAPI entrypoint for the hertford control app.

Phase B: skeleton with a live-status home page, an admin page that echoes the
Cloudflare Access user header, and a JSON status API. Auth, source switching
and the proper guest UI land in Phase C.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .config import Config, load_config
from .matrix import MatrixClient, MatrixError

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    app.state.config = cfg
    app.state.matrix = MatrixClient(cfg.matrix_host, cfg.matrix_port)
    log.info("hertford starting; matrix=%s:%s", cfg.matrix_host, cfg.matrix_port)
    try:
        yield
    finally:
        await app.state.matrix.close()


app = FastAPI(title="Hertford", lifespan=lifespan)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# -------------------------------------------------------------------- routes


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/status")
async def api_status(request: Request) -> JSONResponse:
    cfg: Config = request.app.state.config
    matrix: MatrixClient = request.app.state.matrix
    try:
        routing = await matrix.status()
        matrix_ok = True
        error = None
    except MatrixError as e:
        routing = {}
        matrix_ok = False
        error = str(e)

    return JSONResponse(
        {
            "matrix": {
                "host": cfg.matrix_host,
                "ok": matrix_ok,
                "error": error,
                "routing": {
                    cfg.room_by_output(out).id if cfg.room_by_output(out) else f"out{out}": (
                        cfg.source_by_input(inp).id if cfg.source_by_input(inp) else f"in{inp}"
                    )
                    for out, inp in routing.items()
                }
                if matrix_ok
                else {},
            }
        }
    )


@app.get("/", response_class=HTMLResponse)
async def root(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "config": request.app.state.config,
            "status": await _gather_status(request),
        },
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request) -> HTMLResponse:
    cf_user = request.headers.get("Cf-Access-Authenticated-User-Email", "(no CF Access header)")
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "config": request.app.state.config,
            "cf_user": cf_user,
            "status": await _gather_status(request),
        },
    )


# -------------------------------------------------------------------- helpers


async def _gather_status(request: Request) -> dict:
    """Fetch matrix status with named rooms/sources, swallowing errors."""
    cfg: Config = request.app.state.config
    matrix: MatrixClient = request.app.state.matrix
    try:
        routing = await matrix.status()
        rows = []
        for room in cfg.rooms:
            inp = routing.get(room.output)
            src = cfg.source_by_input(inp) if inp is not None else None
            rows.append(
                {
                    "room": room,
                    "source": src,
                    "raw_input": inp,
                }
            )
        return {"ok": True, "rows": rows, "error": None}
    except MatrixError as e:
        return {"ok": False, "rows": [], "error": str(e)}
