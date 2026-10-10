"""CCTV Map server

  uvicorn server.app:app --host 0.0.0.0 --port 8080

Storage: Postgres when DATABASE_URL is set (see db/), else data/cameras.json.

API v1 (also served at the legacy /api/... paths for compatibility)
  GET  /api/v1/cameras                 GeoJSON; filters: ?source=medan,jogja &status=online
                                       &bbox=minLon,minLat,maxLon,maxLat &include_inactive=1
  GET  /api/v1/cameras/{id}            one camera + recent status changes + re-stream state
  GET  /api/v1/cameras/{id}/uptime     online share + timeline segments (?days=7)
  GET  /api/v1/stats                   counts per source / status (+ last collector runs)
  GET  /api/v1/health                  backend + database status (for monitoring)
  GET  /api/v1/resolve/{id}            fresh player URL for tokenised sources (Kemenhub)
  POST /api/v1/refresh                 re-run collectors (+ health) in background   [admin]
  POST /api/v1/auth/login | logout     session cookie;  GET /api/v1/auth/me, /api/v1/auth/check
  /api/v1/admin/...                    admin API (server/admin.py)                   [admin]
Auth (Postgres backend only): every page and API needs a session except /login and
/api/v1/health; non-GET API calls must send the header  X-Eye: 1  (CSRF guard).
Other
  GET  /hls?u=<url>                    HLS reverse proxy (host allow-list, playlist rewrite)
  GET  /                               map UI (web/)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from collector.common import RESOLVE, UA
from collector.run import collect, health_only
from collector.sources import binamarga
from db.store import get_store

from . import admin, auth
from .admin import client_ip

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
WEB = ROOT / "web"
log = logging.getLogger("server")

REFRESH_HOURS = float(os.getenv("REFRESH_HOURS", "12"))     # re-scrape camera lists
HEALTH_MINUTES = float(os.getenv("HEALTH_MINUTES", "30"))   # re-probe stream health

# Only these upstreams may be proxied (prevents the proxy being an open relay)
ALLOWED_HOSTS = {
    "atcsdishub.medan.go.id": "https://atcsdishub.medan.go.id/streaming",
    "cctvjss.jogjakota.go.id": "https://cctv.jogjakota.go.id/home",
    "its.binamarga.pu.go.id": "https://binamarga.pu.go.id/cctv-non-tol",
    "apps.ptbtu.com": "https://binamarga.pu.go.id/cctv-non-tol",
}

app = FastAPI(title="Eyevinovasi CCTV Map", version="1.0")
api = APIRouter()
_lock = threading.Lock()
_http: httpx.AsyncClient | None = None

# ---------------------------------------------------------------- auth gate
PUBLIC = {"/login", "/login.html", "/favicon.ico",
          "/api/v1/auth/login", "/api/v1/auth/check", "/api/v1/health",
          "/api/auth/login", "/api/auth/check", "/api/health"}
PUBLIC_PREFIX = ("/vendor/",)
ADMIN_PREFIX = ("/api/v1/admin/", "/api/admin/")
ADMIN_PATHS = {"/admin", "/admin.html", "/admin.js", "/api/v1/refresh", "/api/refresh"}


def _trusted_local(request: Request) -> bool:
    """GETs made on the server itself (check.sh, curl on the VM) — not via nginx, which
    always adds X-Forwarded-For/X-Real-IP."""
    host = request.client.host if request.client else ""
    return (request.method in ("GET", "HEAD") and host in ("127.0.0.1", "::1")
            and "x-forwarded-for" not in request.headers and "x-real-ip" not in request.headers)


def _deny(request: Request, code: int, msg: str):
    p = request.url.path
    if p.startswith(("/api", "/hls", "/live")) or p.endswith(".js"):
        return JSONResponse({"detail": msg}, status_code=code)
    if code == 403:
        return RedirectResponse("/", status_code=302)          # signed in, but not an admin
    nxt = p + (("?" + request.url.query) if request.url.query else "")
    return RedirectResponse("/login?next=" + quote(nxt, safe=""), status_code=302)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    request.state.user = None
    if not auth.enabled():
        return await call_next(request)                  # no database configured: no accounts
    p = request.url.path
    if p.startswith("/api") and request.method not in ("GET", "HEAD", "OPTIONS") \
            and request.headers.get("x-eye") != "1":
        return JSONResponse({"detail": "missing X-Eye header"}, status_code=403)
    if p in PUBLIC or p.startswith(PUBLIC_PREFIX):
        return await call_next(request)
    try:
        user = await asyncio.to_thread(auth.session_user, request.cookies.get(auth.COOKIE))
    except auth.AuthError:
        user = None
        if not _trusted_local(request):
            return JSONResponse({"detail": "database unavailable"}, status_code=503)
    if user is None:
        if _trusted_local(request):
            user = {"id": None, "username": "local", "role": "admin"}
        else:
            return _deny(request, 401, "login required")
    request.state.user = user
    if (p.startswith(ADMIN_PREFIX) or p in ADMIN_PATHS) and user["role"] != "admin":
        return _deny(request, 403, "admin only")
    return await call_next(request)


@app.on_event("startup")
async def _startup():
    global _http
    _http = httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": UA})
    store = await asyncio.to_thread(get_store)
    if not await asyncio.to_thread(store.list_cameras):
        await asyncio.to_thread(collect, None, True, False)   # first run: seed data
    asyncio.create_task(_scheduler())


@app.on_event("shutdown")
async def _shutdown():
    if _http:
        await _http.aclose()


async def _scheduler():
    """Periodic list refresh + health probes. Runs in-process (single uvicorn worker)."""
    loop = asyncio.get_running_loop()
    next_refresh = loop.time() + 5
    next_health = loop.time() + 60
    while True:
        await asyncio.sleep(30)
        t = loop.time()
        if REFRESH_HOURS > 0 and t >= next_refresh:
            next_refresh = t + REFRESH_HOURS * 3600
            await asyncio.to_thread(_locked, collect, None, False, True)
        elif HEALTH_MINUTES > 0 and t >= next_health:
            next_health = t + HEALTH_MINUTES * 60
            await asyncio.to_thread(_locked, health_only)


def _locked(fn, *args):
    if not _lock.acquire(blocking=False):
        return
    try:
        fn(*args)
    except Exception as e:
        log.exception("background job failed: %s", e)
    finally:
        _lock.release()


def _csv(v: str | None) -> list[str] | None:
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def _is_admin(request: Request) -> bool:
    return (getattr(request.state, "user", None) or {}).get("role") == "admin"


def _feature(c: dict) -> dict:
    geom = ({"type": "Point", "coordinates": [c["lon"], c["lat"]]}
            if c.get("lat") is not None and c.get("lon") is not None else None)
    return {"type": "Feature", "id": c["id"], "geometry": geom, "properties": c}


@api.get("/cameras")
def cameras(request: Request, source: str | None = None, status: str | None = None,
            bbox: str | None = None, include_inactive: bool = False, include_hidden: bool = False):
    box = None
    if bbox:
        try:
            box = [float(x) for x in bbox.split(",")]
            assert len(box) == 4
        except Exception:
            raise HTTPException(400, "bbox must be minLon,minLat,maxLon,maxLat")
    is_admin = _is_admin(request)
    cams = get_store().list_cameras(_csv(source), _csv(status), box, include_inactive,
                                    include_hidden=include_hidden and is_admin)
    body = {"type": "FeatureCollection", "count": len(cams), "features": [_feature(c) for c in cams]}
    return Response(json.dumps(body, ensure_ascii=False), media_type="application/geo+json",
                    headers={"Cache-Control": "no-cache"})


@api.get("/cameras/{cam_id:path}/uptime")
def camera_uptime(cam_id: str, days: int = Query(7, ge=1, le=90)):
    store = get_store()
    if not store.get_camera(cam_id):
        raise HTTPException(404, "unknown camera")
    return store.uptime(cam_id, days)


def _restream_info(cam_id: str) -> dict | None:
    """Live state of our re-stream for this camera (MediaMTX API, local only)."""
    from restream.config import ENABLED, path_name
    if not ENABLED:
        return None
    try:
        r = httpx.get(f"http://127.0.0.1:9997/v3/paths/get/{path_name(cam_id)}", timeout=1.5)
        if r.status_code == 404:
            return {"active": False, "viewers": 0}
        d = r.json()
        return {"active": bool(d.get("ready")), "viewers": len(d.get("readers") or [])}
    except Exception:
        return {"active": None, "viewers": None}


@api.get("/cameras/{cam_id:path}")
def camera(request: Request, cam_id: str, history: int = Query(50, ge=0, le=1000)):
    store = get_store()
    cam = store.get_camera(cam_id)
    if not cam or (cam.get("hidden") and not _is_admin(request)):
        raise HTTPException(404, "unknown camera")
    return cam | {"status_log": store.status_log(cam_id, history), "restream": _restream_info(cam_id)}


@api.get("/stats")
def stats():
    store = get_store()
    return store.stats() | {"backend": store.kind, "last_runs": store.recent_runs(6)}


@api.get("/health")
def health():
    try:
        return get_store().ping()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=503)


@api.get("/resolve/{cam_id:path}")
def resolve(cam_id: str):
    cam = get_store().get_camera(cam_id)
    if not cam:
        raise HTTPException(404, "unknown camera")
    if cam["playback"] != RESOLVE:
        return {"url": cam["stream_url"] or cam["page_url"], "kind": cam["playback"]}
    try:
        return {"url": binamarga.resolve_player(cam["page_url"]), "kind": "iframe"}
    except Exception as e:
        raise HTTPException(502, f"resolve failed: {e}")


@api.post("/refresh")
async def refresh(request: Request, health: bool = True):
    if _lock.locked():
        return JSONResponse({"status": "already running"}, status_code=409)
    if auth.enabled():
        u = request.state.user or {}
        try:
            await asyncio.to_thread(auth.audit, u.get("username"), "refresh", None, {"health": health},
                                    client_ip(request))
        except Exception:
            pass
    asyncio.create_task(asyncio.to_thread(_locked, collect, None, False, health))
    return {"status": "started"}


# ---------------------------------------------------------------- auth endpoints
class LoginBody(BaseModel):
    username: str
    password: str


def _secure(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


@api.post("/auth/login")
def login(body: LoginBody, request: Request):
    if not auth.enabled():
        raise HTTPException(503, "accounts need the database backend")
    try:
        token, user = auth.login(body.username, body.password, client_ip(request),
                                 request.headers.get("user-agent", ""))
    except auth.AuthError as e:
        code = 429 if "too many" in str(e) else 503 if "database" in str(e) else 401
        return JSONResponse({"detail": str(e)}, status_code=code)
    resp = JSONResponse(user)
    resp.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True,
                    samesite="lax", secure=_secure(request), path="/")
    return resp


@api.post("/auth/logout")
def logout(request: Request):
    if auth.enabled():
        u = request.state.user or {}
        auth.logout(request.cookies.get(auth.COOKIE))
        auth.audit(u.get("username"), "logout", ip=client_ip(request))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@api.get("/auth/me")
def me(request: Request):
    u = request.state.user
    if not auth.enabled():
        return {"auth": False, "username": None, "role": "admin"}
    return {"auth": True} | (u or {})


@api.get("/auth/check")
def check(request: Request):
    """For nginx auth_request (protects /live/): 204 with a valid session, else 401."""
    if not auth.enabled():
        return Response(status_code=204)
    try:
        ok = auth.session_user(request.cookies.get(auth.COOKIE))
    except auth.AuthError:
        ok = None
    return Response(status_code=204 if ok else 401)


api.include_router(admin.router, prefix="/admin")
app.include_router(api, prefix="/api/v1")
app.include_router(api, prefix="/api", include_in_schema=False)   # legacy paths


# ---------------------------------------------------------------- HLS proxy
_URI_ATTR = re.compile(r'URI="([^"]+)"')


def _proxied(u: str) -> str:
    return "/hls?u=" + quote(u, safe="")


def _rewrite_playlist(text: str, base: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
        elif s.startswith("#"):
            out.append(_URI_ATTR.sub(lambda m: f'URI="{_proxied(urljoin(base, m.group(1)))}"', line))
        else:
            out.append(_proxied(urljoin(base, s)))
    return "\n".join(out) + "\n"


@app.get("/hls")
async def hls(u: str = Query(..., description="absolute upstream URL")):
    host = urlparse(u).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise HTTPException(403, "host not allowed")
    headers = {"Referer": ALLOWED_HOSTS[host], "Origin": f"https://{urlparse(ALLOWED_HOSTS[host]).hostname}"}
    req = _http.build_request("GET", u, headers=headers)
    try:
        up = await _http.send(req, stream=True)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"upstream error: {e.__class__.__name__}")
    ctype = up.headers.get("content-type", "")
    is_playlist = "mpegurl" in ctype.lower() or urlparse(u).path.endswith(".m3u8")
    if is_playlist:
        body = (await up.aread()).decode("utf-8", "replace")
        await up.aclose()
        if up.status_code != 200:
            raise HTTPException(up.status_code, "upstream playlist error")
        return Response(_rewrite_playlist(body, str(up.url)),
                        media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})

    path = urlparse(u).path
    if path.endswith(".ts"):
        ctype = "video/mp2t"
    elif path.endswith((".m4s", ".mp4")):
        ctype = "video/mp4"

    async def body_iter():
        try:
            async for chunk in up.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await up.aclose()

    return StreamingResponse(body_iter(), status_code=up.status_code,
                             media_type=ctype or "video/mp2t",
                             headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})


@app.get("/live/{rest:path}")
async def live_passthrough(rest: str):
    """Re-stream output. In production nginx serves /live/ straight from MediaMTX;
    this route only matters when running the app without nginx (dev/tests)."""
    try:
        up = await _http.send(_http.build_request("GET", f"http://127.0.0.1:8888/{rest}"), stream=True)
    except httpx.HTTPError:
        raise HTTPException(502, "re-stream server not reachable")

    async def body():
        try:
            async for chunk in up.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await up.aclose()

    return StreamingResponse(body(), status_code=up.status_code,
                             media_type=up.headers.get("content-type", "application/octet-stream"),
                             headers={"Cache-Control": "no-cache"})


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(WEB / "login.html", headers={"Cache-Control": "no-cache"})


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(WEB / "admin.html", headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
