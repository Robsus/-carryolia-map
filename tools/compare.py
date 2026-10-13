#!/usr/bin/env python3
"""
Compares the archive with Overpass: for spots in a region, sends the game's
own discovery and road queries to Overpass and answers the same questions
from the archive the way the game will (the tiles overlapping the circle or
box, then the same distance / box test). Lists everything one side has and
the other doesn't.

Overpass is asked for the data as it was at the extract's timestamp
([date:...]), so edits made since then don't show up as differences. If the
server can't do that, it is asked for today's data and the report says so.

It waits --delay seconds between Overpass requests and stops at the first
"too many requests" for a minute, so it stays within the public servers' rules.

Usage:
  python compare.py <dir with the region's .pmtiles parts and .summary.json>
         --region netherlands --points 200 [--server URL] [--delay 10]
"""

import argparse
import glob
import gzip
import json
import math
import os
import random
import sys
import time
import urllib.parse
import urllib.request

from pmtiles.reader import MmapSource, Reader
from pmtiles.tile import zxy_to_tileid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_tiles as bt  # noqa: E402

R = 6_378_137.0
UA = "carryolia-map-compare (github.com/Robsus/carryolia-map)"
ROAD_TILE = 0.0025  # RoutingConfig.ROAD_TILE_SIZE_DEGREES


# ---------------------------------------------------------------- geometry

def to_xy(lat, lon, lat_ref, lon_ref):
    return (math.radians(lon - lon_ref) * R * math.cos(math.radians((lat + lat_ref) / 2)),
            math.radians(lat - lat_ref) * R)


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def line_dist(lat, lon, flat):
    pts = [to_xy(flat[i], flat[i + 1], lat, lon) for i in range(0, len(flat), 2)]
    if len(pts) == 1:
        return math.hypot(*pts[0])
    return min(seg_dist(0, 0, *pts[i], *pts[i + 1]) for i in range(len(pts) - 1))


def seg_hits_box(a, b, box):
    s, w, n, e = box
    x0, y0, x1, y1 = a[1], a[0], b[1], b[0]
    t0, t1 = 0.0, 1.0
    dx, dy = x1 - x0, y1 - y0
    for p, q in ((-dx, x0 - w), (dx, e - x0), (-dy, y0 - s), (dy, n - y0)):
        if p == 0:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return False
    return True


def offset(lat, lon, dx, dy):
    return lat + math.degrees(dy / R), lon + math.degrees(dx / (R * math.cos(math.radians(lat))))


# ---------------------------------------------------------------- archive

class Archive:
    def __init__(self, directory, region):
        self.files = []
        self.zoom = None
        for path in sorted(glob.glob(os.path.join(directory, f"{region}-*.pmtiles"))):
            f = open(path, "rb")
            reader = Reader(MmapSource(f))
            self.files.append(reader)
            self.zoom = reader.header()["max_zoom"]
        if not self.files:
            raise SystemExit(f"no {region}-*.pmtiles in {directory}")
        self.cache = {}

    def tile(self, x, y):
        key = (x, y)
        if key not in self.cache:
            elements = []
            for reader in self.files:
                data = reader.get(self.zoom, x, y)
                if data:
                    elements.extend(json.loads(gzip.decompress(data))["e"])
            self.cache[key] = elements
        return self.cache[key]

    def tiles_for_box(self, s, w, n_, e):
        n = 1 << self.zoom
        x0, y0 = bt.tile_xy(n_, w, n)
        x1, y1 = bt.tile_xy(s, e, n)
        return [(x, y) for x in range(int(x0), int(x1) + 1) for y in range(int(y0), int(y1) + 1)]

    def around(self, lat, lon, radius):
        """{(type, id): (kinds, distance)} for discovery blocks p and a."""
        s, w = offset(lat, lon, -radius, -radius)
        n_, e = offset(lat, lon, radius, radius)
        found = {}
        for t in self.tiles_for_box(s, w, n_, e):
            for el in self.tile(*t):
                if "p" not in el["k"] and "a" not in el["k"]:
                    continue
                if el["t"] == "n":
                    d = line_dist(lat, lon, el["c"])
                elif "f" in el and el["t"] == "w":
                    d = min(line_dist(lat, lon, run) for run in el["f"])  # shared object stub
                elif el["t"] == "w":
                    d = line_dist(lat, lon, el["g"])
                elif "m" in el:
                    d = min(line_dist(lat, lon, m["g"]) for m in el["m"])
                else:
                    d = min(line_dist(lat, lon, run) for run in el["f"])
                key = (el["t"], el["id"])
                if d <= radius and (key not in found or d < found[key][1]):
                    found[key] = (el["k"], d, el)
        return found

    def roads(self, box):
        found = set()
        for t in self.tiles_for_box(*box):
            for el in self.tile(*t):
                if "r" not in el["k"]:
                    continue
                g = el["g"]
                pts = [(g[i], g[i + 1]) for i in range(0, len(g), 2)]
                if any(seg_hits_box(pts[i], pts[i + 1], box) for i in range(len(pts) - 1)):
                    found.add(el["id"])
        return found

    def sample_points(self, bounds, count, rnd):
        """Half near buildings (towns), half anywhere with data in the region."""
        points = []
        tries = 0
        n = 1 << self.zoom
        s, w, n_, e = bounds
        while len(points) < count and tries < count * 200:
            tries += 1
            lat, lon = rnd.uniform(s, n_), rnd.uniform(w, e)
            x, y = bt.tile_xy(lat, lon, n)
            els = self.tile(int(x), int(y))
            if not els:
                continue
            if len(points) % 2 == 0:
                houses = [el for el in els if el["t"] == "w" and "p" in el["k"]]
                if not houses:
                    continue
                c = rnd.choice(houses)["c"]
                lat, lon = c[0], c[1]
            points.append((lat, lon))
        return points


