#!/usr/bin/env python3
"""
Carryolia map data builder.

Turns a (filtered) OpenStreetMap extract into one or more PMTiles archives
that the game reads instead of asking Overpass. Every archive tile (web-map
tile at --zoom, 15 by default) holds gzip-compressed JSON with every object
the game's Overpass queries could return for a spot inside that tile:

  {"v": 1, "e": [ element, element, ... ]}

An element is compact, not Overpass's own layout; the game turns it back into
the Overpass answer it used to get (see the app's MapDataArchive):

  "t"     "n" node, "w" way, "r" relation
  "id"    OSM id
  "k"     why it is here, one letter per query block:
            p = discovery points block ("out tags center")
            q = discovery points block, objects the game asks for since 1.3.5
                (pubs, nightclubs, community centres: QUERY_POINTS_135).
                Older versions skip the letter, so they don't show these as
                plain buildings. An object that is also "p" is only "p".
            a = discovery areas block ("out tags geom")
            r = roads (RoadRepository, "out body" + nodes)
  "tags"  all OSM tags
  "c"     [lat, lon]: a node's position, or the centre of a way's/relation's
          bounding box (what Overpass sends as "center")
  "b"     [minlat, minlon, maxlat, maxlon] (Overpass "bounds")
  "g"     a way's geometry, flat: [lat, lon, lat, lon, ...]
  "n"     a road way's node ids, in the same order as "g"
  "m"     an area relation's members: [{"r": way id, "o": role, "g": [...]}]
  "f"     a relation's outline pieces inside THIS tile: a list of flat runs like "g"
          (only for relations sent without "m"), so the game can do the
          "around" distance test
  "big"   1: an area relation with more points than the game handles
          (DiscoveryConfig.MAX_FOREST_RELATION_POINTS); sent without members
  "obj"   1: a shared object. A big resource area (a way or relation of the
          areas block only, with at least SHARE_MIN_POINTS points crossing at
          least SHARE_MIN_TILES tiles) is stored ONCE, as its own entry at
          zoom OBJECT_ZOOM_RELATION (relations) or OBJECT_ZOOM_WAY (ways),
          x = id % 2^z, y = id // 2^z, holding the full element. The tiles it
          crosses hold this stub instead: tags, "c", "b" and "f" (its outline
          pieces inside that tile), without "g" / "m". The game fetches the
          full element only when it needs the outline.

An object goes into every tile that a piece of its outline passes through,
which is what Overpass's "around" and bounding-box filters look at.

SEEK INDEX (since game 1.4.17). A Traveller's "Seek resources" looks for one
kind of object within 5 km. Reading every tile of such a circle would cost
megabytes, so the archive also holds a small index: per kind of object (the
game's SeekConfig.FILTERS, mirrored in SEEK_TYPES below) and per square at
zoom SEEK_INDEX_ZOOM (12, about 6 km), one tile with the objects of that kind
whose centre lies in the square. It lives at zoom SEEK_ADDRESS_ZOOM (20),
x = code * 2^SEEK_INDEX_ZOOM + x12, y = y12 (code = position in SEEK_TYPES,
from 1), in the same {"v": 1, "e": [...]} layout as a tile:

  points (the Overpass points block of that kind, "out tags center"):
          {"t", "id", "c", "tags"}           -> a building, like discovery
  areas  (the areas block, "out tags geom"):
          {"t", "id", "c", "m": 1}           -> a marker only (no outline;
                                                the outline comes with the
                                                normal tiles when needed)

The summary (and so map-data.json) says "seek_index": SEEK_INDEX_VERSION;
a region without it is searched through Overpass, as before.

Usage:
  python build_tiles.py <filtered.osm.pbf> --name netherlands --out-dir out

Writes <out-dir>/<name>-NN.pmtiles (each part below --max-part-bytes, for
GitHub release assets) and <out-dir>/<name>.summary.json, and prints a
summary.
"""

import argparse
import gzip
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from array import array
from datetime import datetime, timezone

import osmium
from pmtiles.tile import Compression, TileType, tileid_to_zxy, zxy_to_tileid
from pmtiles.writer import Writer

FORMAT_VERSION = 1

# --- The game's queries (BuildingRepository / RoadRepository / RoutingConfig) ---
AMENITY_POINTS = {
    "place_of_worship", "restaurant", "fast_food", "cafe", "food_court", "veterinary",
    "fuel", "police", "fire_station", "townhall", "library", "school", "college", "university",
}
# Since game 1.3.5 (letter "q", see above).
AMENITY_POINTS_135 = {"pub", "nightclub", "community_centre"}
MAN_MADE_POINTS = {"windmill", "works", "petroleum_well"}
TANK_CONTENTS = {"oil", "fuel", "diesel"}
CRAFT_POINTS = {"bakery", "sawmill", "carpenter", "tailor", "blacksmith", "metal_construction", "welder"}
INDUSTRIAL_POINTS = {"mine", "refinery", "oil", "petroleum", "scrap_yard"}
BICYCLE_PARKING_MIN_CAPACITY = 50  # DiscoveryConfig.BICYCLE_PARKING_MIN_CAPACITY
BICYCLE_PARKING_KINDS = {"building", "multi-storey", "underground"}

