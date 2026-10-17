# Carryolia map data

Map data for the game Carryolia, built from OpenStreetMap. The game reads it
instead of asking the public Overpass servers for every spot a player visits.

## What is here

- `tools/plan_regions.py`: which Geofabrik extracts are built: every European country
  plus Turkey, without Russia and Ukraine; big countries are split into Geofabrik's
  smaller regions. Every region gets its own day of the month for its rebuild.
- `tools/filters.txt`: what to keep from an OpenStreetMap extract (the same
  objects the game asks Overpass for: buildings and places, resource areas, roads).
- `tools/build_tiles.py`: turns a filtered extract into PMTiles archives; each
  tile (zoom 15, about 750 m wide in the Netherlands) holds gzip-compressed JSON.
  The format is described at the top of the file.
- `tools/collect_releases.py` and `tools/make_manifest.py`: find the newest release
  of every region and write `map-data.json` from them.
- `tools/compare.py`: compares an archive with Overpass.
- `tools/tests/test_build.py`: a self-test on a made-up map; it compares what the
  archive delivers with a brute-force search over the original objects.
- `.github/workflows/build-map-data.yml`: rebuilds the regions (run by hand; the daily
  schedule that rebuilds each region on its own day is paused), each on its own GitHub
  machine, each published as its own release.
- `docs/`: the game's web pages (GitHub Pages): the
  [privacy policy](https://robsus.github.io/carryolia-map/privacy-policy.html).
- `status/regions.md`: what is published now, per region.

## The data

Every region has its own releases, `<region>-YYYY-MM-DD-r<run>`, holding its archive
parts (`<region>-NN.pmtiles`, each under 2 GiB), a summary and its outline. The two
newest per region are kept. The game starts from

    https://github.com/Robsus/carryolia-map/releases/download/map-data/map-data.json

which lists the newest release of every region.

## Building it yourself

    sudo apt install osmium-tool && pip install osmium pmtiles
    wget https://download.geofabrik.de/europe/netherlands-latest.osm.pbf
    osmium tags-filter netherlands-latest.osm.pbf --expressions=tools/filters.txt -o nl.osm.pbf
    python tools/build_tiles.py nl.osm.pbf --name netherlands --out-dir out

## Licence

The map data is © OpenStreetMap contributors and available under the
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/).
The archives in the releases are a derived database under the same licence.
Source extracts: [Geofabrik](https://download.geofabrik.de/).
