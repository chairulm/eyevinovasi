"""Lightweight stream health check: fetch each .m3u8 playlist (a few KB), never video segments.

Some servers (e.g. Medan's MediaMTX) start a camera only when it is first requested, so the
first hit can be slow or fail; an "offline" camera gets one more try after a short pause.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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


def probe_all(cams: list[Camera], workers: int = 10, on_progress=None) -> None:
    """Modest concurrency on purpose — these are public government servers.
    on_progress(done, total) is called as each camera finishes (for progress display)."""
    total = len(cams)
    with client(timeout=TIMEOUT) as c, ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(probe, c, cam): cam for cam in cams}
        for done, f in enumerate(as_completed(futs), 1):
            cam, st = futs[f], f.result()
            if st != "unknown":
                cam.status, cam.last_checked = st, now_iso()
            if on_progress:
                on_progress(done, total)
    on = sum(1 for x in cams if x.status == "online")
    off = sum(1 for x in cams if x.status == "offline")
    log.info("health: %d online, %d offline, %d n/a", on, off, len(cams) - on - off)
