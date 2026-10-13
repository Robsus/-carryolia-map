#!/usr/bin/env python3
"""
Self-test for build_tiles.py on a made-up map (no download needed).

1. Writes a synthetic OSM file round a point in the Netherlands: buildings,
   shops, a storage tank with/without the right content, bicycle parkings
   above/below the capacity limit, fields, a tree row, roads (some long and
   diagonal), a forest relation spanning many tiles, a water relation, a forest
   relation too big for the game, and objects the game never asks for.
2. Filters it with osmium tags-filter and builds the archive.
3. For hundreds of random circles, compares what the archive delivers (the
   tiles overlapping the circle, then the same "around" distance test) with a
   brute-force search over the original objects. Does the same for road
   bounding boxes. Any difference fails the test.

Usage: python tools/tests/test_build.py [work dir]
"""

import gzip
import json
import math
import os
import random
import subprocess
import sys

import osmium
from pmtiles.reader import MmapSource, Reader, all_tiles
from pmtiles.tile import tileid_to_zxy, zxy_to_tileid

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)
import build_tiles as bt  # noqa: E402

ZOOM = 15
LAT0, LON0 = 52.09, 5.12
R = 6_378_137.0
random.seed(7)


def offset(lat, lon, dx, dy):
    return lat + math.degrees(dy / R), lon + math.degrees(dx / (R * math.cos(math.radians(lat))))


def to_xy(lat, lon, lat_ref, lon_ref):
    x = math.radians(lon - lon_ref) * R * math.cos(math.radians((lat + lat_ref) / 2))
    y = math.radians(lat - lat_ref) * R
    return x, y


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
    """Does segment a-b (lat, lon) touch box (s, w, n, e)? Liang-Barsky clip."""
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


# ---------------------------------------------------------------- synthetic map

class MapMaker:
    def __init__(self):
        self.nodes = {}   # id -> (lat, lon, tags)
        self.ways = {}    # id -> (node ids, tags)
        self.rels = {}    # id -> (members [(type, ref, role)], tags)
        self.next_node = 1
        self.next_way = 1
        self.next_rel = 1

    def node(self, lat, lon, tags=None):
        nid = self.next_node
        self.next_node += 1
        self.nodes[nid] = (round(lat, 7), round(lon, 7), tags or {})
        return nid

    def way(self, coords, tags=None, closed=False):
        ids = [self.node(lat, lon) for lat, lon in coords]
        if closed:
            ids.append(ids[0])
        wid = self.next_way
        self.next_way += 1
        self.ways[wid] = (ids, tags or {})
        return wid

    def rel(self, members, tags):
        rid = self.next_rel
        self.next_rel += 1
        self.rels[rid] = (members, tags)
        return rid

    def write(self, path):
        if os.path.exists(path):
            os.remove(path)
        w = osmium.SimpleWriter(path)
        for nid in sorted(self.nodes):
            lat, lon, tags = self.nodes[nid]
            w.add_node(osmium.osm.mutable.Node(id=nid, location=(lon, lat), tags=tags, version=1))
        for wid in sorted(self.ways):
            ids, tags = self.ways[wid]
            w.add_way(osmium.osm.mutable.Way(id=wid, nodes=ids, tags=tags, version=1))
        for rid in sorted(self.rels):
            members, tags = self.rels[rid]
            w.add_relation(osmium.osm.mutable.Relation(id=rid, members=members, tags=tags, version=1))
        w.close()


def ring(lat, lon, radius, points=8, wobble=0.0):
    out = []
    for i in range(points):
        a = 2 * math.pi * i / points
        r = radius * (1 + wobble * (random.random() - 0.5))
        out.append(offset(lat, lon, r * math.cos(a), r * math.sin(a)))
    return out


