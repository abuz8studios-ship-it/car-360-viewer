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

import os
import re
import sqlite3
from pathlib import Path, PurePosixPath

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from jose import JWTError

# Exact-match public paths
PUBLIC_EXACT = {
    # The root route is the inventory dashboard, not a public marketing page.
    # Require a session there; login and registration remain public.
    "/login", "/register", "/pricing", "/api/health",
    "/api/register", "/api/login", "/api/plans", "/api/stripe/config",
    "/api/stripe/webhook",
    "/api/auth/token", "/api/auth/refresh", "/api/auth/logout",
    "/mobile", "/mobile/manifest.json", "/mobile/sw.js",
    "/favicon.ico", "/api/analytics/event", "/api/leads/submit",
    "/api/unsubscribe",
}

# Prefix-match public paths (any method — pages, assets, feeds)
PUBLIC_PREFIXES = (
    "/view/", "/embed/", "/viewer/", "/view3d/", "/splat3d/",
    "/showroom/",                 # public dealer storefront microsite
    "/static/",
    "/api/feeds/",             # feed handler requires its own configured secret
    "/api/leadmagnet/",           # buyer-facing conversion tools (trade-in/price/history/etc.)
    "/api/chat/",                 # buyer-facing AI assistant
    "/docs", "/openapi.json", "/redoc",
)

# Prefix-match public for GET ONLY (read-only viewer data; writes stay gated)
PUBLIC_GET_PREFIXES = (
    "/vdp/",                     # buyer-facing 3D vehicle detail page
    "/vehicles/",                # spin frames / ply / glb assets for the viewer
    "/api/viewer/",              # unified viewer manifest (read)
    "/api/hotspots/",            # viewer hotspot overlay (read)
    "/api/pano-hotspots/",       # interior-360 feature pins (read)
    "/api/backgrounds",          # viewer background thumbnails (read)
)


def _is_public_get_special(path: str) -> bool:
    # read-only viewer payload + showroom inventory JSON
    if path.startswith("/api/vehicles/") and path.endswith("/viewer"):
        return True
    return path.startswith("/api/showroom/") and path.endswith("/inventory")

# Methods that are public on specific paths (buyer lead submission)
PUBLIC_METHOD_PATHS = {
    ("POST", "/api/leads"),
}


_PUBLIC_STORAGE_DIRS = {
    # Buyer-facing derivatives only. Raw captures, imports, temporary video,
    # job status, database files, and other internal artifacts stay private.
    "spin", "spin_hd", "exterior", "interior", "panoramas", "gallery",
    "studio", "studio_spin", "splat", "3d", "hotspot_photos", "walkaround",
    "video", "videos", "commercial", "video_ads", "exports",
}
_PUBLIC_MEDIA_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".m4v", ".webm",
    ".ply", ".glb", ".gltf", ".bin",
}


def _is_public_storage(path: str) -> bool:
    """Allow anonymous reads of published media, never the full storage tree."""
    parts = path.split("/")
    if len(parts) < 4 or parts[1] != "storage":
        return False
    # Reject traversal, hidden files, separators and encoded path tricks before
    # routing to StaticFiles. Starlette decodes URL paths before this middleware.
    components = parts[2:]
    if any(part in {"", ".", ".."} or part.startswith(".") or "\\" in part
           or not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in components):
        return False
    if parts[2] == "_brand":
        return (len(parts) == 4 and PurePosixPath(parts[3].lower()).suffix in {
            ".jpg", ".jpeg", ".png", ".webp", ".gif",
        })
    if len(parts) == 4:
        filename = parts[3].lower()
        return filename == "thumbnail.jpg" or PurePosixPath(filename).suffix in {
            ".glb", ".gltf", ".ply",
        }
    if parts[3] not in _PUBLIC_STORAGE_DIRS:
        return False
    return PurePosixPath(parts[-1].lower()).suffix in _PUBLIC_MEDIA_SUFFIXES