# ---------------------------------------------------------------- Overpass

DISCOVERY = """[out:json][timeout:60]{date};
(
  nwr["building"](around:{r},{lat},{lon});
  nwr["shop"](around:{r},{lat},{lon});
  nwr["amenity"~"^(place_of_worship|restaurant|fast_food|cafe|food_court|veterinary|fuel|police|fire_station|townhall|library|school|college|university)$"](around:{r},{lat},{lon});
  nwr["tourism"="museum"](around:{r},{lat},{lon});
  nwr["man_made"~"^(windmill|works|petroleum_well)$"](around:{r},{lat},{lon});
  nwr["man_made"="storage_tank"]["content"~"^(oil|fuel|diesel)$"](around:{r},{lat},{lon});
  nwr["craft"~"^(bakery|sawmill|carpenter|tailor|blacksmith|metal_construction|welder)$"](around:{r},{lat},{lon});
  nwr["industrial"~"^(mine|refinery|oil|petroleum|scrap_yard)$"](around:{r},{lat},{lon});
  nwr["amenity"="recycling"]["recycling_type"="centre"](around:{r},{lat},{lon});
  nwr["amenity"="bicycle_parking"](around:{r},{lat},{lon})(if: number(t["capacity"]) >= 50 || t["bicycle_parking"] == "building" || t["bicycle_parking"] == "multi-storey" || t["bicycle_parking"] == "underground");
  relation["natural"="water"](around:{r},{lat},{lon});
);
out ids;
(
  way["landuse"~"^(farmland|meadow|orchard|vineyard|greenhouse_horticulture|plant_nursery|allotments|farmyard|heath|grass|flowerbed)$"](around:{r},{lat},{lon});
  way["natural"="heath"](around:{r},{lat},{lon});
  way["natural"="wetland"](around:{r},{lat},{lon});
  way["leisure"="garden"](around:{r},{lat},{lon});
  way["leisure"="park"](around:{r},{lat},{lon});
  way["landuse"="forest"](around:{r},{lat},{lon});
  way["landuse"="quarry"](around:{r},{lat},{lon});
  way["natural"="wood"](around:{r},{lat},{lon});
  way["natural"="tree_row"](around:{r},{lat},{lon});
  way["natural"="water"](around:{r},{lat},{lon});
  way["building"="greenhouse"](around:{r},{lat},{lon});
  relation["type"="multipolygon"]["landuse"="forest"](around:{r},{lat},{lon});
  relation["type"="multipolygon"]["natural"="wood"](around:{r},{lat},{lon});
  relation["type"="multipolygon"]["leisure"="park"](around:{r},{lat},{lon});
);
out ids;
"""

ROADS = """[out:json][timeout:60]{date};
way["highway"~"^(trunk|trunk_link|primary|primary_link|secondary|secondary_link|tertiary|tertiary_link|unclassified|residential|living_street|service|road|pedestrian|footway|path|steps|cycleway|bridleway|track)$"]({s:.7f},{w:.7f},{n:.7f},{e:.7f});
out ids;
"""


