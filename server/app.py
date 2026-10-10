"""CCTV Map server

  uvicorn server.app:app --host 0.0.0.0 --port 8080

Storage: Postgres when DATABASE_URL is set (see db/), else data/cameras.json.

API v1 (also served at the legacy /api/... paths for compatibility)
  GET  /api/v1/cameras                 GeoJSON; filters: ?source=medan,jogja &status=online
                                       &bbox=minLon,minLat,maxLon,maxLat &include_inactive=1
  GET  /api/v1/cameras/{id}            one camera + recent status changes
  GET  /api/v1/stats                   counts per source / status (+ last collector runs)
  GET  /api/v1/health                  backend + database status (for monitoring)
  GET  /api/v1/resolve/{id}            fresh player URL for tokenised sources (Kemenhub)
  POST /api/v1/refresh                 re-run collectors (+ health) in background
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
from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from collector.common import RESOLVE, UA
from collector.run import collect, health_only
from collector.sources import binamarga
from db.store import get_store

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


def _feature(c: dict) -> dict:
    geom = ({"type": "Point", "coordinates": [c["lon"], c["lat"]]}
            if c.get("lat") is not None and c.get("lon") is not None else None)
    return {"type": "Feature", "id": c["id"], "geometry": geom, "properties": c}


@api.get("/cameras")
def cameras(source: str | None = None, status: str | None = None, bbox: str | None = None,
            include_inactive: bool = False):
    box = None
    if bbox:
        try:
            box = [float(x) for x in bbox.split(",")]
            assert len(box) == 4
        except Exception:
            raise HTTPException(400, "bbox must be minLon,minLat,maxLon,maxLat")
    cams = get_store().list_cameras(_csv(source), _csv(status), box, include_inactive)
    body = {"type": "FeatureCollection", "count": len(cams), "features": [_feature(c) for c in cams]}
    return Response(json.dumps(body, ensure_ascii=False), media_type="application/geo+json",
                    headers={"Cache-Control": "no-cache"})


@api.get("/cameras/{cam_id:path}")
def camera(cam_id: str, history: int = Query(50, ge=0, le=1000)):
    store = get_store()
    cam = store.get_camera(cam_id)
    if not cam:
        raise HTTPException(404, "unknown camera")
    return cam | {"status_log": store.status_log(cam_id, history)}


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
async def refresh(health: bool = True):
    if _lock.locked():
        return JSONResponse({"status": "already running"}, status_code=409)
    asyncio.create_task(asyncio.to_thread(_locked, collect, None, False, health))
    return {"status": "started"}


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


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