AREA_LANDUSE = {
    "farmland", "meadow", "orchard", "vineyard", "greenhouse_horticulture", "plant_nursery",
    "allotments", "farmyard", "heath", "grass", "flowerbed", "forest", "quarry",
}
AREA_NATURAL = {"heath", "wetland", "wood", "tree_row", "water"}
AREA_LEISURE = {"garden", "park"}

HIGHWAYS = {
    "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential", "living_street", "service", "road",
    "pedestrian", "footway", "path", "steps", "cycleway", "bridleway", "track",
}

MAX_FOREST_RELATION_POINTS = 50_000  # DiscoveryConfig.MAX_FOREST_RELATION_POINTS

# Shared objects (see "obj" above): big areas are stored once, not in every tile they cross.
SHARE_MIN_POINTS = 300
SHARE_MIN_TILES = 4
OBJECT_ZOOM_RELATION = 26
OBJECT_ZOOM_WAY = 27


def object_address(osm_type, osm_id):
    """Where a shared object lives in the archive: (z, x, y)."""
    z = OBJECT_ZOOM_RELATION if osm_type == "r" else OBJECT_ZOOM_WAY
    return z, osm_id % (1 << z), osm_id // (1 << z)


def overpass_number(value):
    """Overpass number(): the value as a number, or NaN."""
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return float("nan")


def is_point(tags, osm_type):
    """Matches the discovery query's first block (nwr[...], plus relation[natural=water])."""
    if "building" in tags or "shop" in tags:
        return True
    amenity = tags.get("amenity")
    if amenity in AMENITY_POINTS:
        return True
    if tags.get("tourism") == "museum":
        return True
    man_made = tags.get("man_made")
    if man_made in MAN_MADE_POINTS:
        return True
    if man_made == "storage_tank" and tags.get("content") in TANK_CONTENTS:
        return True
    if tags.get("craft") in CRAFT_POINTS:
        return True
    if tags.get("industrial") in INDUSTRIAL_POINTS:
        return True
    if amenity == "recycling" and tags.get("recycling_type") == "centre":
        return True
    if amenity == "bicycle_parking" and (
        overpass_number(tags.get("capacity", "")) >= BICYCLE_PARKING_MIN_CAPACITY
        or tags.get("bicycle_parking") in BICYCLE_PARKING_KINDS
    ):
        return True
    if osm_type == "r" and tags.get("natural") == "water":
        return True
    return False


def is_point_135(tags, osm_type):
    """Discovery points the game asks for since 1.3.5 (and older versions don't): letter "q"."""
    return tags.get("amenity") in AMENITY_POINTS_135 and not is_point(tags, osm_type)


def point_kind(tags, osm_type):
    """"p", "q" or "" for the discovery's points block."""
    if is_point(tags, osm_type):
        return "p"
    if is_point_135(tags, osm_type):
        return "q"
    return ""


def is_area(tags, osm_type):
    """Matches the discovery query's areas block."""
    if osm_type == "w":
        return (
            tags.get("landuse") in AREA_LANDUSE
            or tags.get("natural") in AREA_NATURAL
            or tags.get("leisure") in AREA_LEISURE
            or tags.get("building") == "greenhouse"
        )
    if osm_type == "r":
        return tags.get("type") == "multipolygon" and (
            tags.get("landuse") == "forest" or tags.get("natural") == "wood" or tags.get("leisure") == "park"
        )
    return False


def is_road(tags, osm_type):
    return osm_type == "w" and tags.get("highway") in HIGHWAYS


# --- Seek index (see SEEK INDEX above; the game's SeekConfig.FILTERS) ---
SEEK_INDEX_VERSION = 1
SEEK_INDEX_ZOOM = 12
SEEK_ADDRESS_ZOOM = 20

# Codes are positions in this list (from 1). Only ever append: the game has
# the same list (SeekConfig.SEEK_INDEX_TYPES).
SEEK_TYPES = [
    "FIELD", "VINEYARD", "ORCHARD", "FOREST", "WATER", "PASTURE", "QUARRY", "GREENHOUSE", "FLOWERBED",
    "ALLOTMENT", "FARMYARD", "HEATH", "BOG", "BICYCLE_PARKING", "SCRAPYARD", "CHURCH", "SHOP", "SUPERMARKET",
    "MINE", "MUSEUM", "FUEL_STATION", "OIL_TANK", "OIL_WELL", "WINDMILL", "BAKERY", "SAWMILL", "TAILOR",
    "SMELTER", "FLORIST", "BICYCLE_SHOP", "RESTAURANT", "BUTCHER", "DEPARTMENT_STORE", "GARAGE", "CARPENTER",
    "SPINNING_MILL", "PUB", "NIGHTCLUB", "OIL_REFINERY", "VETERINARY", "POLICE_STATION", "FIRE_STATION",
    "TOWN_HALL", "LIBRARY", "COMMUNITY_CENTRE", "SCHOOL", "INDUSTRIAL",
]
SEEK_CODE = {name: i + 1 for i, name in enumerate(SEEK_TYPES)}


