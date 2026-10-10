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


def collect(only=None, seed_only=False, health=False, progress=None) -> list:
    """progress(phase, done, total) — optional callback for a progress display."""
    prev = {}
    if (DATA / "cameras.json").exists():
        try:
            prev = {c.id: c for c in load_cameras(DATA / "cameras.json")}
        except Exception:
            prev = {}

    cams, runs = [], {}
    n_src = len(REGISTRY)
    if progress:
        progress("Collecting camera lists", 0, n_src)
    for i_src, (name, mod) in enumerate(REGISTRY.items(), 1):
        if only and name not in only:
            cams += [c for c in prev.values() if c.source == name]
            if progress:
                progress("Collecting camera lists", i_src, n_src)
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
        if progress:
            progress("Collecting camera lists", i_src, n_src)

    # de-duplicate on id (keep first)
    seen, uniq = set(), []
    for c in cams:
        if c.id not in seen:
            seen.add(c.id)
            uniq.append(c)

    if health:
        probe_all(uniq, on_progress=(lambda d, t: progress("Checking cameras", d, t)) if progress else None)
    from db.store import get_store  # late import: db.store imports collector.common
    store = get_store()
    store.persist(uniq, runs)
    log.info("saved %d cameras (%s store)", len(uniq), store.kind)
    from restream import config as restream
    if restream.ENABLED:
        restream.write(store.list_cameras())
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
    ap.add_argument("--progress", action="store_true",
                    help="show only a percentage per phase instead of log lines (used by the installer)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if a.progress else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cams = collect(a.only, a.seed_only, a.health, progress=_Progress() if a.progress else None)
    if a.progress:
        on = sum(1 for c in cams if c.status == "online")
        off = sum(1 for c in cams if c.status == "offline")
        print(f"  {len(cams)} cameras: {on} online, {off} offline, {len(cams) - on - off} not checkable")


class _Progress:
    """One updating line per phase on a terminal; 10% steps when output is a log/pipe."""

    def __init__(self):
        self.tty = sys.stdout.isatty()
        self.phase, self.last = None, -1

    def __call__(self, phase, done, total):
        pct = int(100 * done / total) if total else 100
        if phase != self.phase:
            if self.phase and self.tty:
                print()
            self.phase, self.last = phase, -1
        if self.tty:
            print(f"\r  {phase}: {pct:3d}% ({done}/{total})", end="", flush=True)
            if done >= total:
                print()
                self.phase = None
        elif pct // 10 != self.last // 10 or done >= total:
            print(f"  {phase}: {pct}% ({done}/{total})", flush=True)
        self.last = pct


if __name__ == "__main__":
    sys.exit(main())
