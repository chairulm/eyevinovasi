"""Camera storage: Postgres when DATABASE_URL is set, otherwise the JSON export.

Both backends expose the same read API used by the server:
  list_cameras(source=None, status=None, bbox=None, include_inactive=False) -> list[dict]
  get_camera(cam_id) -> dict | None
  status_log(cam_id, limit) -> list[dict]
  stats() -> dict
  recent_runs(limit) -> list[dict]
and one write API used by the collector / health checker:
  persist(cams, runs=None)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from collector.common import Camera, load_cameras, write_outputs

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
log = logging.getLogger("store")

FIELDS = ["id", "source", "name", "lat", "lon", "geo_source", "playback", "stream_url",
          "page_url", "category", "area", "city", "meta", "status", "last_checked"]


def _bbox_ok(c: dict, bbox) -> bool:
    if not bbox:
        return True
    if c.get("lat") is None:
        return False
    w, s, e, n = bbox
    return s <= c["lat"] <= n and w <= c["lon"] <= e


def _with_ready(d: dict) -> dict:
    from restream.config import live_url
    d["analytics_ready"] = d.get("playback") == "hls" and bool(d.get("stream_url"))
    d["live_url"] = live_url(d)   # our re-stream (None when RESTREAM is off)
    return d


def _uptime(rows: list[dict], days: int, now: datetime) -> dict:
    """rows: status changes ascending, the first may predate the window.
    Returns online share of *known* time plus contiguous segments for a timeline bar."""
    from datetime import timedelta
    start = now - timedelta(days=days)
    segs = []
    for i, r in enumerate(rows):
        at = r["at"] if isinstance(r["at"], datetime) else datetime.fromisoformat(r["at"])
        nxt = rows[i + 1]["at"] if i + 1 < len(rows) else now
        nxt = nxt if isinstance(nxt, datetime) else datetime.fromisoformat(nxt)
        a, b = max(at, start), min(nxt, now)
        if b > a:
            segs.append({"from": a.isoformat(), "to": b.isoformat(), "status": r["status"]})
    known = sum((datetime.fromisoformat(x["to"]) - datetime.fromisoformat(x["from"])).total_seconds()
                for x in segs)
    on = sum((datetime.fromisoformat(x["to"]) - datetime.fromisoformat(x["from"])).total_seconds()
             for x in segs if x["status"] == "online")
    return {"days": days, "window_start": start.isoformat(), "known_hours": round(known / 3600, 1),
            "online_pct": round(100 * on / known, 1) if known else None, "segments": segs}


# --------------------------------------------------------------------------- JSON
class JsonStore:
    kind = "json"

    def __init__(self, data_dir: Path = DATA):
        self.path = data_dir / "cameras.json"

    def _all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [_with_ready({k: r.get(k) for k in FIELDS} | {"active": True})
                for r in json.loads(self.path.read_text())]

    def list_cameras(self, source=None, status=None, bbox=None, include_inactive=False):
        return [c for c in self._all()
                if (not source or c["source"] in source)
                and (not status or c["status"] in status)
                and _bbox_ok(c, bbox)]

    def get_camera(self, cam_id):
        return next((c for c in self._all() if c["id"] == cam_id), None)

    def status_log(self, cam_id, limit=100):
        return []

    def uptime(self, cam_id, days=7):
        return {"days": days, "online_pct": None, "known_hours": 0, "segments": []}

    def recent_runs(self, limit=10):
        return []

    def stats(self):
        cams = self._all()
        out = {"total": len(cams), "by_source": {}, "by_status": {}, "analytics_ready": 0}
        for c in cams:
            out["by_source"][c["source"]] = out["by_source"].get(c["source"], 0) + 1
            out["by_status"][c["status"]] = out["by_status"].get(c["status"], 0) + 1
            out["analytics_ready"] += int(c["analytics_ready"])
        return out

    def persist(self, cams: list[Camera], runs: Optional[dict] = None):
        write_outputs(cams, self.path.parent)

    def ping(self):
        return {"backend": "json", "ok": self.path.exists()}


# --------------------------------------------------------------------------- Postgres
class PgStore(JsonStore):
    kind = "postgres"

    def __init__(self, url: str, data_dir: Path = DATA):
        super().__init__(data_dir)
        from psycopg_pool import ConnectionPool
        from psycopg.rows import dict_row
        self.pool = ConnectionPool(url, min_size=1, max_size=5, open=True,
                                   kwargs={"row_factory": dict_row}, timeout=10)
        import atexit
        atexit.register(self.close)

    def close(self):
        try:
            self.pool.close(timeout=2)
        except Exception:
            pass

    # ---- reads
    def list_cameras(self, source=None, status=None, bbox=None, include_inactive=False):
        where, args = [], []
        if not include_inactive:
            where.append("active")
        if source:
            where.append("source = ANY(%s)"); args.append(list(source))
        if status:
            where.append("status = ANY(%s)"); args.append(list(status))
        if bbox:
            w, s, e, n = bbox
            where.append("lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s"); args += [s, n, w, e]
        sql = f"SELECT {', '.join(FIELDS)}, active FROM cameras"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY source, id"
        with self.pool.connection() as c:
            rows = c.execute(sql, args).fetchall()
        return [_with_ready(self._iso(r)) for r in rows]

    def get_camera(self, cam_id):
        with self.pool.connection() as c:
            r = c.execute(f"SELECT {', '.join(FIELDS)}, active, first_seen, last_seen "
                          "FROM cameras WHERE id = %s", (cam_id,)).fetchone()
        return _with_ready(self._iso(r)) if r else None

    def status_log(self, cam_id, limit=100):
        with self.pool.connection() as c:
            rows = c.execute("SELECT at, status FROM camera_status_log WHERE camera_id = %s "
                             "ORDER BY at DESC LIMIT %s", (cam_id, limit)).fetchall()
        return [self._iso(r) for r in rows]

    def uptime(self, cam_id, days=7):
        now = datetime.now(timezone.utc)
        with self.pool.connection() as c:
            rows = c.execute(
                """(SELECT at, status FROM camera_status_log
                     WHERE camera_id = %(id)s AND at < now() - make_interval(days => %(d)s)
                     ORDER BY at DESC LIMIT 1)
                   UNION ALL
                   (SELECT at, status FROM camera_status_log
                     WHERE camera_id = %(id)s AND at >= now() - make_interval(days => %(d)s))
                   ORDER BY at""", {"id": cam_id, "d": days}).fetchall()
        return _uptime(rows, days, now)

    def recent_runs(self, limit=10):
        with self.pool.connection() as c:
            rows = c.execute("SELECT * FROM collector_runs ORDER BY started_at DESC LIMIT %s",
                             (limit,)).fetchall()
        return [self._iso(r) for r in rows]

    def stats(self):
        with self.pool.connection() as c:
            rows = c.execute(
                "SELECT source, status, count(*) AS n, "
                "count(*) FILTER (WHERE playback = 'hls' AND stream_url IS NOT NULL) AS ready "
                "FROM cameras WHERE active GROUP BY source, status").fetchall()
        out = {"total": 0, "by_source": {}, "by_status": {}, "analytics_ready": 0}
        for r in rows:
            out["total"] += r["n"]
            out["by_source"][r["source"]] = out["by_source"].get(r["source"], 0) + r["n"]
            out["by_status"][r["status"]] = out["by_status"].get(r["status"], 0) + r["n"]
            out["analytics_ready"] += r["ready"]
        return out

    def ping(self):
        with self.pool.connection() as c:
            n = c.execute("SELECT count(*) AS n FROM cameras WHERE active").fetchone()["n"]
            v = c.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()["v"]
        return {"backend": "postgres", "ok": True, "cameras": n, "schema": v}

    # ---- writes
    def persist(self, cams: list[Camera], runs: Optional[dict] = None):
        """Upsert cameras, log status changes, soft-delete cameras a *live* scrape no longer
        lists, record collector runs. Also refreshes the JSON export (static fallback)."""
        super().persist(cams, runs)
        now = datetime.now(timezone.utc)
        runs = runs or {}
        with self.pool.connection() as c, c.transaction():
            prev = {r["id"]: r["status"] for r in
                    c.execute("SELECT id, status FROM cameras").fetchall()}
            added = {}
            for cam in cams:
                d = cam.to_dict()
                c.execute(
                    """
                    INSERT INTO cameras (id, source, name, lat, lon, geo_source, playback,
                        stream_url, page_url, category, area, city, meta, status, last_checked,
                        active, last_seen, updated_at)
                    VALUES (%(id)s, %(source)s, %(name)s, %(lat)s, %(lon)s, %(geo_source)s,
                        %(playback)s, %(stream_url)s, %(page_url)s, %(category)s, %(area)s,
                        %(city)s, %(meta)s, %(status)s, %(last_checked)s, true, %(now)s, %(now)s)
                    ON CONFLICT (id) DO UPDATE SET
                        source = EXCLUDED.source, name = EXCLUDED.name, lat = EXCLUDED.lat,
                        lon = EXCLUDED.lon, geo_source = EXCLUDED.geo_source,
                        playback = EXCLUDED.playback, stream_url = EXCLUDED.stream_url,
                        page_url = EXCLUDED.page_url, category = EXCLUDED.category,
                        area = EXCLUDED.area, city = EXCLUDED.city, meta = EXCLUDED.meta,
                        status = CASE WHEN EXCLUDED.status = 'unknown' THEN cameras.status
                                      ELSE EXCLUDED.status END,
                        last_checked = COALESCE(EXCLUDED.last_checked, cameras.last_checked),
                        active = true, last_seen = EXCLUDED.last_seen, updated_at = EXCLUDED.updated_at
                    """,
                    d | {"meta": json.dumps(d["meta"] or {}), "now": now},
                )
                if cam.id not in prev:
                    added[cam.source] = added.get(cam.source, 0) + 1
                if cam.status != "unknown" and prev.get(cam.id) != cam.status:
                    c.execute("INSERT INTO camera_status_log (camera_id, at, status) "
                              "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                              (cam.id, now, cam.status))
            removed = {}
            for src, info in runs.items():
                if info.get("mode") != "live":
                    continue  # only a successful live scrape may retire cameras
                ids = [x.id for x in cams if x.source == src]
                cur = c.execute("UPDATE cameras SET active = false, updated_at = %s "
                                "WHERE source = %s AND active AND NOT (id = ANY(%s))",
                                (now, src, ids))
                removed[src] = cur.rowcount
            for src, info in runs.items():
                c.execute("INSERT INTO collector_runs (source, started_at, finished_at, mode, "
                          "cameras, added, removed, error) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                          (src, info.get("started_at", now), now, info.get("mode"),
                           info.get("cameras"), added.get(src, 0), removed.get(src, 0),
                           info.get("error")))
        log.info("db: upserted %d, added %s, retired %s", len(cams), added or 0, removed or 0)

    @staticmethod
    def _iso(r: dict) -> dict:
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()}


_store = None


def get_store():
    """Process-wide store. Postgres if DATABASE_URL is set and reachable, else JSON."""
    global _store
    if _store is None:
        url = os.getenv("DATABASE_URL")
        if url:
            pg = None
            try:
                pg = PgStore(url)
                pg.ping()
                _store = pg
            except Exception as e:
                log.error("DATABASE_URL set but database unavailable (%s) — using JSON", e)
                if pg:
                    pg.close()
                _store = JsonStore()
        else:
            _store = JsonStore()
    return _store


def cameras_from_store(store) -> list[Camera]:
    """Current active cameras as Camera objects (for health checks)."""
    out = []
    for d in store.list_cameras():
        d = {k: d[k] for k in FIELDS}
        out.append(Camera(**d))
    return out