def seek_matches(tags, osm_type):
    """The kinds of SeekConfig.FILTERS this object matches, each with its block:
    [(kind, "p" | "a")]. "p": the points block (nwr[...], relation[natural=water]),
    "a": the areas block (way[...], relation[type=multipolygon][...])."""
    out = []
    g = tags.get
    landuse, natural, leisure = g("landuse"), g("natural"), g("leisure")
    amenity, shop, building = g("amenity"), g("shop"), g("building")
    man_made, craft, industrial = g("man_made"), g("craft"), g("industrial")

    if osm_type == "w":
        if landuse in ("farmland", "greenhouse_horticulture", "plant_nursery"):
            out.append(("FIELD", "a"))
        if landuse == "vineyard":
            out.append(("VINEYARD", "a"))
        if landuse == "orchard":
            out.append(("ORCHARD", "a"))
        if landuse == "forest" or natural in ("wood", "tree_row") or leisure == "park":
            out.append(("FOREST", "a"))
        if natural == "water":
            out.append(("WATER", "a"))
        if landuse == "meadow":
            out.append(("PASTURE", "a"))
        if landuse == "quarry":
            out.append(("QUARRY", "a"))
        if building == "greenhouse":
            out.append(("GREENHOUSE", "a"))
        if landuse == "flowerbed" or leisure == "garden":
            out.append(("FLOWERBED", "a"))
        if landuse == "allotments":
            out.append(("ALLOTMENT", "a"))
        if landuse == "farmyard":
            out.append(("FARMYARD", "a"))
        if natural == "heath" or landuse == "heath":
            out.append(("HEATH", "a"))
        if natural == "wetland":
            out.append(("BOG", "a"))
    if osm_type == "r":
        if g("type") == "multipolygon" and (landuse == "forest" or natural == "wood" or leisure == "park"):
            out.append(("FOREST", "a"))
        if natural == "water":
            out.append(("WATER", "p"))

    def point(kind):
        out.append((kind, "p"))

    if amenity == "bicycle_parking" and (
        overpass_number(g("capacity", "")) >= BICYCLE_PARKING_MIN_CAPACITY or g("bicycle_parking") in BICYCLE_PARKING_KINDS
    ):
        point("BICYCLE_PARKING")
    if industrial == "scrap_yard" or (amenity == "recycling" and g("recycling_type") == "centre"):
        point("SCRAPYARD")
    if (amenity == "place_of_worship" and g("religion") == "christian") or building in ("church", "chapel", "cathedral"):
        point("CHURCH")
    if shop is not None or building in ("retail", "commercial"):
        point("SHOP")
    if shop in ("supermarket", "convenience", "greengrocer") or building == "supermarket" or \
            amenity in ("fast_food", "cafe", "food_court"):
        point("SUPERMARKET")
    if industrial == "mine":
        point("MINE")
    if g("tourism") == "museum":
        point("MUSEUM")
    if amenity == "fuel":
        point("FUEL_STATION")
    if man_made == "storage_tank" and g("content") in TANK_CONTENTS:
        point("OIL_TANK")
    if man_made == "petroleum_well":
        point("OIL_WELL")
    if man_made == "windmill" or building == "windmill":
        point("WINDMILL")
    if shop == "bakery" or craft == "bakery":
        point("BAKERY")
    if craft in ("sawmill", "carpenter") or shop in ("hardware", "doityourself"):
        point("SAWMILL")
    if shop in ("clothes", "tailor") or craft == "tailor":
        point("TAILOR")
    if craft in ("blacksmith", "metal_construction", "welder") or man_made == "works":
        point("SMELTER")
    if shop == "florist":
        point("FLORIST")
    if shop == "bicycle":
        point("BICYCLE_SHOP")
    if amenity == "restaurant":
        point("RESTAURANT")
    if shop == "butcher":
        point("BUTCHER")
    if shop in ("houseware", "department_store", "variety_store"):
        point("DEPARTMENT_STORE")
    if shop in ("car_repair", "car"):
        point("GARAGE")
    if shop == "furniture":
        point("CARPENTER")
    if shop in ("fabric", "wool", "sewing"):
        point("SPINNING_MILL")
    if amenity == "pub":
        point("PUB")
    if amenity == "nightclub":
        point("NIGHTCLUB")
    if industrial in ("refinery", "oil", "petroleum"):
        point("OIL_REFINERY")
    if amenity == "veterinary":
        point("VETERINARY")
    if amenity == "police":
        point("POLICE_STATION")
    if amenity == "fire_station" or building == "fire_station":
        point("FIRE_STATION")
    if amenity == "townhall":
        point("TOWN_HALL")
    if amenity == "library":
        point("LIBRARY")
    if amenity == "community_centre":
        point("COMMUNITY_CENTRE")
    if amenity in ("school", "college", "university"):
        point("SCHOOL")
    if building == "industrial":
        point("INDUSTRIAL")
    return out


def seek_address(code, lat, lon):
    """(z, x, y) of the index tile of kind [code] for an object centred at (lat, lon)."""
    n = 1 << SEEK_INDEX_ZOOM
    x, y = tile_xy(lat, lon, n)
    return SEEK_ADDRESS_ZOOM, code * n + clamp(int(x), n), clamp(int(y), n)


