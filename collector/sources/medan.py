"""Kota Medan — atcsdishub.medan.go.id (Next.js app, "ITS Kota Medan")

Camera list : GET /api/v3/pv/ldevice?orderBy=id_lokasi&sortedBy=asc
              requires headers x-client-id / x-client-secret. These are PUBLIC values
              shipped in the site's own JS bundle (/_next/static/chunks/app/streaming/page-*.js),
              so we discover them at runtime instead of hard-coding (they may rotate).
              Override with env MEDAN_CLIENT_ID / MEDAN_CLIENT_SECRET if needed.
Fields      : id_lokasi, nama_lokasi, ket_lokasi, lat_lokasi, lon_lokasi, is_active,
              tb_device_lokasi[0].{url_proxy_hls, poster, nama_alias, ...}
Streams     : MediaMTX-style HLS  <url_proxy_hls>index.m3u8
              (the device record also carries private 10.x RTSP URLs — ignored, not reachable)
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from ..common import HLS, Camera, clean, client, valid_ll

SOURCE = "medan"
BASE = "https://atcsdishub.medan.go.id"
LIST_PATH = "/api/v3/pv/ldevice"


def _discover_credentials(c) -> tuple[str, str, str]:
    """Return (api_base, client_id, client_secret) from the streaming page bundle."""
    env_id, env_sec = os.getenv("MEDAN_CLIENT_ID"), os.getenv("MEDAN_CLIENT_SECRET")
    html = c.get(f"{BASE}/streaming").text
    chunks = re.findall(r'(/_next/static/chunks/app/streaming/page-[\w-]+\.js)', html)
    # fall back to scanning every chunk if the page chunk name pattern changes
    if not chunks:
        chunks = re.findall(r'(/_next/static/chunks/[\w/\-\.]+\.js)', html)
    for path in dict.fromkeys(chunks):
        js = c.get(BASE + path).text
        cid = re.search(r'"x-client-id"\s*:\s*"([^"]+)"', js)
        sec = re.search(r'"x-client-secret"\s*:\s*"([^"]+)"', js)
        if cid and sec:
            return f"{BASE}/api/v3/pv", env_id or cid.group(1), env_sec or sec.group(1)
    if env_id and env_sec:
        return f"{BASE}/api/v3/pv", env_id, env_sec
    raise RuntimeError("Medan: could not discover API client credentials from JS bundle")


def _make(lid, nama, ket, lat, lon, hls_base, extra=None) -> Camera:
    ok = valid_ll(lat, lon)
    stream = (hls_base.rstrip("/") + "/index.m3u8") if hls_base else None
    return Camera(
        id=f"{SOURCE}:{lid}",
        source=SOURCE,
        name=clean(nama),
        lat=float(lat) if ok else None,
        lon=float(lon) if ok else None,
        geo_source="source" if ok else "none",
        playback=HLS,
        stream_url=stream,
        page_url=hls_base,  # their own hls.js player page
        category="simpang" if clean(ket).lower().startswith("simpang") else
                 ("ruas" if clean(ket).lower().startswith("ruas") else "lainnya"),
        area=clean(ket),
        city="Kota Medan",
        meta=extra or {},
    )


def fetch() -> list[Camera]:
    with client() as c:
        api, cid, sec = _discover_credentials(c)
        r = c.get(
            f"{api}/ldevice",
            params={"orderBy": "id_lokasi", "sortedBy": "asc"},
            headers={"x-client-id": cid, "x-client-secret": sec, "Referer": f"{BASE}/streaming"},
        )
        r.raise_for_status()
        rows = r.json().get("data", [])
    out = []
    for x in rows:
        dev = (x.get("tb_device_lokasi") or [{}])[0]
        poster = dev.get("poster")
        out.append(_make(
            x.get("id_lokasi"), x.get("nama_lokasi"), x.get("ket_lokasi"),
            x.get("lat_lokasi"), x.get("lon_lokasi"), dev.get("url_proxy_hls"),
            extra={
                "is_active": x.get("is_active"),
                "alias": clean(dev.get("nama_alias")),
                "poster": poster,
                "tahun": x.get("tahun"),
            },
        ))
    return out


def from_seed(path: Path) -> list[Camera]:
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        lid, nama, ket, lat, lon, slug = line.split("|")
        out.append(_make(lid, nama, ket, lat, lon, f"{BASE}/stream/{slug}/"))
    return out
