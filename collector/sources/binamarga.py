"""Ditjen Bina Marga (PUPR) — binamarga.pu.go.id/cctv-non-tol  (national non-toll roads)

Camera list : inline JS on the page:  var cctvLocations = [ {lat, lng, info, category, url}, ... ];
Categories  :
  its       -> /play-hls/{n}/its       -> direct HLS  its.binamarga.pu.go.id:8989/play/hls/CT-xx/index.m3u8
  ch        -> /play-hls/{n}/ch        -> direct HLS  apps.ptbtu.com:8078/hls-proxy/{n}/index.m3u8   (bridge cams)
  kemenhub  -> /play-hls/{n}/kemenhub  -> iframe hubnet.kemenhub.go.id/backend/api/show-cctv?id=..&token=..
                                          (token is short-lived -> resolve on demand, never cache)
  apace     -> https://apace-ai.com/APCxxxxx/   third-party AI-CCTV page (external)
Note        : binamarga play pages send CSP frame-ancestors 'self' -> cannot be iframed; we go
              one level deeper (to the m3u8 / hubnet URL) instead.
              ~15 kemenhub cameras have lat/lng = 0 -> approximate town-level coords below.
"""
from __future__ import annotations

import html as htmllib
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..common import EXTERNAL, HLS, RESOLVE, Camera, clean, client, valid_ll

SOURCE = "binamarga"
BASE = "https://binamarga.pu.go.id"
PAGE = f"{BASE}/cctv-non-tol"
# categories whose cameras are dropped from a live scrape when their stream link could not be
# resolved; collector.run then backfills them from the previous run or the seed
BACKFILL_CATEGORIES = {"its", "ch"}

CATEGORY_LABEL = {
    "its": "ITS (Bekasi–Karawang)",
    "ch": "Jembatan (CH)",
    "kemenhub": "Kemenhub",
    "apace": "Apace AI",
    "rcms": "RCMS",
    "kpbu": "KPBU",
}

# Town-level fallbacks for records published with lat/lng = 0 (geo_source="approx")
APPROX = [
    ("PURWOREJO", (-7.7136, 110.0085)),
    ("RANCAEKEK", (-6.9665, 107.8162)),
    ("PLERED", (-6.7047, 108.5097)),
    ("KLAMPOK", (-6.8760, 108.9950)),
    ("WANGON", (-7.5171, 109.0562)),
    ("KARANGNONGKO", (-7.8575, 110.1560)),
    ("WATES", (-7.8575, 110.1560)),
    ("CIKAMPEK", (-6.4245, 107.4548)),
]


def _approx(name: str):
    up = name.upper()
    for key, ll in APPROX:
        if key in up:
            return ll
    return None


def _num(m):
    return float(m.group(1)) if m else None


def parse_locations(page_html: str) -> list[dict]:
    m = re.search(r"var\s+cctvLocations\s*=\s*\[(.*?)\];", page_html, re.S)
    if not m:
        raise RuntimeError("Bina Marga: cctvLocations array not found (page layout changed?)")
    items = []
    for obj in re.findall(r"\{(.*?)\}", m.group(1), re.S):
        def s(key):
            mm = re.search(rf'\b{key}\s*:\s*"((?:[^"\\]|\\.)*)"', obj)
            return mm.group(1) if mm else ""
        items.append({
            "lat": _num(re.search(r"\blat\s*:\s*(-?[\d.]+)", obj)),
            "lng": _num(re.search(r"\blng\s*:\s*(-?[\d.]+)", obj)),
            "info": s("info"),
            "category": s("category"),
            "url": s("url"),
        })
    return items


def _split_info(info: str) -> tuple[str, str]:
    parts = [clean(p) for p in re.split(r"<br\s*/?>|\s/\s", htmllib.unescape(info)) if clean(p)]
    return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")


