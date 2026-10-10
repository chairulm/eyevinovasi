"""Grab still frames from HLS cameras on an interval -> data/frames/<camera>/<UTC>.jpg

Starter for the analytics layer. Decodes only 1 frame per camera per interval, so it is
cheap on both your box and the upstream servers.

  python -m analytics.frame_grabber --source jogja --interval 60
  python -m analytics.frame_grabber --ids medan:1 medan:25 --interval 15 --once

Needs ffmpeg on PATH.
"""
from __future__ import annotations

import argparse
import logging
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from collector.common import UA, load_cameras

ROOT = Path(__file__).resolve().parents[1]
FRAMES = ROOT / "data" / "frames"
REFERER = {
    "atcsdishub.medan.go.id": "https://atcsdishub.medan.go.id/streaming",
    "cctvjss.jogjakota.go.id": "https://cctv.jogjakota.go.id/home",
    "its.binamarga.pu.go.id": "https://binamarga.pu.go.id/cctv-non-tol",
    "apps.ptbtu.com": "https://binamarga.pu.go.id/cctv-non-tol",
}
log = logging.getLogger("grabber")


def safe(cid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", cid)


def grab(cam, timeout=25) -> Path | None:
    ref = REFERER.get(urlparse(cam.stream_url).hostname or "", "")
    out_dir = FRAMES / safe(cam.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".jpg")
    from restream.config import ENABLED, path_name
    if ENABLED:
        # read our local re-stream: the agency's server is pulled once for viewers + analytics
        src = ["-rtsp_transport", "tcp", "-timeout", "15000000",
               "-i", f"rtsp://127.0.0.1:8554/{path_name(cam.id)}"]
    else:
        src = ["-user_agent", UA, "-headers", f"Referer: {ref}\r\n",
               "-rw_timeout", "15000000", "-i", cam.stream_url]
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *src,
           "-frames:v", "1", "-q:v", "3", str(out)]
    try:
        subprocess.run(cmd, timeout=timeout, check=True, capture_output=True)
        return out
    except Exception as e:
        log.debug("%s: %s", cam.id, e)
        return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["medan", "jogja", "binamarga"])
    ap.add_argument("--ids", nargs="*")
    ap.add_argument("--interval", type=int, default=60, help="seconds between rounds")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    cams = [c for c in load_cameras(ROOT / "data" / "cameras.json") if c.analytics_ready]
    if a.source:
        cams = [c for c in cams if c.source == a.source]
    if a.ids:
        cams = [c for c in cams if c.id in set(a.ids)]
    log.info("grabbing from %d cameras every %ds", len(cams), a.interval)

    while True:
        t0 = time.time()
        with ThreadPoolExecutor(a.workers) as ex:
            ok = sum(1 for r in ex.map(grab, cams) if r)
        log.info("round done: %d/%d frames in %.0fs", ok, len(cams), time.time() - t0)
        if a.once:
            break
        time.sleep(max(0, a.interval - (time.time() - t0)))


if __name__ == "__main__":
    main()
