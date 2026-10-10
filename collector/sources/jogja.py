"""Kota Yogyakarta — cctv.jogjakota.go.id

Camera list : GET https://cctv.jogjakota.go.id/home/getdata   (open JSON array)
Fields      : cctv_id, cctv_title, cctv_link (.m3u8), cctv_latitude, cctv_longitude,
              cctv_category, kecamatan_nama, kelurahan_nama, ...
Streams     : Wowza (cctvjss.jogjakota.go.id) HLS, no token, ~1280x960
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from ..common import HLS, Camera, clean, client, valid_ll

SOURCE = "jogja"
BASE = "https://cctv.jogjakota.go.id"
STREAM_HOST = "https://cctvjss.jogjakota.go.id"
LIST_URL = f"{BASE}/home/getdata"


def _category(stream_url: str) -> str:
    # first path segment of the stream is the operator group: atcs, bpbd, malioboro, rthp ...
    try:
        return urlparse(stream_url).path.strip("/").split("/")[0]
    except Exception:
        return ""


def _make(cid, title, lat, lon, cat_code, kec, kel, link) -> Camera:
    ok = valid_ll(lat, lon)
    return Camera(
        id=f"{SOURCE}:{cid}",
        source=SOURCE,
        name=clean(title).replace("_", " "),
        lat=float(lat) if ok else None,
        lon=float(lon) if ok else None,
        geo_source="source" if ok else "none",
        playback=HLS,
        stream_url=link,
        page_url=f"{BASE}/home",
        category=_category(link),
        area=clean(kec).title(),
        city="Kota Yogyakarta",
        meta={"category_code": str(cat_code), "kelurahan": clean(kel).title()},
    )


def fetch() -> list[Camera]:
    with client() as c:
        r = c.get(LIST_URL, headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{BASE}/home"})
        r.raise_for_status()
        rows = r.json()
    return [
        _make(
            x.get("cctv_id"), x.get("cctv_title"), x.get("cctv_latitude"), x.get("cctv_longitude"),
            x.get("cctv_category"), x.get("kecamatan_nama"), x.get("kelurahan_nama"), x.get("cctv_link"),
        )
        for x in rows
        if x.get("cctv_link")
    ]


def from_seed(path: Path) -> list[Camera]:
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        cid, title, lat, lon, cat, kec, spath = line.split("|")
        link = f"{STREAM_HOST}{spath}.stream/playlist.m3u8"
        out.append(_make(cid, title, lat, lon, cat, kec, "", link))
    return out