def seek_entries(element):
    """The index entries of a tile element (needs "t", "id", "k", "tags", "c"):
    [((z, x, y), entry)], one per kind of SeekConfig.FILTERS it matches. Only
    objects the discovery itself returns (letters p, q, a) are indexed."""
    kinds = element.get("k", "")
    if not ("p" in kinds or "q" in kinds or "a" in kinds) or "c" not in element:
        return []
    lat, lon = element["c"]
    result = []
    seen = set()
    for name, block in seek_matches(element.get("tags", {}), element["t"]):
        if name in seen:
            continue
        seen.add(name)
        if block == "p":
            entry = {"t": element["t"], "id": element["id"], "c": element["c"], "tags": element["tags"]}
        else:
            entry = {"t": element["t"], "id": element["id"], "c": element["c"], "m": 1}
        result.append((seek_address(SEEK_CODE[name], lat, lon), entry))
    return result


# --- Tile maths (web mercator, as PMTiles / slippy maps) ---
MAX_LAT = 85.0511287798


def tile_xy(lat, lon, n):
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def clamp(i, n):
    return 0 if i < 0 else (n - 1 if i >= n else i)


def segment_tiles(out, x0, y0, x1, y1, n, depth=0):
    """Every tile the segment (in tile units) passes through, plus at most a few neighbours."""
    tx0, tx1 = sorted((clamp(int(x0), n), clamp(int(x1), n)))
    ty0, ty1 = sorted((clamp(int(y0), n), clamp(int(y1), n)))
    if (tx1 - tx0 <= 1 and ty1 - ty0 <= 1) or depth > 24:
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                out.add((tx, ty))
        return
    mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    segment_tiles(out, x0, y0, mx, my, n, depth + 1)
    segment_tiles(out, mx, my, x1, y1, n, depth + 1)


def line_tiles(flat, n):
    """Tiles of a flat [lat, lon, ...] line; per segment, so the result can be split per tile."""
    out = set()
    pts = [tile_xy(flat[i], flat[i + 1], n) for i in range(0, len(flat), 2)]
    if len(pts) == 1:
        out.add((clamp(int(pts[0][0]), n), clamp(int(pts[0][1]), n)))
        return out, pts
    for i in range(len(pts) - 1):
        segment_tiles(out, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], n)
    return out, pts


def line_pieces(lines, n):
    """For flat lines: every tile they cross, and per tile the runs of segments inside it."""
    pieces = {}
    tiles_all = set()
    for flat in lines:
        flat = list(flat)
        pts = [tile_xy(flat[i], flat[i + 1], n) for i in range(0, len(flat), 2)]
        prev = set()
        for i in range(len(pts) - 1):
            seg = set()
            segment_tiles(seg, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], n)
            for t in seg:
                runs = pieces.setdefault(t, [])
                a = flat[2 * i: 2 * i + 2]
                bpt = flat[2 * i + 2: 2 * i + 4]
                if t in prev and runs and runs[-1][-2:] == a:
                    runs[-1].extend(bpt)
                else:
                    runs.append(a + bpt)
            prev = seg
            tiles_all |= seg
    return tiles_all, pieces


def bounds_of(flat):
    lats = flat[0::2]
    lons = flat[1::2]
    return [min(lats), min(lons), max(lats), max(lons)]


def centre_of(b):
    return [round((b[0] + b[2]) / 2.0, 7), round((b[1] + b[3]) / 2.0, 7)]


def deg(e7):
    return round(e7 / 1e7, 7)


try:
    import orjson

    def dumps(obj):
        return orjson.dumps(obj).decode("utf-8")
except ImportError:  # slower, same result
    def dumps(obj):
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


class Stats:
    def __init__(self):
        self.started = time.time()
        self.phases = {}
        self.counts = {}
        self.bytes_by_kind = {}
        self.min_lat = 90.0
        self.min_lon = 180.0
        self.max_lat = -90.0
        self.max_lon = -180.0

    def count(self, key, by=1):
        self.counts[key] = self.counts.get(key, 0) + by

    def extent(self, b):
        self.min_lat = min(self.min_lat, b[0])
        self.min_lon = min(self.min_lon, b[1])
        self.max_lat = max(self.max_lat, b[2])
        self.max_lon = max(self.max_lon, b[3])


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class LineSink:
    """Collects '<tile id>\t<element json>' lines in a gzip file, for sorting by tile."""

    def __init__(self, path, zoom, stats):
        self.f = gzip.open(path, "wt", compresslevel=1, encoding="utf-8")
        self.zoom = zoom
        self.stats = stats

    def add(self, tiles, element_json, kind):
        for tx, ty in tiles:
            tid = zxy_to_tileid(self.zoom, tx, ty)
            self.f.write(f"{tid:020d}\t{element_json}\n")
        self.stats.count("lines", len(tiles))
        self.stats.bytes_by_kind[kind] = self.stats.bytes_by_kind.get(kind, 0) + len(element_json) * len(tiles)

    def add_object(self, osm_type, osm_id, element_json, kind):
        """A shared object: stored once, at its own address."""
        z, x, y = object_address(osm_type, osm_id)
        tid = zxy_to_tileid(z, x, y)
        self.f.write(f"{tid:020d}\t{element_json}\n")
        self.stats.count("lines")
        self.stats.count("shared_objects")
        self.stats.bytes_by_kind["shared"] = self.stats.bytes_by_kind.get("shared", 0) + len(element_json)

    def add_seek(self, element):
        """The element's Seek index entries (see SEEK INDEX)."""
        for (z, x, y), entry in seek_entries(element):
            text = dumps(entry)
            self.f.write(f"{zxy_to_tileid(z, x, y):020d}\t{text}\n")
            self.stats.count("seek_entries")
            self.stats.bytes_by_kind["seek"] = self.stats.bytes_by_kind.get("seek", 0) + len(text)

    def add_stub(self, element, pieces, kind):
        """The tile entries of a shared object: per tile, its outline pieces there."""
        for t, runs in pieces.items():
            element["f"] = runs
            self.add([t], dumps(element), kind)
        element.pop("f", None)

    def close(self):
        self.f.close()


