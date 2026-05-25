"""Auth helpers for hertford.

Two kinds of auth:

1. **Guest** — anyone with the shared `GUEST_PASSWORD` can use the guest UI.
   Implemented with a signed-cookie session (Starlette's SessionMiddleware).
   No accounts, no rotation per-user — one password printed on a card in the
   house. To rotate, change the env var and restart.

2. **Admin** — protected by Cloudflare Access at the tunnel layer. Anything
   reaching `/admin/*` came through CF Access; we trust the
   `Cf-Access-Authenticated-User-Email` header that CF injects on protected
   requests.
"""

from __future__ import annotations

import secrets

from fastapi import Request
from fastapi.responses import RedirectResponse


CF_ACCESS_EMAIL_HEADER = "Cf-Access-Authenticated-User-Email"


def is_guest_authed(request: Request) -> bool:
    return bool(request.session.get("guest"))


def grant_guest(request: Request) -> None:
    request.session["guest"] = True


def revoke_guest(request: Request) -> None:
    request.session.pop("guest", None)


def check_guest_password(submitted: str, expected: str | None) -> bool:
    if not expected:
        return False
    return secrets.compare_digest(submitted.encode(), expected.encode())


def cf_access_user(request: Request) -> str | None:
    """Return the CF-Access-authenticated user email if present, else None."""
    return request.headers.get(CF_ACCESS_EMAIL_HEADER) or None


def is_admin(request: Request) -> bool:
    """Admin = the request came through Cloudflare Access.

    Since cloudflared is the only public ingress and CF Access only fires on
    /admin/* paths, presence of the header is a sufficient signal.
    """
    return cf_access_user(request) is not None


def login_redirect(next_path: str = "/") -> RedirectResponse:
    """Redirect unauthed guests to /login, remembering where they were."""
    if next_path and next_path != "/":
        url = f"/login?next={next_path}"
    else:
        url = "/login"
    return RedirectResponse(url, status_code=303)