def _storage_path_is_safe(path: str, storage_root: str | Path) -> bool:
    """Allow StaticFiles to serve only regular files beneath storage, never symlinks."""
    parts = path.split("/")
    if len(parts) < 4 or parts[1] != "storage":
        return False
    components = parts[2:]
    if any(part in {"", ".", ".."} or part.startswith(".") or "\\" in part
           or not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in components):
        return False
    try:
        root = Path(storage_root).resolve(strict=True)
        candidate = root
        for part in components:
            candidate = candidate / part
            if candidate.is_symlink():
                return False
        resolved = candidate.resolve(strict=True)
        return resolved.is_relative_to(root) and resolved.is_file()
    except (OSError, RuntimeError, ValueError):
        return False


def _published_vehicle_storage(path: str, db_path: str) -> bool:
    parts = path.split("/")
    if len(parts) < 4 or parts[2] == "_brand":
        return parts[2] == "_brand" if len(parts) > 2 else False
    try:
        with sqlite3.connect(db_path, timeout=2) as conn:
            return bool(conn.execute(
                "SELECT 1 FROM vehicles WHERE vin=? AND status='active'", (parts[2].upper(),)
            ).fetchone())
    except sqlite3.Error:
        return False


def _is_public(method: str, path: str) -> bool:
    if path in PUBLIC_EXACT:
        return True
    if path.startswith("/storage/") and _is_public_storage(path):
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


def _owns_private_storage(path: str, token: str, db_path: str, jwt_module, secret: str, algorithm: str) -> bool:
    """Authorize non-public storage files by the vehicle's authenticated tenant."""
    parts = path.split("/")
    if len(parts) < 4 or parts[1] != "storage":
        return False
    components = parts[2:]
    if any(part in {"", ".", ".."} or part.startswith(".") or "\\" in part
           or not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in components):
        return False
    vehicle_id = components[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", vehicle_id):
        return False
    try:
        payload = jwt_module.decode(token, secret, algorithms=[algorithm])
    except JWTError:
        return False
    if not isinstance(payload, dict):
        return False
    email = str(payload.get("sub", "")).strip().lower()
    if payload.get("type") != "access" or not email:
        return False
    owner_email = os.environ.get("LOTVUE_OWNER_EMAIL", "").strip().lower()
    try:
        with sqlite3.connect(db_path, timeout=2) as conn:
            if owner_email and email == owner_email:
                return bool(conn.execute(
                    "SELECT 1 FROM vehicles WHERE vin=?", (vehicle_id.upper(),)
                ).fetchone())
            row = conn.execute(
                "SELECT v.user_email FROM vehicles v "
                "JOIN users u ON LOWER(u.email)=LOWER(v.user_email) "
                "WHERE v.vin=? AND v.status='active'",
                (vehicle_id.upper(),),
            ).fetchone()
        return bool(row and str(row[0] or "").strip().lower() == email)
    except sqlite3.Error:
        return False


def _owns_vehicle(token: str, vehicle_id: str, db_path: str, jwt_module, secret: str, algorithm: str) -> bool:
    vehicle_id = (vehicle_id or "").upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,32}", vehicle_id):
        return False
    try:
        payload = jwt_module.decode(token, secret, algorithms=[algorithm])
    except JWTError:
        return False
    if not isinstance(payload, dict):
        return False
    email = str(payload.get("sub", "")).strip().lower()
    if payload.get("type") != "access" or not email:
        return False
    owner_email = os.environ.get("LOTVUE_OWNER_EMAIL", "").strip().lower()
    try:
        with sqlite3.connect(db_path, timeout=2) as conn:
            if owner_email and email == owner_email:
                return bool(conn.execute(
                    "SELECT 1 FROM vehicles WHERE vin=?", (vehicle_id,)
                ).fetchone())
            row = conn.execute(
                "SELECT v.user_email FROM vehicles v "
                "JOIN users u ON LOWER(u.email)=LOWER(v.user_email) "
                "WHERE v.vin=? AND v.status='active'",
                (vehicle_id,),
            ).fetchone()
        return bool(row and str(row[0] or "").strip().lower() == email)
    except sqlite3.Error:
        return False


