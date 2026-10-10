"""Example analytic: count vehicles / people per grabbed frame -> data/analytics/counts.csv

Joins every count with the camera's lat/lon so it can go straight back onto the map
(heatmap / choropleth / time series per junction).

  pip install ultralytics      # YOLO; swap for DeepStream/Triton in production
  python -m analytics.vehicle_count --model yolo11n.pt
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from collector.common import load_cameras

ROOT = Path(__file__).resolve().parents[1]
FRAMES = ROOT / "data" / "frames"
OUT = ROOT / "data" / "analytics" / "counts.csv"
CLASSES = {"car", "motorcycle", "bus", "truck", "bicycle", "person"}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="yolo11n.pt")
    ap.add_argument("--conf", type=float, default=0.35)
    a = ap.parse_args(argv)

    from ultralytics import YOLO  # lazy import: optional dependency
    model = YOLO(a.model)
    cams = {c.id.replace(":", "_"): c for c in load_cameras(ROOT / "data" / "cameras.json")}

    OUT.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if OUT.exists():
        with OUT.open() as f:
            done = {(r["camera_id"], r["frame"]) for r in csv.DictReader(f)}

    new = not OUT.exists()
    with OUT.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["camera_id", "source", "lat", "lon", "frame", *sorted(CLASSES)])
        for cam_dir in sorted(FRAMES.glob("*")):
            cam = cams.get(cam_dir.name)
            if not cam:
                continue
            for img in sorted(cam_dir.glob("*.jpg")):
                if (cam.id, img.stem) in done:
                    continue
                res = model(str(img), conf=a.conf, verbose=False)[0]
                names = [res.names[int(c)] for c in res.boxes.cls.tolist()]
                cnt = {k: names.count(k) for k in sorted(CLASSES)}
                w.writerow([cam.id, cam.source, cam.lat, cam.lon, img.stem, *cnt.values()])
    print("appended ->", OUT)


if __name__ == "__main__":
    main()
