#!/usr/bin/env python3
"""
Combines the regions built by build_tiles.py into map-data.json: the one
file the game reads first. It says where every archive part lives and which
area each region covers (from Geofabrik's .poly outlines), so the game knows
when to use the archive and when to fall back to Overpass.

Usage:
  python make_manifest.py <dir with *.summary.json, *.pmtiles, *.poly>
         --base-url https://github.com/<owner>/<repo>/releases/download/<tag>/
         --tag <tag> -o map-data.json
"""

import argparse
import glob
import json
import os
from datetime import datetime, timezone

MANIFEST_VERSION = 1


def read_poly(path):
    """Geofabrik .poly: sections of "lon lat" lines; a section name starting with ! is a hole."""
    rings = []
    with open(path) as f:
        lines = [l.strip() for l in f]
    i = 1  # first line is the polygon's name
    while i < len(lines):
        name = lines[i]
        i += 1
        if name == "END" or name == "":
            continue
        pts = []
        while i < len(lines) and lines[i] != "END":
            parts = lines[i].split()
            if len(parts) >= 2:
                pts.append((float(parts[1]), float(parts[0])))  # (lat, lon)
            i += 1
        i += 1
        if len(pts) >= 3:
            rings.append({"hole": name.startswith("!"), "pts": pts})
    return rings


def simplify(pts, tolerance):
    """Douglas-Peucker in degrees (iterative)."""
    if len(pts) < 4:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ay, ax), (by, bx) = pts[a], pts[b]
        dx, dy = bx - ax, by - ay
        norm = (dx * dx + dy * dy) ** 0.5
        best, index = -1.0, -1
        for i in range(a + 1, b):
            py, px = pts[i]
            if norm == 0:
                d = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > best:
                best, index = d, i
        if best > tolerance and index > 0:
            keep[index] = True
            stack.append((a, index))
            stack.append((index, b))
    return [p for p, k in zip(pts, keep) if k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--tolerance", type=float, default=0.002, help="outline simplification, degrees")
    ap.add_argument("-o", "--output", default="map-data.json")
    args = ap.parse_args()

    regions = []
    zoom = None
    tile_format = None
    for summary_path in sorted(glob.glob(os.path.join(args.dir, "*.summary.json"))):
        with open(summary_path) as f:
            s = json.load(f)
        if zoom is None:
            zoom, tile_format = s["zoom"], s["format_version"]
        if s["zoom"] != zoom or s["format_version"] != tile_format:
            raise SystemExit(f"{summary_path}: zoom/format differ from the other regions")
        region = {
            "name": s["region"],
            "osm_timestamp": s["osm_timestamp"],
            "parts": [
                {k: p[k] for k in ("file", "bytes", "tiles", "min_tile", "max_tile", "bounds")}
                for p in s["parts"]
            ],
        }
        poly_path = os.path.join(args.dir, s["region"] + ".poly")
        if os.path.exists(poly_path):
            rings = read_poly(poly_path)
            region["outline"] = [
                {"hole": r["hole"], "ring": [round(v, 5) for p in simplify(r["pts"], args.tolerance) for v in p]}
                for r in rings
            ]
            lats = [p[0] for r in rings if not r["hole"] for p in r["pts"]]
            lons = [p[1] for r in rings if not r["hole"] for p in r["pts"]]
            region["bbox"] = [round(min(lats), 5), round(min(lons), 5), round(max(lats), 5), round(max(lons), 5)]
        else:
            b = s["bounds"]
            region["bbox"] = [round(v, 5) for v in b]
            region["outline"] = [{"hole": False, "ring": [b[0], b[1], b[0], b[3], b[2], b[3], b[2], b[1]]}]
        regions.append(region)

    if not regions:
        raise SystemExit("no *.summary.json found")

    manifest = {
        "format": "carryolia-map-manifest",
        "version": MANIFEST_VERSION,
        "tile_format_version": tile_format,
        "zoom": zoom,
        "tag": args.tag,
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_url": args.base_url if args.base_url.endswith("/") else args.base_url + "/",
        "attribution": "Map data © OpenStreetMap contributors, ODbL",
        "regions": regions,
    }
    with open(args.output, "w") as f:
        json.dump(manifest, f, separators=(",", ":"))
    print(f"{args.output}: {len(regions)} region(s), "
          f"{sum(len(r['parts']) for r in regions)} part(s), {os.path.getsize(args.output):,} bytes")


if __name__ == "__main__":
    main()