def _private_vehicle_scope(path: str) -> str | None:
    parts = path.strip("/").split("/")
    if not parts or not parts[0]:
        return None
    if parts[0] in {"tag", "manage", "interior-studio"} and len(parts) == 2:
        return parts[1]
    if len(parts) < 3 or parts[0] != "api":
        return None
    if parts[1] == "studio" and len(parts) >= 4 and parts[2] == "vehicles":
        return parts[3]
    scoped_prefixes = {
        "vehicle", "vehicles", "render", "finalize", "finalize-status", "watermark",
        "dossier", "pricing", "intel", "valuation", "history",
    }
    if parts[1] in scoped_prefixes:
        if parts[1] == "vehicles" and parts[2] == "clear-all":
            return None  # Handler clears only the authenticated tenant's inventory.
        if parts[1] == "render" and parts[2] == "unlock":
            return None
        if parts[1] in {"pricing", "intel"} and parts[2] == "status":
            return None
        # Read-only viewer data has its own active-public-inventory check.
        if parts[1] == "vehicles" and len(parts) >= 4 and parts[3] == "viewer":
            return None
        return parts[2]
    if parts[1] == "marketplace" and len(parts) >= 4:
        # Listing packs and publication operations are tenant-scoped, not merely
        # authenticated. Catalog export is scoped internally to the actor.
        return None if parts[3] in {"catalog.csv", "status"} else parts[3]
    if parts[1] == "social" and len(parts) >= 4 and parts[2] == "postiz":
        return None if parts[3] == "status" else parts[3]
    if parts[1:3] == ["syndication", "push"] and len(parts) >= 4:
        return None if parts[3] == "status" else parts[3]
    if parts[1] == "import" and len(parts) == 4 and parts[2] == "reclassify":
        return parts[3]
    return None


def _private_job_scope(path: str) -> str | None:
    """Job IDs are not VINs; protect job-processing APIs through their vehicle row."""
    parts = path.strip("/").split("/")
    if len(parts) == 4 and parts[:2] == ["api", "process"]:
        return parts[2]
    return None