def read_relations(path, stats):
    """Pass 1: the relations the game asks for, and which member ways they need."""
    wanted = {}
    needed_ways = set()
    for rel in osmium.FileProcessor(path, osmium.osm.RELATION):
        tags = {t.k: t.v for t in rel.tags}
        kind = point_kind(tags, "r") + ("a" if is_area(tags, "r") else "")
        if not kind:
            continue
        members = [(m.ref, m.role) for m in rel.members if m.type == "w"]
        if not members:
            continue
        wanted[rel.id] = (kind, tags, members)
        needed_ways.update(ref for ref, _ in members)
    stats.count("relations_wanted", len(wanted))
    return wanted, needed_ways


def object_tiles(flat, n):
    """Tiles of a flat [lat, lon, ...] line. Small objects (most buildings) take a shortcut:
    every tile of their bounding box (at most 2 x 2); bigger ones go segment by segment."""
    lats = flat[0::2]
    lons = flat[1::2]
    x0, y0 = tile_xy(max(lats), min(lons), n)
    x1, y1 = tile_xy(min(lats), max(lons), n)
    tx0, tx1 = clamp(int(x0), n), clamp(int(x1), n)
    ty0, ty1 = clamp(int(y0), n), clamp(int(y1), n)
    if tx1 - tx0 <= 1 and ty1 - ty0 <= 1:
        return [(tx, ty) for tx in range(tx0, tx1 + 1) for ty in range(ty0, ty1 + 1)]
    tiles, _ = line_tiles(flat, n)
    return tiles


def nodes_and_ways_worker(path, zoom, needed_ways, workers, index, lines_path):
    """Pass 2, one of [workers] processes: the nodes and ways whose id % workers == index.

    Reads a file whose ways already carry their node coordinates
    (osmium add-locations-to-ways), so no location index is needed here.
    Returns the counts, the extent and the geometry of relation member ways it saw."""
    n = 1 << zoom
    stats = Stats()
    sink = LineSink(lines_path, zoom, stats)
    member_geometry = {}
    seen = 0
    last_log = time.time()
    for obj in osmium.FileProcessor(path, osmium.osm.NODE | osmium.osm.WAY):
        if obj.id % workers != index:
            continue
        seen += 1
        if index == 0 and time.time() - last_log > 60:
            log(f"  worker 0: {seen:,} objects, {stats.counts.get('lines', 0):,} tile entries")
            last_log = time.time()

        if obj.is_node():
            if not obj.tags:
                continue
            tags = {t.k: t.v for t in obj.tags}
            kind = point_kind(tags, "n")
            if not kind or not obj.location.valid():
                continue
            lat, lon = deg(obj.location.y), deg(obj.location.x)
            tx, ty = tile_xy(lat, lon, n)
            element = {"t": "n", "id": obj.id, "k": kind, "tags": tags, "c": [lat, lon]}
            stats.extent([lat, lon, lat, lon])
            stats.count("nodes_" + kind)
            sink.add([(clamp(int(tx), n), clamp(int(ty), n))], dumps(element), kind)
            sink.add_seek(element)
            continue

        # A way.
        node_ids = []
        flat = []
        for nd in obj.nodes:
            loc = nd.location
            if loc.valid():
                node_ids.append(nd.ref)
                flat.append(deg(loc.y))
                flat.append(deg(loc.x))
        if obj.id in needed_ways and len(flat) >= 4:
            member_geometry[obj.id] = array("d", flat)
        if not obj.tags or len(flat) < 2:
            continue
        tags = {t.k: t.v for t in obj.tags}
        kind = point_kind(tags, "w") + ("a" if is_area(tags, "w") else "") + \
               ("r" if is_road(tags, "w") else "")
        if not kind:
            continue
        element = {"t": "w", "id": obj.id, "k": kind, "tags": tags, "g": flat}
        if "p" in kind or "q" in kind or "a" in kind:
            b = bounds_of(flat)
            element["b"] = b
            element["c"] = centre_of(b)
            stats.extent(b)
        if "r" in kind:
            element["n"] = node_ids
        for letter in kind:
            stats.count("ways_" + letter)
        sink.add_seek(element)
        tiles = object_tiles(flat, n)
        if kind == "a" and len(flat) // 2 >= SHARE_MIN_POINTS and len(tiles) >= SHARE_MIN_TILES:
            # Stored once; the tiles get a stub with their outline pieces.
            sink.add_object("w", obj.id, dumps(element), kind)
            tiles_all, pieces = line_pieces([flat], n)
            stub = {"t": "w", "id": obj.id, "k": kind, "tags": tags, "b": element["b"], "c": element["c"], "obj": 1}
            sink.add_stub(stub, pieces, kind)
            stats.count("ways_shared")
        else:
            sink.add(tiles, dumps(element), kind)
    sink.close()
    return {"counts": stats.counts, "bytes_by_kind": stats.bytes_by_kind,
            "extent": [stats.min_lat, stats.min_lon, stats.max_lat, stats.max_lon],
            "member_geometry": member_geometry}


