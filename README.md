# Carryolia map data

Map data for the game Carryolia, built from OpenStreetMap. The game reads it
instead of asking the public Overpass servers for every spot a player visits.

## What is here

- `tools/filters.txt`: what to keep from an OpenStreetMap extract (the same
  objects the game asks Overpass for: buildings and places, resource areas, roads).
- `tools/build_tiles.py`: turns a filtered extract into PMTiles archives; each
  tile (zoom 15, about 750 m wide in the Netherlands) holds gzip-compressed JSON.
  The format is described at the top of the file.
- `tools/make_manifest.py`: writes `map-data.json`, which lists every region,
  its outline and its archive parts.
- `tools/regions.txt`: the regions built by default.
- `tools/tests/test_build.py`: a self-test on a made-up map; it compares what the
  archive delivers with a brute-force search over the original objects.
- `.github/workflows/build-map-data.yml`: builds each region on its own GitHub
  machine and publishes the result as a release.

## The data

Every release holds the archive parts (`<region>-NN.pmtiles`, each under 2 GiB),
a summary per region and `map-data.json`. The game starts from

    https://github.com/Robsus/-carryolia-map/releases/latest/download/map-data.json

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