def _native_id(cat, target, stream, idx) -> str:
    """Stable per-camera key, identical whether built from live scrape or seed."""
    pats = {
        "its": (stream or "", r"/(CT-\d+)/"),
        "ch": (stream or "", r"/hls-proxy/(\d+)/"),
        "kemenhub": (target, r"/play-hls/(\d+)/"),
        "apace": (target, r"(APC\d+)"),
    }
    src, pat = pats.get(cat, (target, r"/play-hls/(\d+)/"))
    m = re.search(pat, src) or re.search(r"/play-hls/(\d+)/", target)
    return m.group(1) if m else str(idx)


def _make(idx, cat, lat, lng, info, target, stream=None) -> Camera:
    name, area = _split_info(info)
    ok = valid_ll(lat, lng)
    geo = "source"
    if not ok:
        ap = _approx(name + " " + area)
        if ap:
            lat, lng, ok, geo = ap[0], ap[1], True, "approx"
        else:
            geo = "none"
    if cat in ("its", "ch"):
        playback, page = HLS, (BASE + target if target.startswith("/") else target)
    elif cat == "kemenhub":
        playback, page = RESOLVE, BASE + target
    else:
        playback, page = EXTERNAL, target
    native_id = _native_id(cat, target, stream, idx)
    return Camera(
        id=f"{SOURCE}:{cat}-{native_id}",
        source=SOURCE,
        name=name,
        lat=float(lat) if ok else None,
        lon=float(lng) if ok else None,
        geo_source=geo,
        playback=playback,
        stream_url=stream,
        page_url=page,
        category=cat,
        area=area,
        city=area,
        meta={"category_label": CATEGORY_LABEL.get(cat, cat)},
    )


def _resolve_m3u8(c, play_path: str, tries: int = 3) -> str | None:
    """Fetch a /play-hls page and pull out its .m3u8. Retries with backoff — the site
    occasionally returns an empty/error page under parallel load."""
    for attempt in range(tries):
        try:
            r = c.get(BASE + play_path, headers={"Referer": PAGE})
            m = re.search(r"https?://[^\s'\"`<>\\]+?\.m3u8", r.text)
            if m:
                return m.group(0)
        except Exception:
            pass
        time.sleep(1.5 * (attempt + 1))
    return None


def fetch() -> list[Camera]:
    with client() as c:
        items = parse_locations(c.get(PAGE).text)
        play_paths = {}
        for it in items:
            url = it["url"]
            path = re.sub(r"^https?://[^/]+", "", url) if "binamarga.pu.go.id" in url else url
            it["target"] = path
            if it["category"] in ("its", "ch"):
                play_paths[path] = None
        with ThreadPoolExecutor(max_workers=3) as ex:
            for path, m3u8 in zip(play_paths, ex.map(lambda p: _resolve_m3u8(c, p), play_paths)):
                play_paths[path] = m3u8
    out = []
    for i, it in enumerate(items):
        stream = play_paths.get(it["target"])
        if it["category"] in ("its", "ch") and not stream:
            continue  # unresolved this round -> collector.run backfills from last run / seed
        out.append(_make(i, it["category"], it["lat"], it["lng"], it["info"], it["target"], stream))
    return out


def resolve_player(page_url: str) -> str:
    """Kemenhub: return a *fresh* hubnet show-cctv URL (contains short-lived token)."""
    with client() as c:
        t = c.get(page_url, headers={"Referer": PAGE}).text
    m = re.search(r"https?://hubnet\.kemenhub\.go\.id/[^\s'\"`<>\\]+", t)
    if not m:
        raise RuntimeError("Kemenhub player URL not found on play page")
    return htmllib.unescape(m.group(0))


def from_seed(path: Path) -> list[Camera]:
    out = []
    for i, line in enumerate(path.read_text().splitlines()):
        if not line.strip():
            continue
        cat, lat, lng, info, target = line.split("|")
        stream = target if target.endswith(".m3u8") else None
        out.append(_make(i, cat, float(lat), float(lng), info, target, stream))
    return out