_WORKER_ARGS = None


def _run_worker(index):
    path, zoom, needed_ways, workers, tmp_dir = _WORKER_ARGS
    return nodes_and_ways_worker(path, zoom, needed_ways, workers, index,
                                 os.path.join(tmp_dir, f"lines-{index}.gz"))


def read_nodes_and_ways(path, zoom, needed_ways, workers, tmp_dir, stats):
    """Pass 2 over [workers] processes; returns the relation member geometry."""
    global _WORKER_ARGS
    _WORKER_ARGS = (path, zoom, needed_ways, workers, tmp_dir)
    if workers == 1:
        results = [_run_worker(0)]
    else:
        import multiprocessing
        with multiprocessing.get_context("fork").Pool(workers) as pool:
            results = pool.map(_run_worker, range(workers))
    member_geometry = {}
    for r in results:
        for k, v in r["counts"].items():
            stats.count(k, v)
        for k, v in r["bytes_by_kind"].items():
            stats.bytes_by_kind[k] = stats.bytes_by_kind.get(k, 0) + v
        if r["extent"][0] <= r["extent"][2]:
            stats.extent(r["extent"])
        member_geometry.update(r["member_geometry"])
    return member_geometry


def write_relations(wanted, member_geometry, zoom, sink, stats):
    """Pass 3: relations, with member geometry ("a") or per-tile outline pieces ("p" only, or too big)."""
    n = 1 << zoom
    for rel_id, (kind, tags, members) in wanted.items():
        geoms = [(ref, role, member_geometry.get(ref)) for ref, role in members]
        geoms = [(ref, role, g) for ref, role, g in geoms if g is not None and len(g) >= 4]
        if not geoms:
            stats.count("relations_without_geometry")
            continue
        all_lats = [v for _, _, g in geoms for v in g[0::2]]
        all_lons = [v for _, _, g in geoms for v in g[1::2]]
        b = [min(all_lats), min(all_lons), max(all_lats), max(all_lons)]
        stats.extent(b)
        points = sum(len(g) // 2 for _, _, g in geoms)
        big = "a" in kind and points > MAX_FOREST_RELATION_POINTS
        element = {"t": "r", "id": rel_id, "k": kind, "tags": tags, "c": centre_of(b), "b": b}
        sink.add_seek(element)

        # Which tiles each member passes through, and the pieces inside each tile.
        tiles_all, pieces = line_pieces([g for _, _, g in geoms], n)

        if "a" in kind and not big:
            members = [{"r": ref, "o": role, "g": list(g)} for ref, role, g in geoms]
            if points >= SHARE_MIN_POINTS and len(tiles_all) >= SHARE_MIN_TILES:
                # Stored once; the tiles get a stub with their outline pieces.
                sink.add_object("r", rel_id, dumps(dict(element, m=members)), kind)
                sink.add_stub(dict(element, obj=1), pieces, kind)
                stats.count("relations_shared")
            else:
                element["m"] = members
                sink.add(tiles_all, dumps(element), kind)
        else:
            if big:
                element["big"] = 1
                stats.count("relations_big")
            for t in tiles_all:
                element["f"] = pieces[t]
                sink.add([t], dumps(element), kind)
            element.pop("f", None)
        stats.count("relations_" + kind)
        stats.count("relation_tile_entries", len(tiles_all))


class PartWriter:
    """Writes tiles (in tile-id order) into PMTiles parts below max_bytes each."""

    def __init__(self, out_dir, name, zoom, max_bytes, metadata):
        self.out_dir = out_dir
        self.name = name
        self.zoom = zoom
        self.max_bytes = max_bytes
        self.metadata = metadata
        self.parts = []
        self.writer = None
        self.file = None
        self.part_info = None

    def _open(self):
        index = len(self.parts) + 1
        filename = f"{self.name}-{index:02d}.pmtiles"
        self.file = open(os.path.join(self.out_dir, filename), "wb")
        self.writer = Writer(self.file)
        self.part_info = {"file": filename, "min_tile": None, "max_tile": None, "tiles": 0,
                          "bounds": [90.0, 180.0, -90.0, -180.0]}

    def _close(self):
        info = self.part_info
        b = info["bounds"]
        header = {
            "tile_type": TileType.UNKNOWN,
            "tile_compression": Compression.GZIP,
            "min_lon_e7": int(b[1] * 1e7), "min_lat_e7": int(b[0] * 1e7),
            "max_lon_e7": int(b[3] * 1e7), "max_lat_e7": int(b[2] * 1e7),
            "center_zoom": self.zoom,
            "center_lon_e7": int((b[1] + b[3]) / 2 * 1e7), "center_lat_e7": int((b[0] + b[2]) / 2 * 1e7),
        }
        self.writer.finalize(header, dict(self.metadata, part=info["file"]))
        self.file.close()
        info["bytes"] = os.path.getsize(os.path.join(self.out_dir, info["file"]))
        self.parts.append(info)
        self.writer = None

    def add(self, tile_id, data):
        # Room for the directories (a few MB at most) is kept free.
        reserve = min(64_000_000, self.max_bytes // 20)
        if self.writer is not None and self.writer.offset + len(data) > self.max_bytes - reserve:
            self._close()
        if self.writer is None:
            self._open()
        self.writer.write_tile(tile_id, data)
        info = self.part_info
        if info["min_tile"] is None:
            info["min_tile"] = tile_id
        info["max_tile"] = tile_id
        info["tiles"] += 1
        z, x, y = tileid_to_zxy(tile_id)
        if z != self.zoom:
            return  # a shared object: not a place on the map
        n = 1 << z
        lon0 = x / n * 360.0 - 180.0
        lon1 = (x + 1) / n * 360.0 - 180.0
        lat1 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
        lat0 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
        b = info["bounds"]
        info["bounds"] = [min(b[0], lat0), min(b[1], lon0), max(b[2], lat1), max(b[3], lon1)]

    def finish(self):
        if self.writer is not None:
            self._close()
        return self.parts


def _compress(item):
    """One tile: its JSON, gzip-compressed (runs in a worker process)."""
    tid, raw = item
    return tid, raw, gzip.compress(raw, compresslevel=9, mtime=0)


def sort_and_write(lines_glob, tmp_dir, part_writer, stats, sort_memory, workers=1):
    """Sorts the tile lines by tile id and writes one gzip JSON per tile (compressed on [workers] processes)."""
    cmd = (f"gzip -dc {lines_glob} | LC_ALL=C sort -S {sort_memory} --parallel=4 "
           f"-T '{tmp_dir}' --compress-program=gzip")
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, text=True, encoding="utf-8",
                            bufsize=1 << 20)

    def tiles_in_order():
        current = None
        elements = []
        last_line = None
        for line in proc.stdout:
            if line == last_line:
                continue  # the same element twice in one tile
            last_line = line
            tab = line.index("\t")
            tid = int(line[:tab])
            if tid != current:
                if current is not None:
                    yield current, ('{"v":%d,"e":[' % FORMAT_VERSION + ",".join(elements) + "]}").encode("utf-8")
                current = tid
                elements = []
            elements.append(line[tab + 1:-1])
        if current is not None:
            yield current, ('{"v":%d,"e":[' % FORMAT_VERSION + ",".join(elements) + "]}").encode("utf-8")

    largest = (0, None)
    total_raw = 0
    total_gz = 0
    tiles = 0

    def write(tid, raw, data):
        nonlocal largest, total_raw, total_gz, tiles
        part_writer.add(tid, data)
        tiles += 1
        total_raw += len(raw)
        total_gz += len(data)
        if len(data) > largest[0] and tileid_to_zxy(tid)[0] == part_writer.zoom:
            largest = (len(data), tid, len(raw), raw.count(b'"t":'))

    if workers > 1:
        import multiprocessing
        with multiprocessing.get_context("fork").Pool(workers) as pool:
            # imap keeps the order: tiles must be written by tile id.
            for tid, raw, data in pool.imap(_compress, tiles_in_order(), chunksize=32):
                write(tid, raw, data)
    else:
        for item in tiles_in_order():
            write(*_compress(item))
    if proc.wait() != 0:
        raise RuntimeError("sort failed")
    stats.counts["tiles"] = tiles
    stats.counts["tile_bytes_raw"] = total_raw
    stats.counts["tile_bytes_gzip"] = total_gz
    return largest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="filtered .osm.pbf (see filters.txt)")
    ap.add_argument("--name", required=True, help="region name, used in file names (e.g. netherlands)")
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--zoom", type=int, default=15)
    ap.add_argument("--max-part-bytes", type=int, default=1_900_000_000,
                    help="largest archive part (GitHub release assets must be under 2 GiB)")
    ap.add_argument("--tmp-dir", default=None, help="room for temporary files (default: next to --out-dir)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1, help="processes for the node/way pass")
    ap.add_argument("--sort-memory", default="40%", help="memory for sort(1)")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    tmp_root = args.tmp_dir or os.path.join(args.out_dir, "tmp")
    os.makedirs(tmp_root, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="carryolia-", dir=tmp_root)
    stats = Stats()

    header = osmium.FileProcessor(args.input, osmium.osm.RELATION).header
    osm_timestamp = header.get("osmosis_replication_timestamp", "")

    try:
        t = time.time()
        log("Pass 0/4: node coordinates onto the ways (osmium add-locations-to-ways)")
        located = os.path.join(tmp_dir, "located.osm.pbf")
        subprocess.run(["osmium", "add-locations-to-ways", args.input, "-o", located, "--no-progress"], check=True)
        stats.phases["locations"] = time.time() - t

        t = time.time()
        log("Pass 1/4: relations")
        wanted, needed_ways = read_relations(located, stats)
        stats.phases["relations"] = time.time() - t
        log(f"  {len(wanted):,} relations, {len(needed_ways):,} member ways needed")

        t = time.time()
        log(f"Pass 2/4: nodes and ways ({args.workers} processes)")
        member_geometry = read_nodes_and_ways(located, args.zoom, needed_ways, args.workers, tmp_dir, stats)
        os.remove(located)
        stats.phases["nodes_ways"] = time.time() - t
        lines_path = os.path.join(tmp_dir, "lines-relations.gz")
        sink = LineSink(lines_path, args.zoom, stats)

        t = time.time()
        log("Pass 3/4: relation geometry")
        write_relations(wanted, member_geometry, args.zoom, sink, stats)
        del member_geometry
        sink.close()
        stats.phases["relation_geometry"] = time.time() - t

        t = time.time()
        log(f"Pass 4/4: sorting {stats.counts.get('lines', 0):,} tile entries and writing tiles")
        metadata = {
            "name": f"Carryolia map data: {args.name}",
            "format": "carryolia-osm-json",
            "format_version": FORMAT_VERSION,
            "zoom": args.zoom,
            "region": args.name,
            "osm_timestamp": osm_timestamp,
            "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "attribution": "© OpenStreetMap contributors",
            "license": "ODbL-1.0 (https://opendatacommons.org/licenses/odbl/)",
        }
        part_writer = PartWriter(args.out_dir, args.name, args.zoom, args.max_part_bytes, metadata)
        largest = sort_and_write(os.path.join(tmp_dir, "lines-*.gz"), tmp_dir, part_writer, stats, args.sort_memory,
                                 workers=args.workers)
        parts = part_writer.finish()
        stats.phases["sort_write"] = time.time() - t
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    c = stats.counts
    tiles = c.get("tiles", 0)
    summary = {
        "region": args.name,
        "zoom": args.zoom,
        "format_version": FORMAT_VERSION,
        "osm_timestamp": osm_timestamp,
        "built": metadata["built"],
        "seconds": {k: round(v, 1) for k, v in stats.phases.items()},
        "seconds_total": round(time.time() - stats.started, 1),
        "counts": c,
        "json_bytes_by_kind": stats.bytes_by_kind,
        "bounds": [stats.min_lat, stats.min_lon, stats.max_lat, stats.max_lon],
        "parts": parts,
        "archive_bytes": sum(p["bytes"] for p in parts),
        "average_tile_gzip_bytes": round(c.get("tile_bytes_gzip", 0) / tiles) if tiles else 0,
        "largest_tile": None,
        # Since game 1.4.17: the archive holds the Seek index (see SEEK INDEX).
        "seek_index": SEEK_INDEX_VERSION,
        "seek_index_zoom": SEEK_INDEX_ZOOM,
    }
    if largest[1] is not None:
        z, x, y = tileid_to_zxy(largest[1])
        nn = 1 << z
        lon = (x + 0.5) / nn * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / nn))))
        summary["largest_tile"] = {"z": z, "x": x, "y": y, "lat": round(lat, 5), "lon": round(lon, 5),
                                   "gzip_bytes": largest[0], "json_bytes": largest[2], "elements": largest[3]}
    with open(os.path.join(args.out_dir, f"{args.name}.summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    mb = lambda v: f"{v / 1e6:,.1f} MB"
    print()
    print(f"=== Carryolia map data: {args.name} (zoom {args.zoom}) ===")
    print(f"OSM data as of:   {osm_timestamp or 'unknown'}")
    print(f"Time:             {summary['seconds_total'] / 60:.1f} min  " +
          ", ".join(f"{k} {v / 60:.1f}" for k, v in stats.phases.items()))
    print(f"Archive:          {mb(summary['archive_bytes'])} in {len(parts)} part(s)")
    print(f"Tiles:            {tiles:,}  (average {summary['average_tile_gzip_bytes'] / 1000:.1f} kB gzip)")
    if summary["largest_tile"]:
        lt = summary["largest_tile"]
        print(f"Largest tile:     {lt['gzip_bytes'] / 1000:.0f} kB gzip ({lt['elements']:,} elements) "
              f"at {lt['lat']}, {lt['lon']}")
    print(f"Objects:          {c.get('nodes_p', 0) + c.get('nodes_q', 0):,} point nodes, {c.get('ways_p', 0) + c.get('ways_q', 0):,} building ways, "
          f"{c.get('ways_a', 0):,} area ways, {c.get('ways_r', 0):,} roads")
    print(f"Relations:        {c.get('relations_wanted', 0):,} wanted, {c.get('relations_big', 0):,} too big, "
          f"{c.get('relation_tile_entries', 0):,} tile entries")
    print(f"Seek index:       {c.get('seek_entries', 0):,} entries (zoom {SEEK_INDEX_ZOOM} squares)")
    print(f"Shared objects:   {c.get('shared_objects', 0):,} ({c.get('ways_shared', 0):,} ways, "
          f"{c.get('relations_shared', 0):,} relations), stored once instead of in every tile")
    total = sum(stats.bytes_by_kind.values()) or 1
    print("JSON by kind:     " + ", ".join(
        f"{k} {v / total * 100:.0f}%" for k, v in sorted(stats.bytes_by_kind.items(), key=lambda kv: -kv[1])))
    for p in parts:
        print(f"  {p['file']}: {mb(p['bytes'])}, {p['tiles']:,} tiles")


if __name__ == "__main__":
    main()
