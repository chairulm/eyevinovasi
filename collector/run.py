"""Collect cameras from all sources -> database (if DATABASE_URL) + data/cameras.json/.geojson

  python -m collector.run                 # live scrape all sources, seed fallback per source
  python -m collector.run --seed-only     # build from data/seed/*.psv (offline)
  python -m collector.run --only medan jogja
  python -m collector.run --health        # also probe every HLS stream (online/offline)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .common import load_cameras
from .health import probe_all
from .sources import REGISTRY

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SEED = DATA / "seed"
log = logging.getLogger("collector")


def collect(only=None, seed_only=False, health=False) -> list:
    prev = {}
    if (DATA / "cameras.json").exists():
        try:
            prev = {c.id: c for c in load_cameras(DATA / "cameras.json")}
        except Exception:
            prev = {}

    cams, runs = [], {}
    for name, mod in REGISTRY.items():
        if only and name not in only:
            cams += [c for c in prev.values() if c.source == name]
            continue
        run = runs[name] = {"started_at": datetime.now(timezone.utc), "mode": "seed", "error": None}
        got, how = [], "seed"
        if not seed_only:
            try:
                got, how = mod.fetch(), "live"
            except Exception as e:  # network/layout change -> keep going with seed
                log.warning("%s live fetch failed: %s — using seed", name, e)
                run["error"] = f"{type(e).__name__}: {e}"[:500]
        seed = SEED / f"{name}.psv"
        if not got:
            got = mod.from_seed(seed) if seed.exists() else []
        elif getattr(mod, "BACKFILL_CATEGORIES", None):
            # live scrape skipped some cameras it couldn't resolve this round -> reuse last known
            have = {c.id for c in got}
            pool = {c.id: c for c in (mod.from_seed(seed) if seed.exists() else [])}
            pool.update({k: v for k, v in prev.items() if v.source == name})
            back = [c for c in pool.values()
                    if c.category in mod.BACKFILL_CATEGORIES and c.id not in have and c.stream_url]
            if back:
                log.info("%s: backfilled %d unresolved cameras from last run/seed", name, len(back))
                got += back
        # carry over last health status so a refresh doesn't blank the map
        for c in got:
            if c.id in prev and c.status == "unknown":
                c.status, c.last_checked = prev[c.id].status, prev[c.id].last_checked
        log.info("%-10s %4d cameras (%s)", name, len(got), how)
        run.update(mode=how if got else "none", cameras=len(got))
        cams += got

    # de-duplicate on id (keep first)
    seen, uniq = set(), []
    for c in cams:
        if c.id not in seen:
            seen.add(c.id)
            uniq.append(c)

    if health:
        probe_all(uniq)
    from db.store import get_store  # late import: db.store imports collector.common
    store = get_store()
    store.persist(uniq, runs)
    log.info("saved %d cameras (%s store)", len(uniq), store.kind)
    return uniq


def health_only() -> list:
    """Re-probe the current camera list without re-scraping the sources."""
    from db.store import cameras_from_store, get_store
    store = get_store()
    cams = cameras_from_store(store) or (
        load_cameras(DATA / "cameras.json") if (DATA / "cameras.json").exists() else [])
    probe_all(cams)
    store.persist(cams)
    return cams


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-only", action="store_true")
    ap.add_argument("--health", action="store_true")
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    collect(a.only, a.seed_only, a.health)


if __name__ == "__main__":
    sys.exit(main())
