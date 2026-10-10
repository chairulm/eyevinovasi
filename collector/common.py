"""Shared camera schema + helpers for all source collectors."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

# playback types understood by the web UI / server
HLS = "hls"            # direct .m3u8 -> hls.js (optionally through /hls proxy)
RESOLVE = "resolve"    # tokenised; server resolves a fresh player URL on demand
EXTERNAL = "external"  # third-party player page, open in new tab / iframe


@dataclass
class Camera:
    id: str                      # "<source>:<native id>"  (stable key for analytics)
    source: str                  # medan | jogja | binamarga
    name: str
    lat: Optional[float]
    lon: Optional[float]
    playback: str                # HLS | RESOLVE | EXTERNAL
    stream_url: Optional[str] = None   # .m3u8 when playback == HLS
    page_url: Optional[str] = None     # player/landing page
    category: str = ""
    area: str = ""               # kecamatan / city / province
    city: str = ""
    geo_source: str = "source"   # source | approx | none
    status: str = "unknown"      # online | offline | unknown
    last_checked: Optional[str] = None
    meta: dict = field(default_factory=dict)

    @property
    def analytics_ready(self) -> bool:
        return self.playback == HLS and bool(self.stream_url)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["analytics_ready"] = self.analytics_ready
        return d


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def valid_ll(lat, lon) -> bool:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    # Indonesia bounding box (generous)
    return -11.5 <= lat <= 6.5 and 94.0 <= lon <= 141.5 and not (lat == 0 and lon == 0)


def client(**kw) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": UA, "Accept-Language": "id,en;q=0.8"},
        timeout=kw.pop("timeout", 20),
        follow_redirects=True,
        **kw,
    )


def to_geojson(cams: list[Camera]) -> dict:
    feats = []
    for c in cams:
        d = c.to_dict()
        geom = (
            {"type": "Point", "coordinates": [c.lon, c.lat]}
            if c.lat is not None and c.lon is not None
            else None
        )
        feats.append({"type": "Feature", "id": c.id, "geometry": geom, "properties": d})
    return {"type": "FeatureCollection", "generated_at": now_iso(), "features": feats}


def write_outputs(cams: list[Camera], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cameras.json").write_text(
        json.dumps([c.to_dict() for c in cams], ensure_ascii=False, indent=1)
    )
    gj = json.dumps(to_geojson(cams), ensure_ascii=False)
    (out_dir / "cameras.geojson").write_text(gj)
    # mirror for static hosting of web/ without the API server
    web = Path(__file__).resolve().parents[1] / "web"
    if web.is_dir():
        (web / "cameras.geojson").write_text(gj)


def load_cameras(path: Path) -> list[Camera]:
    rows = json.loads(path.read_text())
    out = []
    for r in rows:
        r.pop("analytics_ready", None)
        out.append(Camera(**r))
    return out