def make_map():
    m = MapMaker()
    spread = 6000  # metres round the centre
    rnd = lambda: offset(LAT0, LON0, random.uniform(-spread, spread), random.uniform(-spread, spread))

    for _ in range(3000):  # houses
        lat, lon = rnd()
        m.way(ring(lat, lon, random.uniform(4, 15), 4), {"building": random.choice(["house", "yes", "apartments"])}, closed=True)
    for _ in range(300):  # shops and amenities as nodes
        lat, lon = rnd()
        m.node(lat, lon, random.choice([{"shop": "bakery"}, {"amenity": "cafe"}, {"amenity": "school"},
                                        {"craft": "carpenter"}, {"tourism": "museum"}]))
    for content in ["oil", "water", "diesel", None]:
        lat, lon = rnd()
        tags = {"man_made": "storage_tank"}
        if content:
            tags["content"] = content
        m.way(ring(lat, lon, 10, 6), tags, closed=True)
    for capacity, kind in [("60", None), ("10", None), ("abc", "building"), ("20", None), (" 50", None)]:
        lat, lon = rnd()
        tags = {"amenity": "bicycle_parking", "capacity": capacity}
        if kind:
            tags["bicycle_parking"] = kind
        m.node(lat, lon, tags)
    for rt in ["centre", "container"]:
        lat, lon = rnd()
        m.node(lat, lon, {"amenity": "recycling", "recycling_type": rt})
    for _ in range(200):  # fields, meadows, parks
        lat, lon = rnd()
        m.way(ring(lat, lon, random.uniform(40, 400), 10, 0.4),
              {random.choice(["landuse", "landuse", "leisure", "natural"]): random.choice(
                  ["farmland", "meadow", "grass", "forest"])} if random.random() < 0.7 else {"leisure": "park"},
              closed=True)
    for _ in range(20):  # greenhouses: both points and areas
        lat, lon = rnd()
        m.way(ring(lat, lon, 30, 4), {"building": "greenhouse"}, closed=True)
    for _ in range(30):  # tree rows (lines)
        lat, lon = rnd()
        m.way([(lat, lon), offset(lat, lon, random.uniform(-200, 200), random.uniform(-200, 200))], {"natural": "tree_row"})
    for _ in range(400):  # roads, some long and diagonal
        lat, lon = rnd()
        pts = [(lat, lon)]
        for _ in range(random.randint(1, 8)):
            step = random.choice([30, 80, 300, 2500])
            pts.append(offset(*pts[-1], random.uniform(-step, step), random.uniform(-step, step)))
        m.way(pts, {"highway": random.choice(["residential", "footway", "primary", "motorway", "track"])})
    for _ in range(300):  # never asked for
        lat, lon = rnd()
        m.way(ring(lat, lon, 50, 5), {"landuse": "residential"}, closed=True)
        m.node(lat, lon, {"natural": "tree"})

    # A forest relation of three outer pieces spanning many tiles, with a clearing.
    big_ring = ring(LAT0 + 0.01, LON0 - 0.02, 2500, 60, 0.2)
    thirds = [big_ring[0:21], big_ring[20:41], big_ring[40:] + [big_ring[0]]]
    piece_ids = [m.way(p) for p in thirds]
    clearing = m.way(ring(LAT0 + 0.01, LON0 - 0.02, 300, 8), closed=True)
    m.rel([("w", piece_ids[0], "outer"), ("w", piece_ids[1], "outer"), ("w", piece_ids[2], "outer"),
           ("w", clearing, "inner")], {"type": "multipolygon", "landuse": "forest"})
    # A lake relation (points block only: no members sent, outline pieces per tile).
    lake = m.way(ring(LAT0 - 0.02, LON0 + 0.03, 1200, 40, 0.3), closed=True)
    m.rel([("w", lake, "outer")], {"type": "multipolygon", "natural": "water"})
    # A forest relation too big for the game (> 50,000 points).
    huge = m.way(ring(LAT0 - 0.03, LON0 - 0.04, 1500, 50_500, 0.0), closed=True)
    m.rel([("w", huge, "outer")], {"type": "multipolygon", "natural": "wood"})
    # A big forest way (shared object: stored once, stubs in the tiles it crosses).
    m.way(ring(LAT0 + 0.03, LON0 + 0.05, 2000, 900, 0.1), {"landuse": "forest"}, closed=True)
    # A forest relation big enough to be shared, with two outer rings.
    r1 = m.way(ring(LAT0 + 0.04, LON0 - 0.06, 1500, 400, 0.1), closed=True)
    r2 = m.way(ring(LAT0 + 0.05, LON0 - 0.03, 600, 200, 0.1), closed=True)
    m.rel([("w", r1, "outer"), ("w", r2, "outer")], {"type": "multipolygon", "natural": "wood"})
    # A park relation whose members are all missing from the extract.
    m.rel([("w", 999_999_999, "outer")], {"type": "multipolygon", "leisure": "park"})
    return m


# ---------------------------------------------------------------- expected answers (brute force)

