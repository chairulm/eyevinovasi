"""Lightweight stream health check: fetch each .m3u8 playlist (a few KB), never video segments.

Some servers (e.g. Medan's MediaMTX) start a camera only when it is first requested, so the
first hit can be slow or fail; an "offline" camera gets one more try after a short pause.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from .common import HLS, Camera, client, now_iso

log = logging.getLogger("health")
TIMEOUT = 15
RETRY_PAUSE = 3


def _once(c, url: str) -> bool:
    try:
        r = c.get(url, timeout=TIMEOUT)
        return r.status_code == 200 and "#EXTM3U" in r.text[:200]
    except Exception:
        return False


def probe(c, cam: Camera) -> str:
    if cam.playback != HLS or not cam.stream_url:
        return "unknown"
    if _once(c, cam.stream_url):
        return "online"
    time.sleep(RETRY_PAUSE)
    return "online" if _once(c, cam.stream_url) else "offline"


def probe_all(cams: list[Camera], workers: int = 10) -> None:
    """Modest concurrency on purpose — these are public government servers."""
    with client(timeout=TIMEOUT) as c, ThreadPoolExecutor(max_workers=workers) as ex:
        for cam, st in zip(cams, ex.map(lambda x: probe(c, x), cams)):
            if st != "unknown":
                cam.status, cam.last_checked = st, now_iso()
    on = sum(1 for x in cams if x.status == "online")
    off = sum(1 for x in cams if x.status == "offline")
    log.info("health: %d online, %d offline, %d n/a", on, off, len(cams) - on - off)