class Overpass:
    def __init__(self, server, delay):
        self.server = server
        self.delay = delay
        self.last = 0.0
        self.requests = 0

    def run(self, query):
        for attempt in range(4):
            wait = self.delay - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self.requests += 1
            req = urllib.request.Request(self.server, data=urllib.parse.urlencode({"data": query}).encode(),
                                         headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    doc = json.loads(resp.read())
                remark = doc.get("remark", "")
                if "error" in remark.lower():
                    raise RuntimeError(remark)
                return doc
            except urllib.error.HTTPError as err:
                if err.code in (429, 500, 502, 503, 504):
                    print(f"  Overpass HTTP {err.code}: resting 60 s", flush=True)
                    time.sleep(60)
                    continue
                body = err.read().decode(errors="replace")[:300]
                raise RuntimeError(f"HTTP {err.code}: {body}")
            except (urllib.error.URLError, TimeoutError) as err:
                print(f"  Overpass unreachable ({err}): resting 60 s", flush=True)
                time.sleep(60)
        raise RuntimeError("Overpass kept refusing")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--region", required=True)
    ap.add_argument("--points", type=int, default=200)
    ap.add_argument("--radius", type=int, default=150)
    ap.add_argument("--server", default="https://overpass-api.de/api/interpreter")
    ap.add_argument("--delay", type=float, default=10.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--report", default="compare-report.json")
    ap.add_argument("--today", action="store_true", help="compare with today's data instead of the extract's date")
    args = ap.parse_args()

    with open(os.path.join(args.dir, f"{args.region}.summary.json")) as f:
        summary = json.load(f)
    archive = Archive(args.dir, args.region)
    overpass = Overpass(args.server, args.delay)
    rnd = random.Random(args.seed)
    points = archive.sample_points(summary["bounds"], args.points, rnd)

    ts = summary.get("osm_timestamp", "")
    date = f'[date:"{ts}"]' if ts and not args.today else ""
    try:
        overpass.run(f'[out:json][timeout:25]{date};node(1);out ids;')
    except RuntimeError as err:
        print(f"Server can't answer for {ts} ({err}); comparing with today's data instead.")
        date = ""
    print(f"Comparing {len(points)} spots (radius {args.radius} m) with {args.server}, "
          f"data as of {ts if date else 'today'}", flush=True)

    totals = {"p": [0, 0, 0], "a": [0, 0, 0], "roads": [0, 0, 0]}  # [both, archive only, overpass only]
    diffs = []
    skipped = 0
    edge = 1.0  # metres: objects this close to the circle's edge may fall either way
    for i, (lat, lon) in enumerate(points):
        fill = dict(date=date, r=args.radius, lat=f"{lat:.7f}", lon=f"{lon:.7f}")
        q = DISCOVERY.format(**fill)
        p_query, a_query = q.split("out ids;\n(", 1)
        try:
            p_doc = overpass.run(p_query + "out ids;")
            a_doc = overpass.run(f"[out:json][timeout:60]{date};\n(" + a_query)
        except RuntimeError as err:
            print(f"  spot {i}: skipped ({str(err)[:80]})", flush=True)
            skipped += 1
            continue
        got = archive.around(lat, lon, args.radius)
        for block, doc in (("p", p_doc), ("a", a_doc)):
            want = {({"node": "n", "way": "w", "relation": "r"}[e["type"]], e["id"]) for e in doc["elements"]}
            have = {k for k, (kinds, d, _) in got.items() if block in kinds}
            near_edge = {k for k, (kinds, d, _) in got.items() if d > args.radius - edge}
            only_archive = have - want - near_edge
            only_overpass = want - have
            totals[block][0] += len(have & want)
            totals[block][1] += len(only_archive)
            totals[block][2] += len(only_overpass)
            for k in sorted(only_archive):
                diffs.append({"spot": i, "lat": lat, "lon": lon, "block": block, "side": "archive only",
                              "object": f"{k[0]}{k[1]}", "distance_m": round(got[k][1], 2),
                              "tags": got[k][2].get("tags")})
            for k in sorted(only_overpass):
                diffs.append({"spot": i, "lat": lat, "lon": lon, "block": block, "side": "overpass only",
                              "object": f"{k[0]}{k[1]}"})

        s = math.floor(lat / ROAD_TILE) * ROAD_TILE
        w = math.floor(lon / ROAD_TILE) * ROAD_TILE
        box = (s, w, s + ROAD_TILE, w + ROAD_TILE)
        try:
            r_doc = overpass.run(ROADS.format(date=date, s=box[0], w=box[1], n=box[2], e=box[3]))
        except RuntimeError as err:
            print(f"  spot {i} roads: skipped ({str(err)[:80]})", flush=True)
            continue
        want = {e["id"] for e in r_doc["elements"] if e["type"] == "way"}
        have = archive.roads(box)
        totals["roads"][0] += len(have & want)
        totals["roads"][1] += len(have - want)
        totals["roads"][2] += len(want - have)
        for wid in sorted(have - want):
            diffs.append({"spot": i, "block": "roads", "side": "archive only", "object": f"w{wid}"})
        for wid in sorted(want - have):
            diffs.append({"spot": i, "block": "roads", "side": "overpass only", "object": f"w{wid}"})

        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(points)} spots: " + ", ".join(
                f"{b} {t[0]:,} same / {t[1]} archive only / {t[2]} overpass only" for b, t in totals.items()),
                flush=True)

    report = {"region": args.region, "server": args.server, "as_of": ts if date else "today",
              "spots": len(points), "skipped": skipped, "radius": args.radius, "totals": totals, "differences": diffs}
    with open(args.report, "w") as f:
        json.dump(report, f, indent=1)
    print()
    print(f"=== Archive vs Overpass: {args.region}, {len(points)} spots ===")
    for b, t in totals.items():
        name = {"p": "Buildings & places", "a": "Resource areas", "roads": "Roads"}[b]
        print(f"{name:20} {t[0]:>8,} in both   {t[1]:>5} archive only   {t[2]:>5} Overpass only")
    print(f"Overpass requests: {overpass.requests}, spots skipped: {skipped}")
    for d in diffs[:60]:
        print("  ", json.dumps(d, ensure_ascii=False)[:300])


if __name__ == "__main__":
    main()
