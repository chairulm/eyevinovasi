"""CCTV Map server

  uvicorn server.app:app --host 0.0.0.0 --port 8080

Endpoints
  GET  /                        map UI (web/)
  GET  /api/cameras             GeoJSON of all cameras
  GET  /api/stats               counts per source / status
  GET  /api/resolve/{cam_id}    fresh player URL for tokenised sources (Kemenhub)
  GET  /hls?u=<url>             HLS reverse proxy (CORS + mixed-content + Referer fix),
                                restricted to known camera hosts, rewrites playlists
  POST /api/refresh             re-run collectors (+ health) in background
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
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from collector.common import RESOLVE, UA, load_cameras
from collector.health import probe_all
from collector.common import write_outputs
from collector.run import collect
from collector.sources import binamarga

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

app = FastAPI(title="CCTV Map")
_lock = threading.Lock()
_http: httpx.AsyncClient | None = None


def _cams():
    p = DATA / "cameras.json"
    if not p.exists():
        collect(seed_only=True)
    return load_cameras(p)


@app.on_event("startup")
async def _startup():
    global _http
    _http = httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": UA})
    if not (DATA / "cameras.json").exists():
        await asyncio.to_thread(collect, None, True, False)
    asyncio.create_task(_scheduler())


@app.on_event("shutdown")
async def _shutdown():
    if _http:
        await _http.aclose()


async def _scheduler():
    """Periodic list refresh + health probes. Runs in-process; swap for cron/k8s CronJob in prod."""
    loop = asyncio.get_running_loop()
    next_refresh = loop.time() + 5
    next_health = loop.time() + 60
    while True:
        await asyncio.sleep(30)
        t = loop.time()
        if REFRESH_HOURS > 0 and t >= next_refresh:
            next_refresh = t + REFRESH_HOURS * 3600
            await asyncio.to_thread(_refresh, True)
        elif HEALTH_MINUTES > 0 and t >= next_health:
            next_health = t + HEALTH_MINUTES * 60
            await asyncio.to_thread(_health_only)


def _refresh(health: bool):
    if not _lock.acquire(blocking=False):
        return
    try:
        collect(health=health)
    except Exception as e:
        log.exception("refresh failed: %s", e)
    finally:
        _lock.release()


def _health_only():
    if not _lock.acquire(blocking=False):
        return
    try:
        cams = load_cameras(DATA / "cameras.json")
        probe_all(cams)
        write_outputs(cams, DATA)
    finally:
        _lock.release()


@app.get("/api/cameras")
def cameras():
    p = DATA / "cameras.geojson"
    if not p.exists():
        _cams()
    return Response(p.read_text(), media_type="application/geo+json",
                    headers={"Cache-Control": "no-cache"})


@app.get("/api/stats")
def stats():
    cams = _cams()
    out = {"total": len(cams), "by_source": {}, "by_status": {}, "analytics_ready": 0}
    for c in cams:
        out["by_source"][c.source] = out["by_source"].get(c.source, 0) + 1
        out["by_status"][c.status] = out["by_status"].get(c.status, 0) + 1
        out["analytics_ready"] += int(c.analytics_ready)
    return out


@app.get("/api/resolve/{cam_id:path}")
def resolve(cam_id: str):
    cam = next((c for c in _cams() if c.id == cam_id), None)
    if not cam:
        raise HTTPException(404, "unknown camera")
    if cam.playback != RESOLVE:
        return {"url": cam.stream_url or cam.page_url, "kind": cam.playback}
    try:
        return {"url": binamarga.resolve_player(cam.page_url), "kind": "iframe"}
    except Exception as e:
        raise HTTPException(502, f"resolve failed: {e}")


@app.post("/api/refresh")
async def refresh(health: bool = True):
    if _lock.locked():
        return JSONResponse({"status": "already running"}, status_code=409)
    asyncio.create_task(asyncio.to_thread(_refresh, health))
    return {"status": "started"}


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
