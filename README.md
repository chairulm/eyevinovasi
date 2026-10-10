# Eyevinovasi — Indonesian public CCTV on one map

Pools public traffic CCTV from three portals, normalises them into one camera schema with
coordinates, and shows them on a map with live playback (up to 4 streams side-by-side).
Built as the base layer for later analytics.

| Source | Cameras | How the list is pulled | Stream | Analytics-ready |
|---|---|---|---|---|
| **Medan** — atcsdishub.medan.go.id | 187 | `GET /api/v3/pv/ldevice` with `x-client-id/secret` headers. Those values are public (they ship in the site's JS bundle), so the collector reads them at runtime | MediaMTX HLS `…/stream/<slug>/index.m3u8` | 187 |
| **Yogyakarta** — cctv.jogjakota.go.id | 146 | `GET /home/getdata` (open JSON) | Wowza HLS `cctvjss…/<group>/<name>.stream/playlist.m3u8` (1280×960) | 146 |
| **Bina Marga** — binamarga.pu.go.id/cctv-non-tol | 98 | inline JS `var cctvLocations=[…]` | ITS (10) and bridge/CH (37): direct HLS · Kemenhub (40): iframe with a short-lived token, resolved on demand · Apace (11): third-party page | 47 |
| **Total** | **431** | | | **380** |

Locations come from each portal's own lat/lon. 15 Kemenhub cameras are published with
`0,0`; they get approximate town-level coordinates (`geo_source: "approx"`, shown with a dashed outline on the map).

## Run

```bash
pip install -r requirements.txt
python -m collector.run --health        # live scrape + stream health → data/cameras.geojson
uvicorn server.app:app --port 8080      # open http://localhost:8080
```

You can also use Docker: `docker compose up -d`. Add `--profile analytics` to start the frame grabber too.

**Run it on a server in Indonesia.** Some upstream servers block foreign IPs or use non-standard ports (`:8989`, `:8078`), so hosting on a domestic VPS or on-prem is the safest choice.

If a live scrape fails (the site is down or its layout changed), the collector falls back to `data/seed/*.psv`.
These seed files are snapshots captured on 10 Oct 2026.

## Deploy on a VM (Ubuntu 24.04)

```bash
apt-get update && apt-get install -y git
git clone https://github.com/chairulm/eyevinovasi.git && cd eyevinovasi
sudo bash deploy/install.sh --auth     # --auth: site login (asked once); --domain/--email for HTTPS
sudo bash /opt/cctv-map/deploy/check.sh
```

Update later with:

```bash
cd ~/eyevinovasi && git pull && sudo bash deploy/install.sh
```

The installer is idempotent. It keeps your camera data in `/opt/cctv-map/data` and the site login.
The services are `cctv-map` (app) and `cctv-grabber` (frame capture for analytics, off by default), with nginx in front on port 80/443.

## Architecture

```
collector/            per-source scrapers → one Camera schema → cameras.json / .geojson
  sources/medan.py    API + runtime credential discovery
  sources/jogja.py    JSON endpoint
  sources/binamarga.py  inline-JS parse, play-page → m3u8 resolution, Kemenhub resolver
  health.py           fetches each .m3u8 playlist only (few KB), never video segments
server/app.py         FastAPI: /api/cameras, /api/resolve/{id}, /api/refresh,
                      /hls?u=…  HLS reverse proxy (CORS + Referer + mixed-content fix,
                      host allow-list, playlist URI rewriting)
web/                  Leaflet + markercluster + hls.js (vendored, no CDN needed)
analytics/            frame_grabber.py (ffmpeg, 1 frame/interval) → vehicle_count.py (YOLO example)
```

### Camera schema (`data/cameras.json`)

`id` (stable, e.g. `medan:25`, `jogja:14`, `binamarga:its-CT-01`) · `source` · `name` · `lat`/`lon` ·
`geo_source` (source|approx|none) · `playback` (hls|resolve|external) · `stream_url` · `page_url` ·
`category` · `area` · `city` · `status` (online|offline|unknown) · `last_checked` · `meta` · `analytics_ready`

The `id` stays the same between the live scrape and the seed, so it is safe to use as the key for analytics time series.

## Analytics path

1. `python -m analytics.frame_grabber --source jogja --interval 60` saves stills to `data/frames/<camera>/`.
2. `python -m analytics.vehicle_count` runs YOLO counts and writes `data/analytics/counts.csv`, with `lat`/`lon` attached to every row.
3. To scale up, replace step 1 with a MediaMTX or go2rtc re-stream and step 2 with DeepStream or Triton reading the re-streamed feeds.
   Results can go into TimescaleDB or PostGIS for congestion heatmaps and per-junction trends.

## Notes / caveats

- **Be polite to the sources.** Pull each stream once through the proxy or re-stream, keep the health-check worker count low, and refresh camera lists every few hours, not every minute.
- **Credentials and tokens are not stored.** The Medan client headers and the Kemenhub tokens are read fresh from the public pages at runtime.
- **Privacy (UU PDP 27/2022).** Keep analytics at the aggregate level (counts, flow, congestion). Don't add face recognition or plate identification without a legal basis.
- **Terms of use.** Check each portal's terms before any public or commercial redistribution.
- **Kemenhub players are iframes.** Bina Marga's own play pages block iframing (`frame-ancestors 'self'`), so the server resolves one level deeper to the hubnet player. Analytics on these cameras would need the underlying stream from the vidstream host.
- **Adding a source** means writing a new `collector/sources/<name>.py` with `fetch()` and `from_seed()`, registering it in `sources/__init__.py`, and adding its stream host to `ALLOWED_HOSTS` in `server/app.py`.

## Data & disclaimer

The camera feeds and metadata belong to the agencies that publish them: Dishub Kota Medan, Pemerintah Kota Yogyakarta, Ditjen Bina Marga PUPR and Kemenhub.
This project only indexes publicly accessible streams. It stores no credentials or tokens; the ones it needs are read from the public pages at runtime.
The files in `data/seed/` are public metadata snapshots, kept as an offline fallback.
