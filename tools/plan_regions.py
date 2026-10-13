#!/usr/bin/env python3
"""
Decides which Geofabrik extracts are built, from Geofabrik's own index
(https://download.geofabrik.de/index-v1-nogeom.json).

- Every country Geofabrik lists under Europe, plus INCLUDE (Turkey), minus
  EXCLUDE (Russia, Ukraine, and Geofabrik's combined extracts such as "alps"
  or "dach", which would duplicate countries).
- An extract bigger than --max-gb is replaced by its subregions (Germany by
  its states, and so on, as deep as needed), so every build job fits on one
  GitHub machine. Overseas territories (France's DOM) are left out.
- Every region gets a fixed day of the month (1-28) for its monthly rebuild,
  spread over the month.

Usage:
  python plan_regions.py --select all|today|plan|"<id> <id> ..." [--github-output FILE]

Prints the plan, writes regions.json, and (with --github-output) the matrix
for the workflow: regions (JSON list) and count.
"""

import argparse
import json
import sys
import urllib.request
import zlib
from datetime import datetime, timezone

INDEX_URL = "https://download.geofabrik.de/index-v1-nogeom.json"
UA = "carryolia-map-builder (github.com/Robsus/carryolia-map)"

INCLUDE = ["turkey"]
EXCLUDE = {
    "russia", "ukraine",
    # Combined extracts that overlap the countries:
    "alps", "britain-and-ireland", "dach",
    # Great Britain is also listed as part of "united-kingdom" (England,
    # Scotland, Wales); Northern Ireland comes with "ireland-and-northern-ireland".
    "great-britain",
    # Overseas territories (not in Europe):
    "guadeloupe", "guyane", "martinique", "mayotte", "reunion", "bermuda", "falklands",
}


def load_index():
    req = urllib.request.Request(INDEX_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        doc = json.load(r)
    return {f["properties"]["id"]: f["properties"] for f in doc["features"]}


def size_of(url, cache={}):
    if url not in cache:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            cache[url] = int(r.headers.get("Content-Length", "0"))
    return cache[url]


def day_of(region_id):
    return zlib.crc32(region_id.encode()) % 28 + 1


def plan(index, max_bytes):
    children = {}
    for rid, p in index.items():
        children.setdefault(p.get("parent"), []).append(rid)
    roots = sorted(rid for rid in children.get("europe", []) if rid not in EXCLUDE)
    roots += [rid for rid in INCLUDE if rid in index and rid not in roots]

    regions = []

    def expand(rid, path):
        p = index[rid]
        pbf = p["urls"]["pbf"]
        size = size_of(pbf)
        subs = [c for c in sorted(children.get(rid, [])) if c not in EXCLUDE]
        if size > max_bytes and subs:
            for c in subs:
                expand(c, path + [rid])
            return
        regions.append({
            "id": rid,
            "name": p.get("name", rid),
            "parent": "/".join(path) or None,
            "pbf": pbf,
            "poly": pbf.replace("-latest.osm.pbf", ".poly"),
            "mb": round(size / 1e6),
            "day": day_of(rid),
        })

    for rid in roots:
        expand(rid, [])
    return regions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--select", default="plan", help="all | today | plan | space-separated region ids")
    ap.add_argument("--max-gb", type=float, default=2.0)
    ap.add_argument("--github-output", default=None)
    ap.add_argument("--out", default="regions.json")
    args = ap.parse_args()

    index = load_index()
    regions = plan(index, args.max_gb * 1e9)
    with open(args.out, "w") as f:
        json.dump(regions, f, indent=1)

    sel = args.select.strip()
    if sel in ("all", "plan"):
        chosen = regions
    elif sel == "today":
        today = datetime.now(timezone.utc).day
        chosen = [r for r in regions if r["day"] == today]
    else:
        wanted = set(sel.split())
        chosen = [r for r in regions if r["id"] in wanted or any(w in (r["parent"] or "").split("/") for w in wanted)]
        missing = wanted - {r["id"] for r in chosen} - {w for w in wanted if any(w in (r["parent"] or "").split("/") for r in regions)}
        if missing:
            print(f"Unknown regions: {sorted(missing)}", file=sys.stderr)
    if sel == "plan":
        chosen_for_build = []
    else:
        chosen_for_build = chosen

    total = sum(r["mb"] for r in regions)
    print(f"{len(regions)} regions, {total / 1000:.1f} GB of extracts; selected for building: {len(chosen_for_build)}")
    for r in sorted(regions, key=lambda r: (r["parent"] or r["id"], r["id"])):
        mark = "*" if r in chosen_for_build else " "
        print(f"{mark} {r['id']:32} {r['mb']:>6} MB  day {r['day']:>2}  {r['parent'] or ''}")

    if args.github_output:
        matrix = [{k: r[k] for k in ("id", "pbf", "poly", "mb")} for r in chosen_for_build]
        # Biggest first, so the longest jobs start early.
        matrix.sort(key=lambda r: -r["mb"])
        with open(args.github_output, "a") as f:
            f.write(f"regions={json.dumps(matrix, separators=(',', ':'))}\n")
            f.write(f"count={len(matrix)}\n")


if __name__ == "__main__":
    main()