def expected_objects(m):
    """Every object the game's queries match, with its full geometry, from the synthetic map."""
    objs = {}
    for nid, (lat, lon, tags) in m.nodes.items():
        if tags and bt.is_point(tags, "n"):
            objs[("n", nid)] = ("p", [lat, lon], None)
    for wid, (ids, tags) in m.ways.items():
        if not tags:
            continue
        kind = ("p" if bt.is_point(tags, "w") else "") + ("a" if bt.is_area(tags, "w") else "") + \
               ("r" if bt.is_road(tags, "w") else "")
        if kind:
            flat = [v for nid in ids for v in m.nodes[nid][:2]]
            objs[("w", wid)] = (kind, flat, None)
    for rid, (members, tags) in m.rels.items():
        kind = ("p" if bt.is_point(tags, "r") else "") + ("a" if bt.is_area(tags, "r") else "")
        if not kind:
            continue
        lines = []
        for t, ref, _ in members:
            if t == "w" and ref in m.ways:
                lines.append([v for nid in m.ways[ref][0] for v in m.nodes[nid][:2]])
        if lines:
            objs[("r", rid)] = (kind, None, lines)
    return objs


def obj_dist(lat, lon, flat, lines):
    if lines is not None:
        return min(line_dist(lat, lon, l) for l in lines)
    return line_dist(lat, lon, flat)


# ---------------------------------------------------------------- archive side

OBJECTS = {}


def load_archive(out_dir, name):
    """Map tiles by (x, y); shared objects (other zoom levels) go into OBJECTS by (type, id)."""
    tiles = {}
    for fn in sorted(os.listdir(out_dir)):
        if fn.startswith(name) and fn.endswith(".pmtiles"):
            with open(os.path.join(out_dir, fn), "rb") as f:
                get = MmapSource(f)
                for (z, x, y), data in all_tiles(get):
                    doc = json.loads(gzip.decompress(data))
                    assert doc["v"] == 1
                    if z == ZOOM:
                        tiles.setdefault((x, y), []).extend(doc["e"])
                    else:
                        for el in doc["e"]:
                            assert bt.object_address(el["t"], el["id"]) == (z, x, y), "shared object at the wrong address"
                            OBJECTS[(el["t"], el["id"])] = el
    return tiles


def tiles_for_box(s, w, n_, e, zoom=ZOOM):
    n = 1 << zoom
    x0, y0 = bt.tile_xy(n_, w, n)
    x1, y1 = bt.tile_xy(s, e, n)
    return [(x, y) for x in range(int(x0), int(x1) + 1) for y in range(int(y0), int(y1) + 1)]


def archive_around(tiles, lat, lon, radius):
    """What the game would get for a discovery "around" query: the tiles overlapping the circle, filtered."""
    s, w = offset(lat, lon, -radius, -radius)
    n_, e = offset(lat, lon, radius, radius)
    found = {}
    for t in tiles_for_box(s, w, n_, e):
        for el in tiles.get(t, []):
            if "p" not in el["k"] and "a" not in el["k"]:
                continue
            if el["t"] == "n":
                d = line_dist(lat, lon, el["c"])
            elif "f" in el:
                d = min(line_dist(lat, lon, run) for run in el["f"])
            elif el["t"] == "w":
                d = line_dist(lat, lon, el["g"])
            elif "m" in el:
                d = min(line_dist(lat, lon, mm["g"]) for mm in el["m"])
            if d <= radius:
                key = (el["t"], el["id"])
                found[key] = min(found.get(key, 1e18), d)
    return found


def archive_roads(tiles, box):
    s, w, n_, e = box
    found = set()
    for t in tiles_for_box(s, w, n_, e):
        for el in tiles.get(t, []):
            if "r" not in el["k"]:
                continue
            g = el["g"]
            assert len(g) == 2 * len(el["n"]), "road nodes and geometry differ in length"
            pts = [(g[i], g[i + 1]) for i in range(0, len(g), 2)]
            if any(seg_hits_box(pts[i], pts[i + 1], box) for i in range(len(pts) - 1)):
                found.add(el["id"])
    return found


