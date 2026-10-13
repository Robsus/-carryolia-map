#!/usr/bin/env python3
"""
Writes map-data.json: the one file the game reads first. Since format 2,
every region has its own release (rebuilt on its own day each month), so
the manifest lists per region: its release tag, where its archive parts live,
its outline (from Geofabrik's .poly, so the game knows when to fall back to
Overpass) and its parts' tile ranges.

Input: one folder per region under <dist>, each holding
  <region>.summary.json   (from build_tiles.py)
  <region>.poly           (Geofabrik's outline)
  tag                     (the region's release tag, one line)

Usage:
  python make_manifest.py <dist> --repo Robsus/carryolia-map -o map-data.json
"""

import argparse
import json
import os
from datetime import datetime, timezone

MANIFEST_VERSION = 2


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


def region_entry(folder, name, repo, tolerance):
    with open(os.path.join(folder, f"{name}.summary.json")) as f:
        s = json.load(f)
    with open(os.path.join(folder, "tag")) as f:
        tag = f.read().strip()
    region = {
        "name": name,
        "tag": tag,
        "base_url": f"https://github.com/{repo}/releases/download/{tag}/",
        "osm_timestamp": s["osm_timestamp"],
        "built": s["built"],
        "zoom": s["zoom"],
        "tile_format_version": s["format_version"],
        "parts": [
            {k: p[k] for k in ("file", "bytes", "tiles", "min_tile", "max_tile", "bounds")}
            for p in s["parts"]
        ],
    }
    poly_path = os.path.join(folder, f"{name}.poly")
    if os.path.exists(poly_path):
        rings = read_poly(poly_path)
        region["outline"] = [
            {"hole": r["hole"], "ring": [round(v, 5) for p in simplify(r["pts"], tolerance) for v in p]}
            for r in rings
        ]
        lats = [p[0] for r in rings if not r["hole"] for p in r["pts"]]
        lons = [p[1] for r in rings if not r["hole"] for p in r["pts"]]
        region["bbox"] = [round(min(lats), 5), round(min(lons), 5), round(max(lats), 5), round(max(lons), 5)]
    else:
        b = s["bounds"]
        region["bbox"] = [round(v, 5) for v in b]
        region["outline"] = [{"hole": False, "ring": [b[0], b[1], b[0], b[3], b[2], b[3], b[2], b[1]]}]
    return region


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dist")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tolerance", type=float, default=0.002, help="outline simplification, degrees")
    ap.add_argument("-o", "--output", default="map-data.json")
    args = ap.parse_args()

    regions = []
    for name in sorted(os.listdir(args.dist)):
        folder = os.path.join(args.dist, name)
        if os.path.isdir(folder) and os.path.exists(os.path.join(folder, f"{name}.summary.json")):
            regions.append(region_entry(folder, name, args.repo, args.tolerance))
    if not regions:
        raise SystemExit("no regions found")
    zooms = {r["zoom"] for r in regions}
    formats = {r["tile_format_version"] for r in regions}
    if len(zooms) != 1 or len(formats) != 1:
        raise SystemExit(f"regions differ in zoom {zooms} or tile format {formats}")

    manifest = {
        "format": "carryolia-map-manifest",
        "version": MANIFEST_VERSION,
        "tile_format_version": formats.pop(),
        "zoom": zooms.pop(),
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attribution": "Map data © OpenStreetMap contributors, ODbL",
        "regions": regions,
    }
    with open(args.output, "w") as f:
        json.dump(manifest, f, separators=(",", ":"))
    print(f"{args.output}: {len(regions)} region(s), {sum(len(r['parts']) for r in regions)} part(s), "
          f"{os.path.getsize(args.output):,} bytes")


if __name__ == "__main__":
    main()