def _owns_job(token: str, job_id: str, db_path: str, jwt_module, secret: str, algorithm: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", job_id or ""):
        return False
    try:
        payload = jwt_module.decode(token, secret, algorithms=[algorithm])
    except JWTError:
        return False
    if not isinstance(payload, dict):
        return False
    email = str(payload.get("sub", "")).strip().lower()
    if payload.get("type") != "access" or not email:
        return False
    owner_email = os.environ.get("LOTVUE_OWNER_EMAIL", "").strip().lower()
    if owner_email and email == owner_email:
        return True
    try:
        with sqlite3.connect(db_path, timeout=2) as conn:
            row = conn.execute(
                "SELECT v.user_email FROM vehicles v "
                "JOIN users u ON LOWER(u.email)=LOWER(v.user_email) "
                "WHERE v.job_id=? AND v.status='active'",
                (job_id,),
            ).fetchone()
        return bool(row and str(row[0] or "").strip().lower() == email)
    except sqlite3.Error:
        return False


def _existing_vin_owned(token: str, vin: str, db_path: str, jwt_module, secret: str, algorithm: str) -> bool:
    """VIN decoder accepts new VINs; if one already exists, it may only mutate for its owner."""
    if not re.fullmatch(r"[A-Z0-9]{11,17}", vin or ""):
        return False
    try:
        payload = jwt_module.decode(token, secret, algorithms=[algorithm])
    except JWTError:
        return False
    if not isinstance(payload, dict):
        return False
    email = str(payload.get("sub", "")).strip().lower()
    if payload.get("type") != "access" or not email:
        return False
    owner_email = os.environ.get("LOTVUE_OWNER_EMAIL", "").strip().lower()
    if owner_email and email == owner_email:
        return True
    try:
        with sqlite3.connect(db_path, timeout=2) as conn:
            row = conn.execute(
                "SELECT v.user_email, v.status FROM vehicles v WHERE v.vin=?", (vin,)
            ).fetchone()
        if not row:
            return True
        return row[1] == "active" and str(row[0] or "").strip().lower() == email
    except sqlite3.Error:
        return False


def install_auth_gate(app):
    import a2_extensions
    from a2_extensions import JWT_ALG, _jwt_secret
    from jose import jwt
    from starlette.concurrency import run_in_threadpool

    def _db_path() -> str:
        return str(a2_extensions.DB_PATH)

    def _verified_email(token: str) -> str | None:
        """Return the account email behind a valid access token, else None."""
        if not token:
            return None
        try:
            payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALG])
            if not isinstance(payload, dict):
                return None
            email = str(payload.get("sub", "")).strip().lower()
            if payload.get("type") != "access" or not email:
                return None
            with sqlite3.connect(_db_path(), timeout=2) as conn:
                found = conn.execute(
                    "SELECT 1 FROM users WHERE LOWER(email)=?", (email,)
                ).fetchone()
            return email if found else None
        except (JWTError, sqlite3.Error):
            return None

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
        if method == "OPTIONS":
            return _no_cache(await call_next(request))
        if path.startswith("/storage/") and not await run_in_threadpool(
            _storage_path_is_safe, path, a2_extensions.STORAGE_DIR
        ):
            return JSONResponse({"detail": "File not found"}, status_code=404)
        is_public = _is_public(method, path)
        if (is_public and path.startswith("/storage/") and _is_public_storage(path)
                and not path.startswith("/storage/_brand/")
                and not await run_in_threadpool(_published_vehicle_storage, path, _db_path())):
            is_public = False
        if is_public:
            return _no_cache(await call_next(request))
        token = _bearer_or_cookie(request)
        # Verify the token once per request and publish the result, so route
        # handlers can reuse it instead of re-opening the database.
        verified_email = await run_in_threadpool(_verified_email, token)
        if verified_email:
            request.state.lotvue_user_email = verified_email
        if method == "GET" and re.fullmatch(r"/api/vin/[A-Za-z0-9]{11,17}/decode", path):
            if not verified_email:
                return JSONResponse({"detail": "Sign in required"}, status_code=401)
            vin = path.split("/")[3].upper()
            if not await run_in_threadpool(_existing_vin_owned, token, vin, _db_path(), jwt, _jwt_secret(), JWT_ALG):
                return JSONResponse({"detail": "Vehicle not found"}, status_code=404)
            return _no_cache(await call_next(request))
        job_id = _private_job_scope(path)
        if job_id:
            if not verified_email:
                return JSONResponse({"detail": "Sign in required"}, status_code=401)
            if not await run_in_threadpool(_owns_job, token, job_id, _db_path(), jwt, _jwt_secret(), JWT_ALG):
                return JSONResponse({"detail": "Job not found"}, status_code=404)
            return _no_cache(await call_next(request))
        vehicle_id = _private_vehicle_scope(path)
        if vehicle_id:
            if not verified_email:
                return JSONResponse({"detail": "Sign in required"}, status_code=401)
            if not await run_in_threadpool(_owns_vehicle, token, vehicle_id, _db_path(), jwt, _jwt_secret(), JWT_ALG):
                return JSONResponse({"detail": "Vehicle not found"}, status_code=404)
            return _no_cache(await call_next(request))
        if path.startswith("/storage/"):
            if not verified_email:
                return JSONResponse({"detail": "Sign in required"}, status_code=401)
            if not await run_in_threadpool(_owns_private_storage, path, token, _db_path(), jwt, _jwt_secret(), JWT_ALG):
                return JSONResponse({"detail": "File not found"}, status_code=404)
            return _no_cache(await call_next(request))
        if verified_email:
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