def main():
    work = sys.argv[1] if len(sys.argv) > 1 else "/tmp/carryolia-test"
    os.makedirs(work, exist_ok=True)
    raw = os.path.join(work, "synthetic.osm.pbf")
    filtered = os.path.join(work, "synthetic-filtered.osm.pbf")
    out = os.path.join(work, "out")

    m = make_map()
    m.write(raw)
    subprocess.run(["osmium", "tags-filter", raw, "--expressions", os.path.join(TOOLS, "filters.txt"),
                    "-o", filtered, "--overwrite", "--no-progress"], check=True)
    subprocess.run([sys.executable, os.path.join(TOOLS, "build_tiles.py"), filtered, "--name", "synthetic",
                    "--out-dir", out, "--max-part-bytes", "300000000"], check=True)

    tiles = load_archive(out, "synthetic")
    expected = expected_objects(m)
    failures = 0

    # Checks on single objects.
    by_key = {}
    for els in tiles.values():
        for el in els:
            by_key.setdefault((el["t"], el["id"]), el)
    tank_kinds = {m.ways[k[1]][1].get("content"): k for k in by_key if k[0] == "w" and
                  m.ways[k[1]][1].get("man_made") == "storage_tank"}
    assert set(tank_kinds) == {"oil", "diesel"}, f"storage tanks: {set(tank_kinds)}"
    caps = sorted(m.nodes[k[1]][2].get("capacity") for k in by_key
                  if k[0] == "n" and m.nodes[k[1]][2].get("amenity") == "bicycle_parking")
    assert caps == [" 50", "60", "abc"], f"bicycle parkings: {caps}"
    rec = [k for k in by_key if k[0] == "n" and m.nodes[k[1]][2].get("amenity") == "recycling"]
    assert len(rec) == 1, "recycling centre"
    rels = {k[1]: by_key[k] for k in by_key if k[0] == "r"}
    forest, lake, huge = rels[1], rels[2], rels[3]
    assert "m" in forest and len(forest["m"]) == 4 and forest["k"] == "a", "forest relation"
    assert "m" not in lake and "f" in lake and lake["k"] == "p", "lake relation"
    assert huge.get("big") == 1 and "m" not in huge, "huge relation"
    assert 5 not in rels, "relation without member geometry must be left out"
    shared_rel = rels[4]
    assert shared_rel.get("obj") == 1 and "m" not in shared_rel and "f" in shared_rel, "shared relation stub"
    full = OBJECTS[("r", 4)]
    assert len(full["m"]) == 2 and sum(len(mm["g"]) for mm in full["m"]) // 2 == 602, "shared relation object"
    big_way = [el for k, el in by_key.items() if k[0] == "w" and el.get("obj") == 1]
    assert len(big_way) == 1 and "g" not in big_way[0], "shared way stub"
    full_way = OBJECTS[("w", big_way[0]["id"])]
    assert len(full_way["g"]) // 2 == 901 and full_way["k"] == "a", "shared way object"
    print(f"shared objects: {len(OBJECTS)}")
    greenhouse = [el for k, el in by_key.items() if k[0] == "w" and el["tags"].get("building") == "greenhouse"]
    assert greenhouse and all(el["k"] == "pa" for el in greenhouse), "greenhouses are points and areas"
    motorway = [k for k in by_key if k[0] == "w" and by_key[k]["tags"].get("highway") == "motorway"]
    assert not motorway, "motorways are not roads for the game"
    print("single-object checks: ok")

    # Discovery circles.
    checked = 0
    for i in range(400):
        lat, lon = offset(LAT0, LON0, random.uniform(-6500, 6500), random.uniform(-6500, 6500))
        radius = random.choice([150, 200, 250, 600, 2000])
        want = {k for k, (kind, flat, lines) in expected.items()
                if ("p" in kind or "a" in kind) and obj_dist(lat, lon, flat, lines) <= radius}
        got = archive_around(tiles, lat, lon, radius)
        missing = want - set(got)
        extra = set(got) - want
        # Objects right on the edge can differ by rounding; anything else is a bug.
        missing = {k for k in missing if obj_dist(lat, lon, *expected[k][1:]) < radius - 0.01}
        extra = {k for k in extra if got[k] < radius - 0.01}
        if missing or extra:
            failures += 1
            print(f"circle {i} ({lat:.5f},{lon:.5f} r={radius}): missing {sorted(missing)[:5]} extra {sorted(extra)[:5]}")
        checked += len(want)
    print(f"discovery circles: 400 checked, {checked:,} objects expected in total, {failures} with differences")

    # Road boxes (RoadRepository: RoutingConfig.ROAD_TILE_SIZE_DEGREES squares).
    road_failures = 0
    size = 0.0025
    for i in range(400):
        lat, lon = offset(LAT0, LON0, random.uniform(-6000, 6000), random.uniform(-6000, 6000))
        s = math.floor(lat / size) * size
        w = math.floor(lon / size) * size
        box = (s, w, s + size, w + size)
        want = set()
        for k, (kind, flat, _) in expected.items():
            if "r" in kind:
                pts = [(flat[j], flat[j + 1]) for j in range(0, len(flat), 2)]
                if any(seg_hits_box(pts[j], pts[j + 1], box) for j in range(len(pts) - 1)):
                    want.add(k[1])
        got = archive_roads(tiles, box)
        if want != got:
            road_failures += 1
            print(f"road box {i}: missing {sorted(want - got)[:5]} extra {sorted(got - want)[:5]}")
    print(f"road boxes: 400 checked, {road_failures} with differences")

    if failures or road_failures:
        sys.exit(1)
    print("ALL OK")


if __name__ == "__main__":
    main()
