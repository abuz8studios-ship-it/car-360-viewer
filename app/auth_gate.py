"""
Auth Gate — LotVue360 enterprise sign-in enforcement.
Gate-by-default: every route requires a valid JWT (Authorization: Bearer …
or HttpOnly cookie `lotvue_token`) unless explicitly public.

Public surface (the product still has to work for buyers + crawlers):
  - viewer/embed pages + their read-only data APIs + media assets
  - lead SUBMISSION (buyers) + analytics beacon
  - syndication feeds (Google/Facebook fetch anonymously)
  - auth/register/login/pricing + Stripe webhook/config + health

Wired from main.py:  from auth_gate import install_auth_gate; install_auth_gate(app)
"""

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

# Exact-match public paths
PUBLIC_EXACT = {
    "/", "/login", "/register", "/pricing", "/api/health",
    "/api/register", "/api/login", "/api/plans", "/api/stripe/config",
    "/api/stripe/webhook", "/api/create-checkout-session",
    "/api/auth/token", "/api/auth/refresh", "/api/auth/logout",
    "/mobile", "/mobile/manifest.json", "/mobile/sw.js",
    "/favicon.ico", "/api/analytics/event", "/api/leads/submit",
    "/api/unsubscribe",
}

# Prefix-match public paths (any method — pages, assets, feeds)
PUBLIC_PREFIXES = (
    "/view/", "/embed/", "/viewer/", "/view3d/",
    "/showroom/",                 # public dealer storefront microsite
    "/storage/", "/static/",
    "/api/syndication/", "/api/feeds/",
    "/api/leadmagnet/",           # buyer-facing conversion tools (trade-in/price/history/etc.)
    "/api/chat/",                 # buyer-facing AI assistant
    "/docs", "/openapi.json", "/redoc",
)

# Prefix-match public for GET ONLY (read-only viewer data; writes stay gated)
PUBLIC_GET_PREFIXES = (
    "/api/hotspots/",            # viewer hotspot overlay (read)
    "/api/backgrounds",          # viewer background thumbnails (read)
)


def _is_public_get_special(path: str) -> bool:
    # read-only viewer payload + showroom inventory JSON
    if path.startswith("/api/vehicles/") and path.endswith("/viewer"):
        return True
    if path.startswith("/api/showroom/") and path.endswith("/inventory"):
        return True
    return False

# Methods that are public on specific paths (buyer lead submission)
PUBLIC_METHOD_PATHS = {
    ("POST", "/api/leads"),
}


def _is_public(method: str, path: str) -> bool:
    if path in PUBLIC_EXACT:
        return True
    if (method, path) in PUBLIC_METHOD_PATHS:
        return True
    for p in PUBLIC_PREFIXES:
        if path.startswith(p):
            return True
    if method == "GET":
        for p in PUBLIC_GET_PREFIXES:
            if path.startswith(p):
                return True
        if _is_public_get_special(path):
            return True
    return False


def _bearer_or_cookie(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get("lotvue_token", "")


def install_auth_gate(app):
    from a2_extensions import _jwt_secret, JWT_ALG
    from jose import jwt, JWTError

    def _valid(token: str) -> bool:
        if not token:
            return False
        try:
            payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALG])
            return payload.get("type") == "access"
        except JWTError:
            return False

    def _no_cache(resp):
        # force fresh HTML so app-mode/browsers never show a stale "old style"
        ct = resp.headers.get("content-type", "")
        if "text/html" in ct:
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
        return resp

    @app.middleware("http")
    async def auth_gate_middleware(request: Request, call_next):
        path = request.url.path.rstrip("/") or "/"
        method = request.method.upper()
        if method == "OPTIONS" or _is_public(method, path):
            return _no_cache(await call_next(request))
        if _valid(_bearer_or_cookie(request)):
            return _no_cache(await call_next(request))
        # HTML navigation → login redirect; API → 401 JSON
        accept = request.headers.get("accept", "")
        if "text/html" in accept and method == "GET":
            return RedirectResponse(f"/login?next={path}", status_code=302)
        return JSONResponse(
            {"detail": "Sign in required. POST /api/auth/token for a JWT, "
                       "or sign in at /login."},
            status_code=401,
        )

    print("[Auth-Gate] sign-in required for all dealer tooling "
          f"({len(PUBLIC_EXACT)} exact + {len(PUBLIC_PREFIXES)} prefix public paths)")
